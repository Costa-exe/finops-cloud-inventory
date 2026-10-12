"""Pure-data tests for monthly service report semantics; no AWS clients or I/O."""

import copy
import unittest
from decimal import Decimal

from aws_inventory.costs import EC2_COMPUTE, EC2_OTHER
from aws_inventory.report_views import build_service_reports


CONTEXT = {"caller_account_id": "123456789012", "period_start": "2026-07-15",
           "period_end": "2026-10-01", "metric": "AmortizedCost"}


def cost(amount, *, service=EC2_COMPUTE, region="eu-west-1", start="2026-07-15",
         end="2026-08-01", unit="USD", metric="AmortizedCost", category="cost_summary", **extra):
    return {"row_type": category, "service": service, "region": region, "amount": amount,
            "period_start": start, "period_end": end, "unit": unit, "metric": metric, **extra}


def inventory(service, kind, identifier, *, region="eu-west-1", name=None, **extra):
    return {"row_type": "inventory", "service": service, "resource_type": kind,
            "resource_id": identifier, "resource_name": name or identifier, "region": region,
            "name_source": "resource_id" if name is None else "native", **extra}


def reports(rows, context=CONTEXT):
    return {report.key: report for report in build_service_reports(
        rows, generated_at="2026-10-11T12:00:00+00:00", context=context, partial=False)}


def table(report, name):
    return next(value for value in report.tables if value.name == name)


