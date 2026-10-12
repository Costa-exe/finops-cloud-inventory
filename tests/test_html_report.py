"""Resource-cost arithmetic, attribution, HTML safety and a real offline export."""
import base64
import hashlib
import json
import re
import stat
import tempfile
import unittest
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import costcsv
from aws_inventory.estimates import build_cost_model, monthly_cost, resource_plan
from aws_inventory.html_report import SCRIPT, render_html, write_html_report
from examples.simulate_export import run_simulation, NODE_A, NODE_B, CLUSTER, DATA_A, UNATTACHED


GENERATED = "2026-10-11T12:00:00+00:00"
CONTEXT = {"caller_account_id": "123456789012", "period_start": "2026-09-01", "period_end": "2026-10-01"}


def resource(identifier="i-one", service="ec2", kind="instance", **fields):
    return {"row_type": "inventory", "service": service, "resource_type": kind,
            "resource_id": identifier, "resource_name": identifier, "region": "eu-west-1",
            "state": "running", "price_status": "matched", "price_currency": "USD",
            "list_price_per_hour": "0.12", **fields}


def quote(identifier, rate="0.08", unit="GB-Mo", service="ebs", **fields):
    return {"row_type": "price_quote", "service": service, "resource_id": identifier,
            "region": "eu-west-1", "price_sku": identifier+unit, "price_per_unit": rate,
            "price_unit": unit, "price_currency": "USD", "price_begin_range": "0",
            "price_end_range": "Inf", "price_component": "Storage", **fields}


def build(rows, **context):
    return build_cost_model(rows, generated_at=GENERATED, context={**CONTEXT, **context})


def actual(amount, start="2026-09-01", end="2026-10-01", identifier="i-one", **fields):
    return {"row_type": "resource_cost", "service": "ec2", "region": "eu-west-1",
            "resource_id": identifier, "amount": amount, "unit": "USD",
            "period_start": start, "period_end": end, "metric": "AmortizedCost", **fields}


