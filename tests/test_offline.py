"""Behavioral regression tests using real SDK response validation and no network."""

from __future__ import annotations

import json
import socket
import tempfile
import unittest
from collections import Counter
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from aws_inventory.costs import fetch_costs
from aws_inventory.pricing import add_prices, extract_dimensions
from examples.simulate_export import (
    ACCOUNT, ASG, CENTRAL, CLUSTER, DATA_A, END, GENERATED, NODE_A, NODE_B,
    ROOT_A, ROOT_B, ROOT_STANDALONE, START, STANDALONE, UNATTACHED, WEST,
    FrozenDateTime, NetworkForbidden, SimulatedAWS, _product, offline_environment,
    read_csv, run_simulation,
)


class CompleteExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="costcsv-complete-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.output = Path(cls.directory.name) / "inventory.csv"
        cls.result = run_simulation(cls.output, workers=2)
        cls.rows = read_csv(cls.output)
        cls.inventory = {row["resource_id"]: row for row in cls.rows if row["row_type"] == "inventory"}

    def test_real_cli_writes_complete_excel_csv(self):
        self.assertEqual(self.result["exit_code"], 0, self.result["stderr"])
        self.assertEqual(self.result["network_attempts"], 0)
        self.assertTrue(self.output.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertEqual({row["report_status"] for row in self.rows}, {"complete"})
        self.assertEqual({row["generated_at_utc"] for row in self.rows}, {GENERATED})
        for row in self.rows:
            context = json.loads(row["query_context"])
            self.assertIs(context["simulation_only"], True)
            self.assertEqual(context["simulation_warning"],
                             "Fabricated offline fixture data; not AWS prices or billing.")
        report = next(row for row in self.rows if row["row_type"] == "report")
        self.assertTrue(report["notes"].startswith("OFFLINE SIMULATION: "))
        self.assertEqual(len(self.inventory), 11)
        coverage = [row for row in self.rows if row["row_type"] == "coverage"]
        self.assertEqual(len(coverage), 7)
        self.assertTrue(all(row["state"] == "complete" for row in coverage))

    def test_native_relationships_and_untagged_name_fallback(self):
        first, unnamed = self.inventory[NODE_A], self.inventory[NODE_B]
        self.assertEqual(first["resource_name"], "worker-a")
        self.assertEqual(first["storage_gib"], "132")
        self.assertEqual(first["vcpu"], "2")
        self.assertEqual(first["memory_mib"], "8192")
        self.assertEqual(unnamed["resource_name"], NODE_B)
        self.assertEqual(unnamed["name_source"], "resource_id")
        self.assertEqual(unnamed["tags"], "{}")
        self.assertEqual(unnamed["storage_gib"], "32")
        self.assertEqual(unnamed["parent_id"], ASG)
        self.assertEqual(json.loads(unnamed["cluster_name"]), [CLUSTER])
        self.assertIn("eks:DescribeNodegroup", unnamed["relationship_source"])
        self.assertEqual(json.loads(self.inventory[DATA_A]["attached_to"]), [NODE_A])
        self.assertEqual(self.inventory[DATA_A]["storage_gib"], "100")
        self.assertEqual(self.inventory[UNATTACHED]["storage_gib"], "200")
        self.assertEqual(self.inventory[UNATTACHED]["state"], "available")
        self.assertEqual(json.loads(self.inventory[UNATTACHED]["attached_to"]), [])
        self.assertEqual(json.loads(self.inventory[STANDALONE]["cluster_name"]), [])

    def test_cost_pagination_precision_and_negative_credit(self):
        costs = [row for row in self.rows if row["row_type"] == "cost_summary"]
        self.assertEqual(len(costs), 5)
        amounts = [Decimal(row["amount"].replace(",", ".")) for row in costs]
        self.assertEqual(sum(amounts), Decimal("274.72"))
        self.assertIn(Decimal("-1.20"), amounts)
        self.assertEqual({row["period_hours"] for row in costs}, {"720"})
        west_ec2 = next(row for row in costs if row["region"] == WEST and row["amount"] == "172,80")
        self.assertEqual(west_ec2["average_cost_per_hour"], "0,24")
        credit = next(row for row in costs if row["amount"] == "-1,20")
        self.assertTrue(credit["average_cost_per_hour"].startswith("-0,0016666"))
        self.assertFalse(credit["amount"].startswith("'"))
        for row in costs:
            context = json.loads(row["query_context"])
            self.assertEqual(context["cost_account_scope"], ACCOUNT)
            self.assertEqual(context["cost_effective_filter"], self.result["expected_cost_filter"])
        calls = [call for call in self.result["calls"] if call["service"] == "ce"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["parameters"]["NextPageToken"], "cost-page-2")

    def test_pricing_cache_and_component_units(self):
        for identifier in (NODE_A, NODE_B):
            row = self.inventory[identifier]
            self.assertEqual(row["list_price_per_hour"], "0,12")
            self.assertEqual(row["price_status"], "matched")
        self.assertEqual(self.inventory[STANDALONE]["list_price_per_hour"], "0,025")
        self.assertEqual(self.inventory[CLUSTER]["list_price_per_hour"], "")
        self.assertEqual(self.inventory[CLUSTER]["price_status"], "catalog_components")
        for identifier in (ROOT_A, ROOT_B, DATA_A, ROOT_STANDALONE, UNATTACHED):
            self.assertEqual(self.inventory[identifier]["list_price_per_hour"], "")
            self.assertEqual(self.inventory[identifier]["price_status"], "storage_components")
        quotes = [row for row in self.rows if row["row_type"] == "price_quote"]
        self.assertEqual(len(quotes), 13)
        self.assertEqual({row["price_unit"] for row in quotes}, {"Hrs", "GB-Mo", "IOPS-Mo"})
        pricing_calls = [call for call in self.result["calls"] if call["service"] == "pricing"]
        # Five distinct configurations; one deliberately paginated configuration.
        self.assertEqual(len(pricing_calls), 6)
        self.assertEqual(sum("NextToken" in call["parameters"] for call in pricing_calls), 1)

    def test_inventory_cache_avoids_duplicate_reads(self):
        calls = Counter((call["service"], call["region"], call["operation"])
                        for call in self.result["calls"])
        for region in (CENTRAL, WEST):
            self.assertEqual(calls[("ec2", region, "DescribeVolumes")], 1)
            self.assertEqual(calls[("autoscaling", region, "DescribeAutoScalingGroups")], 1)
            self.assertEqual(calls[("ec2", region, "DescribeInstanceTypes")], 1)
        self.assertEqual(calls[("ec2", WEST, "DescribeInstances")], 2)
        self.assertTrue(all(count == 1 for count in Counter(self.result["client_requests"]).values()))


class FailureAndScopeTests(unittest.TestCase):
    def test_partial_report_preserves_failure_and_exit_status(self):
        with tempfile.TemporaryDirectory(prefix="costcsv-partial-") as directory:
            output = Path(directory) / "partial.csv"
            result = run_simulation(output, deny_snapshots=True)
            self.assertEqual(result["exit_code"], 2, result["stderr"])
            rows = read_csv(output)
            self.assertEqual({row["report_status"] for row in rows}, {"partial"})
            errors = [row for row in rows if row["row_type"] == "error"]
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]["error_code"], "UnauthorizedOperation")
            coverage = next(row for row in rows if row["row_type"] == "coverage"
                            and row["region"] == WEST and row["service"] == "ebs")
            self.assertEqual(coverage["state"], "partial")

    def test_strict_failure_keeps_existing_output_unchanged(self):
        with tempfile.TemporaryDirectory(prefix="costcsv-strict-") as directory:
            output = Path(directory) / "strict.csv"
            original = b"An existing report must survive a failed strict export.\n"
            output.write_bytes(original)
            result = run_simulation(output, overwrite=True, strict=True, deny_snapshots=True)
            self.assertEqual(result["exit_code"], 1, result["stderr"])
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_custom_filter_cannot_remove_caller_account_constraint(self):
        custom = {"Or": [
            {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": ["999999999999"]}},
            {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [ACCOUNT]}},
        ]}
        with tempfile.TemporaryDirectory(prefix="costcsv-filter-") as directory:
            result = run_simulation(Path(directory) / "filtered.csv", custom_filter=custom, workers=1)
            self.assertEqual(result["exit_code"], 0, result["stderr"])
            call = next(call for call in result["calls"] if call["service"] == "ce")
            account_constraint = call["parameters"]["Filter"]["And"][0]["And"][0]
            self.assertEqual(account_constraint, {"And": [
                {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [ACCOUNT]}}, custom,
            ]})

    def test_dns_and_unstubbed_http_are_blocked(self):
        with offline_environment() as attempts:
            with self.assertRaises(NetworkForbidden):
                socket.getaddrinfo("example.invalid", 443)
            aws = SimulatedAWS()
            try:
                # Stubbers are intentionally inactive: the HTTP layer must fail closed.
                with self.assertRaises(NetworkForbidden):
                    aws.clients[("sts", WEST)].get_caller_identity()
            finally:
                for client in aws.clients.values():
                    client.close()
            self.assertEqual(len(attempts), 2)