class ReportViewsTests(unittest.TestCase):
    def test_monthly_totals_do_not_add_overlapping_breakdowns_or_quotes(self):
        rows = [cost("10.0000000000000000000000000000001"), cost("-1.25", service=EC2_OTHER),
                cost("20", start="2026-08-01", end="2026-09-01"),
                cost("4", category="cost_breakdown", service="ebs", cost_scope="ebs_region"),
                cost("10", category="cost_breakdown", service="ec2", cost_scope="ec2_instance_type"),
                cost("10", category="resource_cost", service="ec2", resource_id="i-1"),
                {"row_type": "price_quote", "service": "ec2", "price_per_unit": "1000"}]
        result = reports(rows)
        total = table(result["overview"], "Monthly total").rows[0]
        self.assertEqual(total["2026-07"], Decimal("8.7500000000000000000000000000001"))
        self.assertEqual(total["2026-08"], Decimal("20"))
        self.assertIsNone(total["2026-09"])
        self.assertEqual(total["total"], Decimal("28.7500000000000000000000000000001"))
        self.assertEqual(table(result["ebs"], "Monthly by region").rows[0]["2026-07"], Decimal("4"))
        self.assertIn("already included", table(result["ebs"], "Monthly by region").note)

    def test_currency_and_metric_remain_separate(self):
        result = reports([cost("5", unit="USD"), cost("6", unit="EUR"),
                          cost("7", metric="UnblendedCost")])
        totals = table(result["overview"], "Monthly total").rows
        self.assertEqual(len(totals), 3)
        self.assertEqual({(row["unit"], row["metric"], row["total"]) for row in totals},
                         {("USD", "AmortizedCost", Decimal(5)), ("EUR", "AmortizedCost", Decimal(6)),
                          ("USD", "UnblendedCost", Decimal(7))})

    def test_daily_costs_roll_up_into_partial_calendar_months(self):
        rows = [cost("2.30", start="2026-07-15", end="2026-07-16"),
                cost("1.20", start="2026-07-16", end="2026-07-17"),
                cost("4", start="2026-08-01", end="2026-08-02")]
        monthly = table(reports(rows)["overview"], "Monthly total")
        self.assertEqual([column.key for column in monthly.columns if column.kind == "money"],
                         ["2026-07", "2026-08", "2026-09", "total"])
        self.assertEqual(monthly.rows[0]["2026-07"], Decimal("3.50"))
        self.assertEqual(monthly.rows[0]["total"], Decimal("7.50"))
        self.assertIn("exclusive", monthly.note)

    def test_ambiguous_multimonth_amount_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "spans multiple months"):
            reports([cost("50", end="2026-09-01")])

    def test_resource_estimates_remain_visible_after_monthly_aggregation(self):
        rows = [inventory("ec2", "instance", "i-1", name="worker"),
                cost("2", service="ec2", category="resource_cost", resource_id="i-1",
                     start="2026-07-15", end="2026-07-16", estimated=True),
                cost("3", service="ec2", category="resource_cost", resource_id="i-1",
                     start="2026-07-16", end="2026-07-17", estimated=False)]
        monthly = table(reports(rows)["ec2"], "Monthly by instance").rows[0]
        self.assertEqual(monthly["2026-07"], Decimal("5"))
        self.assertIn("returned days only", monthly["status"])
        self.assertIn("includes AWS estimated amounts", monthly["status"])

    def test_hourly_billed_average_uses_actual_partial_month_hours(self):
        result = reports([cost("40.8"), cost("74.4", start="2026-08-01", end="2026-09-01"),
                          cost("-8.16", service=EC2_OTHER),
                          cost("-4.08", service="ebs", category="cost_breakdown", cost_scope="ebs_region")])
        ec2 = table(result["ec2"], "Monthly cost details")
        self.assertEqual(ec2.rows[0]["period_hours"], Decimal(17 * 24))
        self.assertEqual(ec2.rows[0]["average_cost_per_hour"], Decimal("0.1"))
        self.assertEqual(ec2.rows[1]["period_hours"], Decimal(31 * 24))
        self.assertEqual(ec2.rows[1]["average_cost_per_hour"], Decimal("0.1"))
        self.assertEqual(ec2.rows[0]["period_start"], "2026-07-15")
        self.assertEqual(ec2.rows[0]["period_end"], "2026-08-01")
        other = table(result["ec2_other"], "Monthly cost details").rows[0]
        self.assertEqual(other["average_cost_per_hour"], Decimal("-0.02"))
        ebs = table(result["ebs"], "Monthly cost details")
        self.assertEqual(ebs.rows[0]["average_cost_per_hour"], Decimal("-0.01"))
        self.assertIn("already included", ebs.note)
        self.assertEqual(len(table(result["overview"], "Monthly cost details").rows), 3)
        self.assertIn("Do not sum hourly averages", ec2.note)

    def test_instance_monthly_costs_never_inferred_from_type_or_price(self):
        row = inventory("ec2", "instance", "i-1", name="worker", instance_type="m6i.large",
                        list_price_per_hour="0.12", price_currency="USD")
        result = reports([row, cost("10"), cost("10", category="cost_breakdown", service="ec2",
                                             cost_scope="ec2_instance_type", group_2_key="INSTANCE_TYPE",
                                             group_2_value="m6i.large")])
        monthly = table(result["ec2"], "Monthly by instance").rows[0]
        self.assertEqual(monthly["resource_name"], "worker")
        self.assertIsNone(monthly["2026-07"])
        self.assertIsNone(monthly["total"])
        self.assertIn("Unavailable", monthly["status"])
        types = table(result["ec2"], "Monthly by instance type").rows[0]
        self.assertEqual(types["instance_type"], "m6i.large")
        self.assertEqual(types["2026-07"], Decimal("10"))

    def test_resource_cost_join_uses_region_and_retains_historical_resources(self):
        rows = [inventory("ec2", "instance", "i-same", name="west"),
                inventory("ec2", "instance", "i-same", name="central", region="eu-central-1"),
                cost("5", category="resource_cost", service="ec2", resource_id="i-same"),
                cost("3", category="resource_cost", service="ec2", resource_id="i-gone"),
                cost("1", category="resource_cost", service="ec2", resource_id="")]
        monthly = table(reports(rows)["ec2"], "Monthly by instance")
        indexed = {(row["region"], row["resource_id"]): row for row in monthly.rows}
        self.assertEqual(indexed[("eu-west-1", "i-same")]["resource_name"], "west")
        self.assertIsNone(indexed[("eu-central-1", "i-same")]["total"])
        self.assertEqual(indexed[("eu-west-1", "i-gone")]["resource_name"], "Not in current inventory")
        self.assertEqual(indexed[("eu-west-1", "")]["resource_name"], "Unallocated by AWS")

    def test_ec2_resource_arn_matches_current_instance_without_extra_blank_row(self):
        rows = [inventory("ec2", "instance", "i-abc123", name="worker"),
                cost("5", category="resource_cost", service="ec2",
                     resource_id="arn:aws:ec2:eu-west-1:123456789012:instance/i-abc123"),
                cost("7", category="resource_cost", service="ec2", resource_id="i-abc123",
                     start="2026-08-01", end="2026-09-01")]
        monthly = table(reports(rows)["ec2"], "Monthly by instance")
        self.assertEqual(len(monthly.rows), 1)
        self.assertEqual(monthly.rows[0]["resource_name"], "worker")
        self.assertEqual(monthly.rows[0]["resource_id"], "i-abc123")
        self.assertEqual(monthly.rows[0]["total"], Decimal(12))

    def test_unrelated_or_wrong_region_arns_are_not_associated_with_instances(self):
        for arn in ("arn:aws:ec2:eu-west-1:123456789012:volume/i-abc123",
                    "arn:aws:ec2:eu-central-1:123456789012:instance/i-abc123",
                    "arn:aws:ec2:eu-west-1:999999999999:instance/i-abc123",
                    "arn:aws:ecs:eu-west-1:123456789012:instance/i-abc123"):
            with self.subTest(arn=arn):
                rows = [inventory("ec2", "instance", "i-abc123", name="worker"),
                        cost("5", category="resource_cost", service="ec2", resource_id=arn)]
                monthly = table(reports(rows)["ec2"], "Monthly by instance")
                indexed = {row["resource_id"]: row for row in monthly.rows}
                self.assertEqual(len(indexed), 2)
                self.assertIsNone(indexed["i-abc123"]["total"])
                self.assertEqual(indexed[arn]["total"], Decimal(5))

    def test_cluster_membership_requires_matching_region(self):
        rows = [inventory("eks", "cluster", "shared", name="shared"),
                inventory("ec2", "instance", "i-west", cluster_name=["shared"],
                          details={"cluster_links": [{"cluster_name": "shared", "source": "native", "parent_id": "workers"}]}),
                inventory("ec2", "instance", "i-central", region="eu-central-1", cluster_name=["shared"]),
                inventory("ebs", "volume", "vol-west", cluster_name=["shared"], storage_gib=32),
                inventory("ebs", "volume", "vol-central", region="eu-central-1", cluster_name=["shared"])]
        eks = reports(rows)["eks"]
        self.assertEqual([row["resource_id"] for row in table(eks, "Cluster nodes").rows], ["i-west"])
        self.assertEqual(table(eks, "Cluster nodes").rows[0]["nodegroup"], "workers")
        self.assertEqual([row["resource_id"] for row in table(eks, "Cluster volumes").rows], ["vol-west"])
        self.assertIsNone(table(eks, "Monthly by cluster").rows[0]["total"])

    def test_attached_disk_matches_are_not_duplicated(self):
        rows = [inventory("ec2", "instance", "i-1", name="worker", details={"block_devices": [
                    {"volume_id": "vol-1", "device_name": "/dev/xvda", "size_gib": 32, "type": "gp3"}]}),
                inventory("ebs", "volume", "vol-1", storage_gib=32, details={"attachments": [
                    {"InstanceId": "i-1", "Device": "/dev/xvda", "State": "attached"}]}),
                inventory("ebs", "volume", "vol-free", state="available", storage_gib=200, attached_to=[])]
        result = reports(rows)
        disks = table(result["ec2"], "Attached disks").rows
        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0]["storage_gib"], 32)
        self.assertEqual(table(result["ebs"], "Unattached volumes").rows[0]["resource_id"], "vol-free")

    def test_sensitive_attributes_redacted_without_mutating_input(self):
        rows = [inventory("rds", "db_instance", "db", tags={"password": "secret", "team": "finance"},
                          details={"clientSecret": "sensitive", "nested": [{"value": 12}]})]
        original = copy.deepcopy(rows)
        rds = reports(rows)["rds"]
        tags = {row["tag"]: row["value"] for row in table(rds, "Tags").rows}
        attrs = {row["attribute"]: row["value"] for row in table(rds, "Attributes").rows}
        self.assertEqual(tags["password"], "[REDACTED]")
        self.assertEqual(attrs["clientSecret"], "[REDACTED]")
        self.assertEqual(attrs["nested[0].value"], 12)
        self.assertEqual(rows, original)

    def test_compact_inventory_preserves_omitted_sdk_metadata_in_attributes(self):
        rows = [inventory("ebs", "volume", "vol-1", created_at="2026-07-01T00:00:00+00:00",
                          resource_arn="arn:aws:ec2:eu-west-1:123456789012:volume/vol-1",
                          related_resource_ids=["i-1"], relationship_source="AWS attachment", encrypted=True)]
        attrs = {row["attribute"]: row["value"] for row in table(reports(rows)["ebs"], "Attributes").rows}
        self.assertEqual(attrs["resource.created_at"], "2026-07-01T00:00:00+00:00")
        self.assertEqual(attrs["resource.resource_arn"], rows[0]["resource_arn"])
        self.assertEqual(attrs["resource.related_resource_ids[0]"], "i-1")
        self.assertEqual(attrs["resource.relationship_source"], "AWS attachment")

    def test_unknown_service_names_are_safe_distinct_and_readable(self):
        result = reports([cost("1", service="../../Company/Service"), cost("2", service="Company Service")])
        service_reports = [report for key, report in result.items() if key != "overview"]
        self.assertEqual(len(service_reports), 2)
        self.assertNotEqual(service_reports[0].key, service_reports[1].key)
        for report in service_reports:
            self.assertRegex(report.key, r"^billing_[a-z0-9_]+_[0-9a-f]{10}$")
        self.assertEqual({report.title for report in service_reports}, {"../../Company/Service", "Company Service"})

    def test_service_without_resources_still_has_coverage(self):
        result = reports([{"row_type": "coverage", "service": "eks", "region": "eu-west-1",
                           "state": "complete", "resource_count": 0}])
        self.assertIn("eks", result)
        self.assertEqual(table(result["eks"], "Coverage").rows[0]["resource_count"], 0)
        self.assertEqual(table(result["eks"], "Monthly by region").rows, [])

    def test_simulation_and_exact_period_visible_in_read_me(self):
        context = {**CONTEXT, "simulation_only": True}
        readme = table(reports([], context)["overview"], "Read me")
        entries = {row["item"]: row["value"] for row in readme.rows}
        self.assertIn("OFFLINE SIMULATION", entries)
        self.assertEqual(entries["Period start (inclusive)"], "2026-07-15")
        self.assertEqual(entries["Period end (exclusive)"], "2026-10-01")


if __name__ == "__main__":
    unittest.main()
