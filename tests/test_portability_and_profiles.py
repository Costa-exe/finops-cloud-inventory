"""Offline authentication selection and timezone portability checks."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import boto3
import costcsv
from examples.simulate_export import offline_environment
from tests.test_html_report import build, resource
from aws_inventory.html_report import render_html


class PortabilityTests(unittest.TestCase):
    def test_timezone_works_without_system_database(self):
        result = subprocess.run([sys.executable, "-c",
            "from aws_inventory.localization import display_datetime; "
            "print(display_datetime('2026-07-01T00:00:00Z', zone='Europe/Rome'))"],
            env={**os.environ, "PYTHONTZPATH": ""}, capture_output=True, text=True, check=True)
        self.assertIn("02:00 Europe/Rome (+0200)", result.stdout)

    def test_explicit_profile_and_terminal_credentials(self):
        session_class = boto3.Session
        with tempfile.TemporaryDirectory() as temporary, offline_environment() as attempts:
            credentials = Path(temporary) / "credentials"
            credentials.write_text("[company]\naws_access_key_id=FAKE_PROFILE_KEY\naws_secret_access_key=FAKE_PROFILE_SECRET\n", encoding="utf-8")
            with patch.dict(os.environ, {"AWS_SHARED_CREDENTIALS_FILE": str(credentials)}):
                for profile, expected in ((None, "FAKE_ACCESS_KEY_FOR_OFFLINE_TESTS"), ("company", "FAKE_PROFILE_KEY")):
                    captured = []
                    def create_session(**kwargs):
                        session = session_class(**kwargs)
                        captured.append(session.get_credentials().access_key)
                        return session
                    flags = ["--mode", "inventory", "--region", "eu-west-1", "--service", "ec2"]
                    if profile:
                        flags += ["--profile", profile]
                    args = costcsv.parser().parse_args(flags)
                    costcsv.validate(args)
                    with patch("costcsv.boto3.Session", side_effect=create_session), \
                            patch("costcsv.InventoryContext.call", side_effect=RuntimeError("stop before AWS")):
                        with self.assertRaisesRegex(RuntimeError, "stop before AWS"):
                            costcsv.execute(args)
                    self.assertEqual(captured, [expected])
            self.assertEqual(attempts, [])

    def test_configuration_is_not_embedded_in_html(self):
        model = build([resource(details={"internal_metadata": "configuration-marker"})])
        model["resources"][0]["configuration"] = {"sentinel": "configuration-marker"}
        document = render_html(model)
        self.assertNotIn("configuration-marker", document)
        self.assertNotIn("configurationDetails", document)
        self.assertIn('id="export-resources"', document)
        self.assertNotIn('id="export-billing"', document)
        self.assertNotIn("downloadCsv", document)
        self.assertIn('"sentinel"', str(model["resources"][0]["configuration"]).replace("'", '"'))
