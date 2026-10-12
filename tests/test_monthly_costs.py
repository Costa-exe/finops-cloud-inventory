"""Monthly service details stay scoped, paginated, and separate from primary totals."""

from __future__ import annotations

import unittest
from copy import deepcopy
from decimal import Decimal

from botocore.exceptions import ClientError

from aws_inventory.costs import EC2_COMPUTE, EC2_OTHER, fetch_service_breakdowns


ACCOUNT = "123456789012"
START = "2026-08-15"
END = "2026-10-01"
ENDPOINT = "us-east-1"
VIEW = "arn:aws:billing::123456789012:billingview/example"
METRIC = "AmortizedCost"


def dimension(key, values):
    return {"Dimensions": {"Key": key, "Values": values}}


def expression(*parts):
    return parts[0] if len(parts) == 1 else {"And": list(parts)}


def cost_request(base, service_filter, groups, *, view=None):
    request = {
        "TimePeriod": {"Start": START, "End": END},
        "Granularity": "MONTHLY", "Metrics": [METRIC],
        "Filter": expression(base, service_filter),
        "GroupBy": [{"Type": "DIMENSION", "Key": key} for key in groups],
    }
    if view:
        request["BillingViewArn"] = view
    return request


def discovery_request(base, *, view=None):
    request = {
        "TimePeriod": {"Start": START, "End": END},
        "Dimension": "USAGE_TYPE", "Context": "COST_AND_USAGE",
        "Filter": expression(base, dimension("SERVICE", [EC2_OTHER])),
    }
    if view:
        request["BillingViewArn"] = view
    return request


def cost_page(groups, keys, amount="12.34", *, start=START, end="2026-09-01",
              token=None):
    page = {
        "GroupDefinitions": [{"Type": "DIMENSION", "Key": key} for key in groups],
        "ResultsByTime": [{
            "TimePeriod": {"Start": start, "End": end}, "Estimated": False,
            "Groups": [{"Keys": keys,
                        "Metrics": {METRIC: {"Amount": amount, "Unit": "USD"}}}],
        }],
    }
    if token:
        page["NextPageToken"] = token
    return page


class ScriptedContext:
    """No session/client exists; every accepted call must match the full request."""

    account_id = ACCOUNT

    def __init__(self, test, script=()):
        self.test = test
        self.script = list(script)
        self.cache = {}
        self.issues = []
        self.calls = []

    def call(self, service, operation, region, **parameters):
        self.test.assertTrue(self.script, "Unexpected SDK call")
        expected_operation, expected_parameters, result = self.script.pop(0)
        self.test.assertEqual((service, operation, region), ("ce", expected_operation, ENDPOINT))
        self.test.assertEqual(parameters, expected_parameters)
        self.calls.append((operation, deepcopy(parameters)))
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)

    def issue(self, service, region, operation, message, **kwargs):
        self.issues.append({"service": service, "region": region, "operation": operation,
                            "message": message, **kwargs})