class CostAndPriceEdgeCases(unittest.TestCase):
    def test_repeated_cost_page_token_aborts_entire_query(self):
        class RepeatedTokenContext:
            account_id = ACCOUNT
            cache = {}

            def call(self, *args, **kwargs):
                return {"NextPageToken": "repeated", "ResultsByTime": []}

        with self.assertRaisesRegex(ValueError, "(?i)repeat"):
            fetch_costs(RepeatedTokenContext(), start=START, end=END, granularity="MONTHLY",
                        metric="AmortizedCost", regions=None, services={"all"},
                        group_by=["SERVICE", "REGION"], filter_expression=None, ce_region="us-east-1")

    def test_eks_regional_alternatives_are_shared_between_clusters(self):
        class CatalogContext:
            generated_at = GENERATED
            account_id = ACCOUNT

            def __init__(self):
                self.calls = 0

            def call(self, *args, **kwargs):
                self.calls += 1
                return {"PriceList": [
                    _product("FAKE-STANDARD", "Standard support", "Hrs", "0.10"),
                    _product("FAKE-EXTENDED", "Extended support", "Hrs", "0.60"),
                ]}

            def issue(self, *args, **kwargs):
                raise AssertionError(f"Unexpected catalog error: {args!r} {kwargs!r}")

        context = CatalogContext()
        resources = [{"row_type": "inventory", "account_id": ACCOUNT, "service": "eks",
                      "region": WEST, "resource_type": "cluster", "resource_id": name,
                      "resource_name": name} for name in ("cluster-a", "cluster-b")]
        with patch("aws_inventory.pricing.datetime", FrozenDateTime):
            quotes = add_prices(context, resources, "us-east-1")
        self.assertEqual(context.calls, 1)
        self.assertEqual(len(quotes), 2)
        self.assertEqual({row["resource_type"] for row in quotes}, {"eks_regional_catalog"})
        self.assertTrue(all("list_price_per_hour" not in row for row in resources))

    def test_negative_catalog_rate_is_rejected(self):
        products = [json.loads(_product("FAKE-NEGATIVE", "Compute Instance", "Hrs", "-0.1"))]
        with patch("aws_inventory.pricing.datetime", FrozenDateTime), self.assertRaises(ValueError):
            extract_dimensions(products, "hourly_instance")


if __name__ == "__main__":
    unittest.main()
