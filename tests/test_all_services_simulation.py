"""Exercise every inventory collector through the offline CLI and HTML writer."""
import json
import re
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import costcsv
from examples.simulate_export import ACCOUNT, CLUSTER, STANDALONE, run_simulation
from examples.three_region_fixtures import REGIONS
from aws_inventory.service_catalog import SERVICES as DOCUMENTED_SERVICES, PRICED_SERVICES


class AllServicesSimulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="aws-all-services-tests-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.result = run_simulation(working_directory=cls.directory.name, report_format="html", all_services=True)
        cls.output = Path(cls.result["output"])
        cls.document = cls.output.read_text(encoding="utf-8")
        cls.model = json.loads(re.search(
            r'<script id="report-data" type="application/json">(.*?)</script>', cls.document, re.S)[1])

    def test_every_service_has_inventory_and_only_one_offline_html_is_published(self):
        self.assertEqual(self.result["exit_code"], 0, self.result["stderr"])
        self.assertEqual(self.result["network_attempts"], 0)
        self.assertEqual(list(Path(self.directory.name).rglob("*.html")), [self.output])
        services = {r["service"] for r in self.result["rows"] if r["row_type"] == "inventory"}
        self.assertEqual(services, set(costcsv.SERVICES))
        self.assertEqual(services, set(DOCUMENTED_SERVICES))
        for region in REGIONS:
            regional = {r["service"] for r in self.result["rows"] if r["row_type"] == "inventory" and r["region"] == region}
            self.assertEqual(regional, services - {"cloudfront", "route53"})
        self.assertEqual(self.model["context"]["inventory_regions"], list(REGIONS))
        inventory_count = sum(r["row_type"] == "inventory" for r in self.result["rows"])
        self.assertEqual(len(self.model["resources"])+len(self.model["structural"]), inventory_count)
        job = next(r for r in self.model["resources"] if r["service"] == "glue")
        self.assertNotIn("configuration", job)
        collected = next(r for r in self.result["rows"] if r["row_type"] == "inventory" and r["service"] == "glue")
        self.assertEqual(collected["details"]["NumberOfWorkers"], 2)
        self.assertFalse(self.model["partial"])
        self.assertEqual(self.model["issues"], [])
        self.assertTrue(self.model["simulation_only"])
        self.assertEqual(self.model["context"]["requested_services"], ["all"])

    def test_all_regions_discovery_and_global_inventory_do_not_restrict_billing(self):
        self.assertEqual(self.model["context"]["requested_regions"], ["all"])
        calls = self.result["calls"]
        discovery = [c for c in calls if c["operation"] == "DescribeRegions"]
        self.assertEqual(len(discovery), 1)
        self.assertEqual(discovery[0]["parameters"], {"AllRegions": False})
        for operation in ("ListDistributions", "ListHostedZones", "ListBuckets"):
            self.assertEqual(sum(c["operation"] == operation for c in calls), 1)
        for service in ("cloudfront", "route53"):
            self.assertEqual({r["region"] for r in self.model["resources"] if r["service"] == service}, {"global"})
        self.assertEqual(self.result["expected_cost_filter"],
                         {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [ACCOUNT]}})
        self.assertEqual(len(self.model["billing"]), 303)
        for month, total in (("2026-07", "6590.98"), ("2026-08", "7600.98"), ("2026-09", "8610.98")):
            self.assertEqual(sum(Decimal(b["amount"]) for b in self.model["billing"] if b["month"] == month), Decimal(total))

    def test_ecs_and_eks_dependencies_do_not_double_count_compute_or_disks(self):
        entries = {r["id"]: r for r in self.model["resources"] if r["region"] == REGIONS[0]}
        ecs = next(r for r in entries.values() if r["service"] == "ecs" and r["name"] == "demo-batch")
        self.assertIn(entries[STANDALONE]["key"], ecs["included_keys"])
        self.assertEqual(Decimal(ecs["totals"]["2026-09"]["amounts"]["USD"]), Decimal("54.16"))
        self.assertEqual(Decimal(entries[CLUSTER]["totals"]["2026-09"]["amounts"]["USD"]), Decimal("257.92"))
        self.assertEqual(Decimal(self.model["summary"]["2026-09"]["USD"]["total"]), Decimal("3047.04"))
        # EKS/EBS did not yet exist; EC2 only exposes an August launch in this fixture.
        self.assertEqual(Decimal(self.model["summary"]["2026-07"]["USD"]["total"]), Decimal("2238.696"))

    def test_rds_compute_is_partial_and_unsupported_prices_are_not_fabricated(self):
        db = next(r for r in self.model["resources"] if r["service"] == "rds")
        self.assertEqual(db["plan"]["rate_label"], "USD 0.15/h")
        self.assertEqual(Decimal(db["months"]["2026-09"]["amount"]), Decimal("108"))
        self.assertFalse(db["months"]["2026-09"]["complete"])
        for resource in self.model["resources"]:
            if resource["service"] not in PRICED_SERVICES:
                self.assertIsNone(resource["months"]["2026-09"]["amount"], resource["id"])

    def test_lambda_configuration_does_not_export_environment_secrets(self):
        self.assertTrue(any(r["service"] == "lambda" for r in self.model["resources"]))
        self.assertNotIn("DO-NOT-EXPORT-FAKE-SECRET", self.document)
        self.assertNotIn("API_SECRET", self.document)
        self.assertNotIn("DO-NOT-EXPORT-JOB-SECRET", self.document)
        self.assertNotIn("DO-NOT-EXPORT-EVENT-PAYLOAD", self.document)
        self.assertTrue(all(c["operation"].startswith(("Get", "List", "Describe")) for c in self.result["calls"]))

    def test_network_load_balancer_and_fargate_base_estimates(self):
        resources = [r for r in self.model["resources"] if r["region"] == REGIONS[0]]
        for name, amount in (("application-nat", "32.4"), ("public-api", "18"), ("session-cache", "36"),
                             ("search-catalog", "86.4"), ("analytics-warehouse", "360")):
            resource = next(r for r in resources if r["name"] == name)
            self.assertEqual(Decimal(resource["months"]["2026-09"]["amount"]), Decimal(amount))
            self.assertFalse(resource["months"]["2026-09"]["complete"])
        task = next(r for r in resources if r["service"] == "ecs" and r["plan"]["known"])
        self.assertEqual(Decimal(task["months"]["2026-09"]["amount"]), Decimal("34.56"))
        balancer = next(r for r in resources if r["name"] == "public-api")
        self.assertEqual(Decimal(balancer["totals"]["2026-09"]["amounts"]["USD"]), Decimal("114.96"))

    def test_native_cross_service_references_are_not_arbitrary_cost_allocations(self):
        for r in self.model["resources"]:
            if r["service"] == "eventbridge" and r["type"] == "rule":
                resolved = [ref for ref in r["relations"] if ref["key"]]
                self.assertTrue(any(ref["name"] == "orders-api" for ref in resolved))
                self.assertEqual(r["included_keys"], [r["key"]])
            if r["service"] == "cloudfront":
                self.assertTrue(any(ref["name"] == "static-site" and ref["key"] for ref in r["relations"]))


if __name__ == "__main__":
    unittest.main()