class CostEstimationTests(unittest.TestCase):
    def test_actual_month_hours_runtime_and_stopped_compute(self):
        model = build([resource()], period_start="2026-08-16", period_end="2026-10-01", estimate_hours_per_day="8")
        months = model["resources"][0]["months"]
        self.assertEqual(months["2026-08"]["amount"], Decimal("15.36"))
        self.assertEqual(months["2026-09"]["amount"], Decimal("28.80"))
        stopped = build([resource(state="stopped")])["resources"][0]
        self.assertEqual(stopped["months"]["2026-09"]["amount"], 0)
        self.assertEqual(stopped["plan"]["rate_label"], "USD 0.12/h")

    def test_ebs_excess_iops_and_throughput_with_partial_month(self):
        volume = resource("vol-a", "ebs", "volume", storage_gib=100, storage_type="gp3", iops=4000,
                          throughput_mib_s=150, list_price_per_hour=None)
        rows = [volume, quote("vol-a"), quote("vol-a", "0.005", "IOPS-Mo"), quote("vol-a", "0.04", "MBps-Mo")]
        model = build(rows, period_end="2026-09-16")
        cost = model["resources"][0]["months"]["2026-09"]
        self.assertEqual(cost["amount"], Decimal("7"))  # (8 + 5 + 1) / 2
        self.assertTrue(cost["complete"])
        volume.update(iops=3000, throughput_mib_s=125)
        baseline = build([volume, quote("vol-a")])["resources"][0]["months"]["2026-09"]
        self.assertEqual(baseline["amount"], Decimal("8"))
        self.assertTrue(baseline["complete"])

    def test_missing_or_tiered_extra_price_is_a_partial_subtotal(self):
        volume = resource("vol-a", "ebs", "volume", storage_gib=100, storage_type="io2", iops=40000)
        tier = quote("vol-a", "0.06", "IOPS-Mo", price_end_range="32000")
        cost = build([volume, quote("vol-a"), tier])["resources"][0]["months"]["2026-09"]
        self.assertEqual(cost["amount"], Decimal("8"))
        self.assertFalse(cost["complete"])

    def test_billed_resource_cost_replaces_estimate_and_preserves_credit(self):
        cost = build([resource(), actual("-1.23456789")])["resources"][0]["months"]["2026-09"]
        self.assertEqual(cost["amount"], Decimal("-1.23456789"))
        self.assertEqual(cost["basis"], "billed")
        self.assertIsNone(cost["estimated"])
        self.assertTrue(cost["complete"])

    def test_partial_resource_billing_fills_only_uncovered_hours(self):
        cost = build([resource(), actual("12", end="2026-09-11")])["resources"][0]["months"]["2026-09"]
        self.assertEqual(cost["billed"], Decimal("12"))
        self.assertEqual(cost["estimated"], Decimal("57.60"))
        self.assertEqual(cost["amount"], Decimal("69.60"))
        self.assertEqual(cost["basis"], "mixed")
        self.assertEqual(cost["covered_hours"], 240)

    def test_no_currency_conversion_and_no_overlapping_billing(self):
        cost = build([resource(), actual("12", end="2026-09-11", unit="EUR")])["resources"][0]["months"]["2026-09"]
        self.assertEqual(cost["amount"], Decimal(12))
        self.assertEqual(cost["currency"], "EUR")
        self.assertFalse(cost["complete"])
        self.assertIsNone(cost["estimated"])
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            build([resource(), actual("12"), actual("12")])

    def test_shared_volume_is_counted_once_and_links_respect_region(self):
        volume = resource("vol-a", "ebs", "volume", storage_gib=100, storage_type="gp2", attached_to=["i-a", "i-b"])
        model = build([resource("i-a"), resource("i-b"), resource("i-a", region="eu-central-1"), volume, quote("vol-a")])
        self.assertEqual(model["summary"]["2026-09"]["USD"]["total"], Decimal("267.20"))
        for instance in [r for r in model["resources"] if r["service"] == "ec2"]:
            expected = "86.40" if instance["region"] == "eu-central-1" else "94.40"
            self.assertEqual(instance["totals"]["2026-09"]["amounts"]["USD"], Decimal(expected))

    def test_uncollected_attachment_marks_incomplete_and_unknown_is_not_zero(self):
        model = build([resource(related_resource_ids=["vol-missing"]),
                       resource("bucket", "s3", "bucket", list_price_per_hour=None, price_status="unsupported")])
        machine = next(r for r in model["resources"] if r["service"] == "ec2")
        self.assertFalse(machine["totals"]["2026-09"]["complete"])
        bucket = next(r for r in model["resources"] if r["service"] == "s3")
        self.assertIsNone(bucket["months"]["2026-09"]["amount"])
        self.assertEqual(model["summary"]["2026-09"]["USD"]["total"], Decimal("86.40"))

    def test_eks_support_is_an_explicit_unique_choice(self):
        cluster = resource("demo", "eks", "cluster", list_price_per_hour=None)
        rates = [quote("catalog", "0.10", "Hrs", "eks", resource_type="eks_regional_catalog", price_component="EKS standard support"),
                 quote("catalog", "0.60", "Hrs", "eks", resource_type="eks_regional_catalog", price_component="EKS extended support")]
        standard = build([cluster, *rates])["resources"][0]
        extended = build([cluster, *rates], eks_support="extended")["resources"][0]
        self.assertEqual(standard["months"]["2026-09"]["amount"], Decimal(72))
        self.assertEqual(extended["months"]["2026-09"]["amount"], Decimal(432))
        self.assertIn("assumed", standard["plan"]["components"][0]["label"])

    def test_no_allocation_of_service_total_to_a_resource(self):
        summary = {**actual("999"), "row_type": "cost_summary", "service": "Amazon Elastic Compute Cloud - Compute"}
        model = build([resource(), summary])
        self.assertEqual(model["resources"][0]["months"]["2026-09"]["amount"], Decimal("86.40"))
        self.assertEqual(model["billing"][0]["amount"], Decimal(999))


class HTMLReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="aws-html-tests-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.result = run_simulation(working_directory=cls.directory.name, report_format="html")
        cls.output = Path(cls.result["output"])
        cls.document = cls.output.read_text()
        cls.model = json.loads(re.search(r'<script id="report-data" type="application/json">(.*?)</script>', cls.document, re.S)[1])

    def test_single_html_full_cli_no_aws_and_exact_resource_plus_attachment_costs(self):
        self.assertEqual(self.result["exit_code"], 0, self.result["stderr"])
        self.assertEqual(self.result["network_attempts"], 0)
        self.assertEqual(len(self.result["calls"]), 27)
        self.assertEqual(list(Path(self.directory.name).rglob("*.html")), [self.output])
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        entries = {r["id"]: r for r in self.model["resources"]}
        self.assertEqual(Decimal(entries[NODE_A]["totals"]["2026-09"]["amounts"]["USD"]), Decimal("96.96"))
        self.assertEqual(Decimal(entries[CLUSTER]["totals"]["2026-09"]["amounts"]["USD"]), Decimal("257.92"))
        self.assertEqual(len(entries[CLUSTER]["included_keys"]), 6)
        self.assertEqual(Decimal(self.model["summary"]["2026-09"]["USD"]["total"]), Decimal("297.92"))
        self.assertEqual(Decimal(self.model["summary"]["2026-07"]["USD"]["total"]), Decimal("306.68"))
        self.assertTrue(self.model["simulation_only"])

    def test_html_escapes_script_terminators_and_redacts_secrets(self):
        injected = '</script><img src=x onerror="alert(1)">'
        model = build([resource(resource_name=injected)], filter={"password": "do-not-leak"})
        document = render_html(model)
        self.assertNotIn(injected, document)
        self.assertNotIn("do-not-leak", document)
        self.assertIn("\\u003c/script\\u003e", document)
        self.assertIn("[REDACTED]", document)
        self.assertNotIn('src="https:', document)
        digest = base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
        self.assertIn(digest, document)
        self.assertIn("default-src &#x27;none&#x27;", document)
        self.assertNotIn("innerHTML", SCRIPT)

    def test_failed_billing_still_publishes_estimates_and_strict_keeps_prior_file(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_simulation(working_directory=directory, report_format="html", deny_costs=True)
            output = Path(result["output"])
            self.assertEqual(result["exit_code"], 2, result["stderr"])
            self.assertIn("PARTIAL EXTRACTION", output.read_text())
            model = build_cost_model(result["rows"], generated_at=GENERATED, context=result["context"], partial=True)
            self.assertEqual(model["summary"]["2026-09"]["USD"]["total"], Decimal("297.92"))
            self.assertEqual(model["billing"], [])
            before = output.read_bytes()
            result = run_simulation(working_directory=directory, report_format="html", deny_costs=True, strict=True, overwrite=True)
            self.assertEqual(result["exit_code"], 1)
            self.assertEqual(output.read_bytes(), before)

    def test_publication_failure_keeps_previous_html_and_no_temp_files(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/"report.html"
            output.write_text("old report")
            with self.assertRaises(ValueError):
                write_html_report(output,[resource(list_price_per_hour="NaN")],generated_at=GENERATED,
                                  context=CONTEXT,partial=False,overwrite=True)
            self.assertEqual(output.read_text(), "old report")
            self.assertEqual(list(Path(directory).iterdir()), [output])
            with self.assertRaises(FileExistsError):
                write_html_report(output,[],generated_at=GENERATED,context=CONTEXT,partial=False)

    def test_html_default_and_runtime_validation_happen_before_aws(self):
        args=costcsv.parser().parse_args(["--from","2026-09-01","--to","2026-10-01"])
        self.assertEqual(args.report_format,"html")
        for value in ("NaN","Infinity","-1","25"):
            with patch("costcsv.execute") as execute:
                code=costcsv.main(["--from","2026-09-01","--to","2026-10-01","--estimate-hours-per-day",value])
                self.assertEqual(code,1)
                execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
