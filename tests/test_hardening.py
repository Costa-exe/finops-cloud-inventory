"""Offline regressions for export safety, thread isolation, and data integrity."""

from __future__ import annotations

import csv
import io
import json
import os
import stat
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Lock
from types import SimpleNamespace
from unittest.mock import Mock, patch

from aws_inventory.common import InventoryContext, chunks, error_message
from aws_inventory.report import write_report
from aws_inventory.read_operations import OPERATIONS
from examples.simulate_export import offline_environment


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        guard = offline_environment()
        self.network_attempts = guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        self.addCleanup(self.assertEqual, self.network_attempts, [])


class ReportSafetyTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="costcsv-hardening-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "inventory.csv"

    def export(self, rows, *, overwrite=False, excel=True, context=None, path=None):
        write_report(path or self.path, rows, generated_at="2026-10-11T12:00:00+00:00",
                     context=context or {}, partial=False, excel=excel, overwrite=overwrite)

    def read(self, *, excel=True):
        with self.path.open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream, delimiter=";" if excel else ","))

    def test_text_formulas_are_neutralized_without_losing_numeric_credits(self):
        formulas = ["=HYPERLINK(\"https://invalid.example\",\"click\")", "+1+1", "-1+2",
                    "@SUM(A1:A9)", " \t=1+1", "\r=1+1", "\n=1+1"]
        rows = [{"row_type": "inventory", "resource_id": str(index),
                 "resource_name": value, "amount": Decimal("-0.1234567890123456789"),
                 "notes": "Ordinary Unicode: café, 日本語; quoted \"text\"\nand a newline"}
                for index, value in enumerate(formulas)]
        self.export(rows)
        output = self.read()
        self.assertEqual(len(output), len(formulas))
        for index, row in enumerate(output):
            self.assertEqual(row["resource_name"], "'" + formulas[index])
            self.assertEqual(row["amount"], "-0,1234567890123456789")
            self.assertEqual(row["notes"], rows[index]["notes"])
        self.assertTrue(self.path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_redacts_nested_metadata_and_query_secrets_without_mutating_inventory(self):
        rows = [{
            "row_type": "inventory", "resource_id": "i-demo",
            "tags": [{"Key": "Name", "Value": "worker-one"},
                     {"Key": "DB_PASSWORD", "Value": "tag-password-never-export"}],
            "details": {"nested": [{"AWS-Secret-Access-Key": "nested-key-never-export"},
                                     {"key": "api_key", "value": "pair-key-never-export"}],
                        "configuration": {"dbPassword": "db-password-never-export",
                                          "allowed": "retained"}},
            "query_context": {"credentials": {"value": "credentials-never-export"},
                              "filter": {"Tags": {"Key": "db_password",
                                                  "Values": ["filter-secret-never-export"]}},
                              "allowed": "row-context"},
            "client_secret": "extra-secret-never-export",
        }]
        context = {"session_token": "context-token-never-export", "scope": "demo"}
        original, original_context = deepcopy(rows), deepcopy(context)
        self.export(rows, context=context)
        serialized = self.path.read_text(encoding="utf-8-sig")
        self.assertNotIn("never-export", serialized)
        self.assertEqual(rows, original)
        self.assertEqual(context, original_context)
        row = self.read()[0]
        tags, details, query = (json.loads(row[field]) for field in
                                ("tags", "details", "query_context"))
        self.assertEqual(tags[0]["Value"], "worker-one")
        self.assertEqual(tags[1]["Value"], "[REDACTED]")
        self.assertEqual(details["configuration"]["allowed"], "retained")
        self.assertEqual(details["extra_fields"]["client_secret"], "[REDACTED]")
        self.assertEqual(query["allowed"], "row-context")
        self.assertEqual(query["scope"], "demo")
        self.assertEqual(query["filter"]["Tags"]["Values"], "[REDACTED]")

    def test_invalid_numeric_payload_never_replaces_existing_report(self):
        original = b"An earlier completed report must remain intact.\n"
        self.path.write_bytes(original)
        invalid = ["NaN", "sNaN", "Infinity", "-Infinity", "1,25", "=1+1",
                   True, {"amount": "1"}, [1], date(2026, 10, 11)]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises((ValueError, TypeError)):
                    self.export([{"row_type": "inventory", "resource_id": "a", "amount": "1"},
                                 {"row_type": "inventory", "resource_id": "b", "amount": value}],
                                overwrite=True)
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(list(self.directory.iterdir()), [self.path])

    def test_numeric_precision_and_infinite_catalog_end_range_are_preserved(self):
        self.export([{"amount": Decimal("-12.00000000000000000001"),
                      "price_per_unit": Decimal("0.000000000001"),
                      "price_begin_range": "0", "price_end_range": "Inf"}], excel=False)
        row = self.read(excel=False)[0]
        self.assertEqual(row["amount"], "-12.00000000000000000001")
        self.assertEqual(row["price_per_unit"], "0.000000000001")
        self.assertEqual(row["price_end_range"], "Inf")
        with self.assertRaises(ValueError):
            self.export([{"price_begin_range": "Inf"}], overwrite=True)

    def test_existing_destination_is_protected_and_new_reports_are_private(self):
        self.export([{"resource_name": "first report"}])
        original = self.path.read_bytes()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            self.export([{"resource_name": "replacement"}])
        self.assertEqual(self.path.read_bytes(), original)
        self.export([{"resource_name": "authorized replacement"}], overwrite=True)
        self.assertEqual(self.read()[0]["resource_name"], "authorized replacement")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_failed_atomic_publication_preserves_previous_file_and_cleans_temp(self):
        self.path.write_bytes(b"previous completed report\n")
        with patch("aws_inventory.report.os.replace", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                self.export([{"resource_name": "replacement"}], overwrite=True)
        self.assertEqual(self.path.read_bytes(), b"previous completed report\n")
        self.assertEqual(list(self.directory.iterdir()), [self.path])

    def test_simultaneous_creators_publish_one_complete_file_without_overwrite(self):
        ready = Barrier(2)
        actual_link = os.link

        def synchronized_link(source, destination):
            ready.wait(timeout=5)
            actual_link(source, destination)

        def export_named(name):
            try:
                self.export([{"resource_id": str(index), "resource_name": name}
                             for index in range(20)])
                return name
            except FileExistsError:
                return None

        with patch("aws_inventory.report.os.link", side_effect=synchronized_link):
            with ThreadPoolExecutor(max_workers=2) as executor:
                winners = list(executor.map(export_named, ("first writer", "second writer")))
        winner = [name for name in winners if name is not None]
        self.assertEqual(len(winner), 1)
        output = self.read()
        self.assertEqual(len(output), 20)
        self.assertEqual({row["resource_name"] for row in output}, set(winner))
        self.assertEqual(list(self.directory.iterdir()), [self.path])


class ContextSafetyTests(OfflineTestCase):
    @staticmethod
    def context(session=None):
        return InventoryContext(session or Mock(), "123456789012", "aws",
                                "2026-10-11T12:00:00+00:00", {"ec2", "ebs"},
                                {"eu-west-1", "eu-central-1"}, False)

    def test_write_operations_are_rejected_before_a_client_is_created(self):
        session = Mock()
        context = self.context(session)
        for operation in ("delete_volume", "terminate_instances", "put_object", "start_instances"):
            with self.subTest(operation=operation):
                with self.assertRaises(ValueError):
                    context.call("ec2", operation, "eu-west-1")
                with self.assertRaises(ValueError):
                    list(context.pages("ec2", operation, "eu-west-1", "Resources"))
        session.client.assert_not_called()

    def test_region_failures_and_cached_resources_do_not_leak_to_other_workers(self):
        parent = self.context()
        first, second = parent.fork(), parent.fork()
        first.cache["volumes"] = {"vol-first": {"Size": 100}}
        first.emit("ec2", "instance", "i-first", "eu-west-1")
        with redirect_stderr(io.StringIO()):
            first.issue("ec2", "eu-west-1", "describe_volumes", "synthetic denial")
        second.cache["volumes"] = {"vol-second": {"Size": 200}}
        second.emit("ec2", "instance", "i-second", "eu-central-1")
        self.assertEqual(parent.rows, [])
        self.assertEqual(parent.issues, [])
        self.assertEqual(parent.cache, {})
        self.assertEqual(second.issues, [])
        self.assertEqual([row["resource_id"] for row in second.rows], ["i-second"])
        self.assertEqual(set(first.cache["volumes"]), {"vol-first"})
        self.assertEqual(set(second.cache["volumes"]), {"vol-second"})

    def test_shared_client_is_created_once_while_api_calls_can_overlap(self):
        workers = 8
        requests_ready, api_ready = Barrier(workers), Barrier(workers)
        construction_lock = Lock()
        constructions = []

        class Client:
            meta = SimpleNamespace(method_to_api_mapping={op: op for op in OPERATIONS['ec2']},
                                   events=Mock())

            def describe_instances(self):
                api_ready.wait(timeout=5)
                return {"Reservations": []}

        client = Client()

        def build_client(service, region_name, **kwargs):
            with construction_lock:
                constructions.append((service, region_name))
            # Widen the race without using any SDK client or network access.
            time.sleep(0.01)
            return client

        session = Mock()
        session.client.side_effect = build_client
        parent = self.context(session)
        contexts = [parent.fork() for _ in range(workers)]

        def read(context):
            requests_ready.wait(timeout=5)
            return context.call("ec2", "describe_instances", "eu-west-1")

        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(read, contexts))
        self.assertEqual(constructions, [("ec2", "eu-west-1")])
        client.meta.events.register_first.assert_called_once()
        self.assertEqual(results, [{"Reservations": []}] * workers)

    def test_error_text_cannot_emit_terminal_control_sequences(self):
        _, message = error_message(ValueError("resource\x1b[2J\rrewritten\nnext\x7f"))
        self.assertFalse(any(ord(char) < 32 or ord(char) == 127 for char in message))
        self.assertIn("resource", message)
        self.assertIn("rewritten", message)

    def test_chunking_consumes_only_the_requested_batch(self):
        consumed = []

        def source():
            for value in range(10):
                consumed.append(value)
                yield value

        batches = chunks(source(), 3)
        self.assertEqual(next(batches), [0, 1, 2])
        self.assertEqual(consumed, [0, 1, 2])
        self.assertEqual(list(batches), [[3, 4, 5], [6, 7, 8], [9]])


if __name__ == "__main__":
    unittest.main()