class MonthlyServiceCostTests(unittest.TestCase):
    def fetch(self, ctx, *, services=None, regions=None, custom=None, view=None):
        result = fetch_service_breakdowns(
            ctx, start=START, end=END, metric=METRIC, regions=regions,
            services={"ec2"} if services is None else services,
            filter_expression=custom, ce_region=ENDPOINT, billing_view_arn=view)
        self.assertEqual(ctx.script, [], "Expected SDK calls were not consumed")
        return result

    def test_exact_monthly_scope_pagination_dates_and_independent_cost_views(self):
        custom = {"Or": [dimension("LINKED_ACCOUNT", [ACCOUNT]),
                         dimension("LINKED_ACCOUNT", ["999999999999"])]}
        original_filter = deepcopy(custom)
        base = expression(expression(dimension("LINKED_ACCOUNT", [ACCOUNT]), custom),
                          dimension("REGION", ["eu-central-1", "eu-west-1"]))
        compute = cost_request(base, dimension("SERVICE", [EC2_COMPUTE]),
                               ["REGION", "INSTANCE_TYPE"], view=VIEW)
        discovery = discovery_request(base, view=VIEW)
        ebs_filter = expression(dimension("SERVICE", [EC2_OTHER]),
                                dimension("USAGE_TYPE", ["EU-EBS:VolumeUsage.gp3",
                                                         "EUC1-EBS:SnapshotUsage"]))
        ebs = cost_request(base, ebs_filter, ["REGION", "SERVICE"], view=VIEW)
        ctx = ScriptedContext(self, [
            ("get_cost_and_usage", compute,
             cost_page(["REGION", "INSTANCE_TYPE"], ["eu-west-1", "m6i.large"],
                       "40.80", token="compute-page-2")),
            ("get_cost_and_usage", {**compute, "NextPageToken": "compute-page-2"},
             cost_page(["REGION", "INSTANCE_TYPE"], ["eu-central-1", "t3.small"],
                       "18.00", start="2026-09-01", end=END)),
            ("get_dimension_values", discovery, {
                "DimensionValues": [{"Value": "EU-NatGateway-Hours"},
                                    {"Value": "EUC1-EBS:SnapshotUsage"}],
                "NextPageToken": "usage-page-2"}),
            ("get_dimension_values", {**discovery, "NextPageToken": "usage-page-2"}, {
                "DimensionValues": [{"Value": "EU-EBS:VolumeUsage.gp3"},
                                    {"Value": "EUC1-EBS:SnapshotUsage"}]}),
            ("get_cost_and_usage", ebs,
             cost_page(["REGION", "SERVICE"], ["eu-central-1", EC2_OTHER], "-1.20",
                       start="2026-09-01", end=END)),
        ])
        rows = self.fetch(ctx, services={"ec2", "eks"},
                          regions=["eu-west-1", "eu-central-1", "eu-west-1"],
                          custom=custom, view=VIEW)
        self.assertEqual(ctx.issues, [])
        self.assertEqual(custom, original_filter)
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["row_type"] for row in rows}, {"cost_breakdown"})
        first, second, storage = rows
        self.assertEqual((first["service"], first["cost_scope"], first["instance_type"]),
                         ("ec2", "ec2_instance_type", "m6i.large"))
        self.assertEqual((first["period_hours"], first["average_cost_per_hour"]),
                         ("408", "0.10"))
        self.assertEqual(second["period_hours"], "720")
        self.assertEqual(storage["amount"], "-1.20")
        self.assertLess(Decimal(storage["average_cost_per_hour"]), 0)
        self.assertEqual((storage["service"], storage["cost_scope"], storage["group_2_value"]),
                         ("ebs", "ebs_region", EC2_OTHER))
        self.assertNotIn("resource_id", first)
        self.assertIn("overlaps", storage["notes"])
        for row in rows:
            self.assertEqual(row["metric"], METRIC)
            self.assertEqual(row["unit"], "USD")
            self.assertIs(row["estimated"], False)
            self.assertEqual(row["query_context"]["cost_granularity"], "MONTHLY")
            self.assertIs(row["query_context"]["overlaps_primary_cost_summary"], True)
            self.assertEqual(row["query_context"]["report_requested_services"], ["ec2", "eks"])

    def test_cluster_only_selection_does_not_fetch_ec2_dependency_costs(self):
        ctx = ScriptedContext(self)
        ctx.selected_services = {"eks", "ec2", "ebs"}
        self.assertEqual(self.fetch(ctx, services={"eks"}), [])
        self.assertEqual(ctx.issues, [])

    def test_ebs_only_selection_does_not_fetch_compute_and_reuses_discovery_cache(self):
        base = dimension("LINKED_ACCOUNT", [ACCOUNT])
        ebs_filter = expression(dimension("SERVICE", [EC2_OTHER]),
                                dimension("USAGE_TYPE", ["EBS:VolumeUsage.gp3"]))
        ebs = cost_request(base, ebs_filter, ["REGION", "SERVICE"])
        result = cost_page(["REGION", "SERVICE"], ["us-east-1", EC2_OTHER])
        ctx = ScriptedContext(self, [
            ("get_dimension_values", discovery_request(base),
             {"DimensionValues": [{"Value": "EBS:VolumeUsage.gp3"}]}),
            ("get_cost_and_usage", ebs, result),
        ])
        first = self.fetch(ctx, services={"ebs"}, regions=["all"])
        ctx.script.append(("get_cost_and_usage", ebs, result))
        second = self.fetch(ctx, services={"ebs"}, regions=["all"])
        self.assertEqual(first, second)
        self.assertEqual([name for name, _ in ctx.calls],
                         ["get_dimension_values", "get_cost_and_usage", "get_cost_and_usage"])

    def test_empty_ebs_discovery_does_not_invent_a_zero_month(self):
        base = dimension("LINKED_ACCOUNT", [ACCOUNT])
        ctx = ScriptedContext(self, [
            ("get_dimension_values", discovery_request(base),
             {"DimensionValues": [{"Value": "DataTransfer-Out-Bytes"},
                                  {"Value": "NatGateway-Hours"}]}),
        ])
        self.assertEqual(self.fetch(ctx, services={"ebs"}), [])
        self.assertEqual(ctx.issues, [])

    def test_failed_compute_later_page_discards_compute_but_preserves_complete_ebs(self):
        base = dimension("LINKED_ACCOUNT", [ACCOUNT])
        compute = cost_request(base, dimension("SERVICE", [EC2_COMPUTE]),
                               ["REGION", "INSTANCE_TYPE"])
        error = ClientError({"Error": {"Code": "AccessDeniedException",
                                        "Message": "No detail access"}}, "GetCostAndUsage")
        ebs_filter = expression(dimension("SERVICE", [EC2_OTHER]),
                                dimension("USAGE_TYPE", ["EBS:VolumeUsage.gp3"]))
        ctx = ScriptedContext(self, [
            ("get_cost_and_usage", compute,
             cost_page(["REGION", "INSTANCE_TYPE"], ["us-east-1", "m6i.large"], token="next")),
            ("get_cost_and_usage", {**compute, "NextPageToken": "next"}, error),
            ("get_dimension_values", discovery_request(base),
             {"DimensionValues": [{"Value": "EBS:VolumeUsage.gp3"}]}),
            ("get_cost_and_usage", cost_request(base, ebs_filter, ["REGION", "SERVICE"]),
             cost_page(["REGION", "SERVICE"], ["us-east-1", EC2_OTHER])),
        ])
        rows = self.fetch(ctx, services={"all"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["service"], "ebs")
        self.assertEqual(len(ctx.issues), 1)
        self.assertEqual(ctx.issues[0]["service"], "ec2")
        self.assertEqual(ctx.issues[0]["code"], "AccessDeniedException")
        self.assertIn("ec2_instance_type", ctx.issues[0]["message"])

    def test_repeated_tokens_discard_both_partial_details_and_partial_discovery(self):
        base = dimension("LINKED_ACCOUNT", [ACCOUNT])
        compute = cost_request(base, dimension("SERVICE", [EC2_COMPUTE]),
                               ["REGION", "INSTANCE_TYPE"])
        page = cost_page(["REGION", "INSTANCE_TYPE"], ["us-east-1", "m6i.large"], token="loop")
        discovery = discovery_request(base)
        usage_page = {"DimensionValues": [{"Value": "EBS:VolumeUsage.gp3"}],
                      "NextPageToken": "loop"}
        ctx = ScriptedContext(self, [
            ("get_cost_and_usage", compute, page),
            ("get_cost_and_usage", {**compute, "NextPageToken": "loop"}, page),
            ("get_dimension_values", discovery, usage_page),
            ("get_dimension_values", {**discovery, "NextPageToken": "loop"}, usage_page),
        ])
        self.assertEqual(self.fetch(ctx), [])
        self.assertEqual(ctx.cache, {})
        self.assertEqual({issue["service"] for issue in ctx.issues}, {"ec2", "ebs"})
        self.assertTrue(all("repeated" in issue["message"] for issue in ctx.issues))

    def test_global_region_discovery_preserves_aws_values_and_normalizes_display(self):
        base = dimension("LINKED_ACCOUNT", [ACCOUNT])
        global_request = {"TimePeriod": {"Start": START, "End": END},
                          "Dimension": "REGION", "Context": "COST_AND_USAGE", "Filter": base}
        region_base = expression(base, dimension("REGION", ["", "NoRegion"]))
        ebs_filter = expression(dimension("SERVICE", [EC2_OTHER]),
                                dimension("USAGE_TYPE", ["EBS:SnapshotUsage"]))
        ctx = ScriptedContext(self, [
            ("get_dimension_values", global_request,
             {"DimensionValues": [{"Value": ""}, {"Value": "NoRegion"}, {"Value": "us-east-1"}]}),
            ("get_dimension_values", discovery_request(region_base),
             {"DimensionValues": [{"Value": "EBS:SnapshotUsage"}]}),
            ("get_cost_and_usage", cost_request(region_base, ebs_filter, ["REGION", "SERVICE"]),
             cost_page(["REGION", "SERVICE"], ["NoRegion", EC2_OTHER])),
        ])
        row, = self.fetch(ctx, services={"ebs"}, regions=["global"])
        self.assertEqual(row["region"], "global")
        self.assertEqual(row["group_1_value"], "NoRegion")

    def test_invalid_dimensions_abort_details_without_partial_rows(self):
        base = dimension("LINKED_ACCOUNT", [ACCOUNT])
        ebs_filter = expression(dimension("SERVICE", [EC2_OTHER]),
                                dimension("USAGE_TYPE", ["EBS:SnapshotUsage"]))
        malformed = cost_page(["SERVICE", "REGION"], [EC2_OTHER, "us-east-1"])
        ctx = ScriptedContext(self, [
            ("get_dimension_values", discovery_request(base),
             {"DimensionValues": [{"Value": "EBS:SnapshotUsage"}]}),
            ("get_cost_and_usage", cost_request(base, ebs_filter, ["REGION", "SERVICE"]), malformed),
        ])
        self.assertEqual(self.fetch(ctx, services={"ebs"}), [])
        self.assertEqual(ctx.issues[0]["code"], "ValueError")
        self.assertIn("unexpected dimensions", ctx.issues[0]["message"])


if __name__ == "__main__":
    unittest.main()
