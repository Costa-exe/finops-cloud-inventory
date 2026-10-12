"""Source currencies, locale controls and conservative pricing regressions."""
import unittest
import io
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch
from decimal import Decimal

import costcsv
from aws_inventory.html_report import render_html
from aws_inventory.localization import display_date, display_datetime, validate_timezone
from aws_inventory.pricing import catalog_query
from examples.generate_service_guide import build_guide
from tests.test_html_report import resource, quote, build


class PresentationTests(unittest.TestCase):
    def test_source_currency_never_converts_or_relabels_amounts(self):
        row = {"row_type": "cost_summary", "service": "AWS Lambda", "region": "eu-west-1",
               "period_start": "2026-09-01", "period_end": "2026-10-01", "amount": "45.12", "unit": "EUR", "metric": "AmortizedCost"}
        model = build([resource(), row], language="it", date_format="dmy")
        self.assertEqual(model["billing"][0]["amount"], Decimal("45.12"))
        self.assertEqual(model["resources"][0]["months"]["2026-09"]["currency"], "USD")
        self.assertEqual(model["resources"][0]["months"]["2026-09"]["amount"], Decimal("86.40"))
        document = render_html(model)
        self.assertIn('<html lang="it">', document)
        self.assertNotIn('id="currency"', document)
        self.assertIn("non vengono convertiti", document)
        self.assertNotIn("Metodo e copertura", document)
        self.assertNotIn("Inventario, relazioni e costi mensili", document)
        self.assertNotIn('href="#billing"', document)
        # No USD data must still produce a report without inventing USD prices.
        foreign = build([resource(price_currency="EUR"), row])
        document = render_html(foreign)
        fallback = document.split("<noscript>")[1].split("</noscript>")[0]
        self.assertNotIn("86.40", fallback)
        self.assertEqual(foreign["resources"][0]["months"]["2026-09"]["currency"], "EUR")

    def test_timezone_handles_dst_and_previous_day_without_changing_billing(self):
        self.assertEqual(display_datetime("2026-07-01T00:00:00Z", "dmy", "Europe/Rome"),
                         "01/07/2026 02:00 Europe/Rome (+0200)")
        self.assertEqual(display_datetime("2026-01-01T00:00:00Z", "iso", "Europe/Rome"),
                         "2026-01-01 01:00 Europe/Rome (+0100)")
        self.assertEqual(display_datetime("2026-09-01T00:00:00Z", "mdy", "America/New_York"),
                         "08/31/2026 20:00 America/New_York (-0400)")
        utc = build([resource()], timezone="UTC")
        local = build([resource()], timezone="America/New_York")
        self.assertEqual(utc["windows"], local["windows"])
        self.assertEqual(utc["summary"], local["summary"])

    def test_invalid_timezone_fails_before_aws_access(self):
        for zone in ("Invalid/Timezone", "../Europe/Rome", "/etc/passwd"):
            with self.assertRaisesRegex(ValueError, "Unknown timezone"):
                validate_timezone(zone)
        with patch("costcsv.execute") as execute, redirect_stderr(io.StringIO()):
            self.assertEqual(costcsv.main(["--timezone", "Invalid/Timezone"]), 1)
            execute.assert_not_called()

    def test_guided_prompt_selects_timezone_and_retries_invalid_input(self):
        args = costcsv.parser().parse_args(["--from", "2026-09-01", "--to", "2026-10-01",
                                           "--region", "all", "--service", "all"])
        with patch("sys.stdin.isatty", return_value=True), redirect_stdout(io.StringIO()), \
                patch("builtins.input", side_effect=["it", "dmy", "Invalid/Timezone", "Europe/Rome", "24"]) as prompt:
            costcsv.guided(args)
        self.assertEqual(args.timezone, "Europe/Rome")
        self.assertFalse(hasattr(args, "currency"))
        self.assertTrue(any("timezone" in call.args[0] for call in prompt.call_args_list))

    def test_date_display_options_do_not_change_cost_intervals(self):
        for style, expected in (("iso", "2026-09-01"), ("dmy", "01/09/2026"), ("mdy", "09/01/2026")):
            self.assertEqual(display_date("2026-09-01T12:00:00Z", style), expected)
            model = build([resource()], date_format=style)
            self.assertEqual(model["windows"][0]["hours"], 720)
            self.assertEqual(model["resources"][0]["months"]["2026-09"]["amount"], Decimal("86.4"))

    def test_pdf_has_valid_header_and_covers_the_service_catalog(self):
        guide = build_guide()
        self.assertTrue(guide.startswith(b"%PDF-1.4"))
        self.assertTrue(guide.endswith(b"%%EOF\n"))
        for selector in costcsv.SERVICES:
            self.assertIn(("Selettore: --service "+selector).encode("cp1252").hex().encode(), guide)

    def test_unsupported_platform_is_not_silently_priced_as_linux(self):
        row = resource("task", "ecs", "task", details={"launch_type": "FARGATE"}, vcpu=1, memory_mib=2048)
        self.assertIsNone(catalog_query(row))
        self.assertEqual(row["price_status"], "missing_or_unsupported_fargate_platform")
        regional_nat = resource("nat-regional", "vpc", "nat_gateway", details={"AvailabilityMode": "regional"})
        self.assertIsNone(catalog_query(regional_nat))
        self.assertEqual(regional_nat["price_status"], "unsupported_regional_nat_quantity")

    def test_network_hours_do_not_follow_instance_runtime(self):
        row = resource("nat-one", "vpc", "nat_gateway")
        rate = quote("nat-one", "0.045", "Hrs", "vpc")
        model = build([row, rate], estimate_hours_per_day="8")
        self.assertEqual(model["resources"][0]["months"]["2026-09"]["amount"], Decimal("32.4"))
        self.assertFalse(model["resources"][0]["months"]["2026-09"]["complete"])

    def test_alternative_hourly_quotes_are_not_added_as_base_rate(self):
        row = resource("lb-one", "elb", "load_balancer")
        a = quote("lb-one", "0.025", "Hrs", "elb")
        b = {**a, "price_sku": "alternative", "price_per_unit": "0.03"}
        model = build([row, a, b])
        self.assertIsNone(model["resources"][0]["months"]["2026-09"]["amount"])


if __name__ == "__main__":
    unittest.main()
