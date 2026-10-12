"""Automatic account directories, portable timestamps and collision protection."""
from contextlib import chdir
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import costcsv
from examples import simulate_export


class OutputPathTests(unittest.TestCase):
    def test_filename_uses_generation_timezone_and_requested_services(self):
        generated = "2026-10-11T23:35:00+00:00"
        root = Path.cwd() / "output" / "123456789012"
        for services, zone, expected in [
            ({"ec2"}, "Europe/Rome", "2026-10-12_ec2_0135.html"),
            ({"all"}, "UTC", "2026-10-11_all_2335.html"),
            ({"eks", "rds"}, "UTC", "2026-10-11_multi_2335.html"),
        ]:
            with self.subTest(services=services, zone=zone):
                self.assertEqual(costcsv.html_output_path("123456789012", services, generated, zone), root / expected)
        with self.assertRaises(ValueError):
            costcsv.html_output_path("../outside", {"ec2"}, generated, "UTC")

    def test_offline_export_creates_default_account_directory(self):
        with tempfile.TemporaryDirectory() as temporary, chdir(temporary):
            result = simulate_export.run_simulation(report_format="html", display_timezone="Europe/Rome")
            expected = Path("output/123456789012/2026-10-11_multi_1400.html")
            self.assertEqual(result["exit_code"], 0, result["stderr"])
            self.assertEqual(result["network_attempts"], 0)
            self.assertTrue(expected.is_file())
            self.assertIn(str(expected.absolute()), result["stderr"])
            self.assertEqual(list(Path.cwd().rglob("*.html")), [expected.absolute()])

    def test_same_minute_collision_stops_before_inventory(self):
        with tempfile.TemporaryDirectory() as temporary, chdir(temporary):
            args = costcsv.parser().parse_args(["--mode", "inventory", "--service", "ec2"])
            costcsv.validate(args)
            generated = simulate_export.GENERATED
            destination = costcsv.html_output_path("123456789012", {"ec2"}, generated, "UTC")
            destination.parent.mkdir(parents=True)
            destination.write_text("previous report")
            identity = {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/example"}
            with patch("costcsv.datetime", simulate_export.FrozenDateTime), \
                    patch("costcsv.InventoryContext.call", return_value=identity) as call, \
                    patch("costcsv.discover_regions") as discover:
                with self.assertRaises(FileExistsError):
                    costcsv.execute(args, session=type("Session", (), {"region_name": "eu-west-1"})())
                self.assertEqual(call.call_count, 1)
                discover.assert_not_called()
            self.assertEqual(destination.read_text(), "previous report")
            args.overwrite = True
            costcsv.validate_output(args)

    def test_html_output_override_is_rejected_before_aws(self):
        for path in ("custom.html", "output/123456789012/custom.html"):
            with self.subTest(path=path), patch("costcsv.execute") as execute:
                code = costcsv.main(["--mode", "inventory", "--output", path])
                self.assertEqual(code, 1)
                execute.assert_not_called()
