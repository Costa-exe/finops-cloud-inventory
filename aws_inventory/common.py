"""Shared AWS context, pagination, and inventory normalization."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from decimal import Decimal
from itertools import islice
from threading import Lock
from typing import Any, Iterable

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


AWS_ERRORS = (BotoCoreError, ClientError)


def tags_dict(tags: Any) -> dict:
    if isinstance(tags, dict):
        return dict(tags)
    return {item.get("Key", item.get("key", "")): item.get("Value", item.get("value", ""))
            for item in (tags or []) if isinstance(item, dict)}


def resource_name(native_name: str | None, tags: Any, resource_id: str) -> tuple[str, str]:
    if native_name:
        return native_name, "native"
    name = tags_dict(tags).get("Name")
    return (name, "tag:Name") if name else (resource_id, "resource_id")


def chunks(sequence: Iterable, size: int):
    if size <= 0:
        raise ValueError("Chunk size must be positive.")
    iterator = iter(sequence)
    while batch := list(islice(iterator, size)):
        yield batch


def json_default(value: Any):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=json_default)


def error_message(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        code, message = error.get("Code", "ClientError"), error.get("Message", str(exc))
    else:
        code, message = type(exc).__name__, str(exc)
    # AWS error text may contain resource-controlled strings. Escape terminal controls.
    message = "".join(f"\\x{ord(char):02x}" if ord(char) < 32 or ord(char) == 127 else char
                      for char in message)
    return code, message


class InventoryContext:
    def __init__(self, session, account_id: str, partition: str, generated_at: str,
                 selected_services: set[str], regions: set[str], all_regions: bool):
        self.session = session
        self.account_id = account_id
        self.partition = partition
        self.generated_at = generated_at
        self.selected_services = selected_services
        self.regions = regions
        self.all_regions = all_regions
        self.cache: dict = {}
        self.rows: list[dict] = []
        self.issues: list[dict] = []
        self.clients: dict = {}
        self._client_lock = Lock()
        self.config = Config(retries={"mode": "standard", "total_max_attempts": 10},
                             connect_timeout=10, read_timeout=60,
                             max_pool_connections=16, user_agent_extra="costcsv/1.1")

    def fork(self):
        """Isolate a region's rows/cache while safely sharing SDK client creation."""
        child = InventoryContext(self.session, self.account_id, self.partition, self.generated_at,
                                 self.selected_services, self.regions, self.all_regions)
        child.clients = self.clients
        child._client_lock = self._client_lock
        child.config = self.config
        child.requested_services = getattr(self, "requested_services", self.selected_services)
        return child

    def client(self, service: str, region: str):
        key = (service, region)
        # Sessions are not thread-safe: serialize client creation, not API I/O.
        with self._client_lock:
            if key not in self.clients:
                self.clients[key] = self.session.client(service, region_name=region,
                                                        config=self.config)
            return self.clients[key]

    @staticmethod
    def _check_read_operation(operation: str):
        if not operation.startswith(("get_", "list_", "describe_")):
            raise ValueError(f"Only read operations are supported: {operation}")

    def call(self, service: str, operation: str, region: str, **kwargs) -> dict:
        self._check_read_operation(operation)
        return getattr(self.client(service, region), operation)(**kwargs)

    def pages(self, service: str, operation: str, region: str, result_key: str, **kwargs):
        self._check_read_operation(operation)
        client = self.client(service, region)
        if client.can_paginate(operation):
            responses = client.get_paginator(operation).paginate(**kwargs)
        else:
            responses = self._manual_pages(client, operation, kwargs)
        for response in responses:
            values = response
            for part in result_key.split("."):
                values = values.get(part, {}) if isinstance(values, dict) else {}
            if isinstance(values, list):
                yield from values
            elif values:
                raise ValueError(f"Unexpected response: {service}.{operation}.{result_key}")

    @staticmethod
    def _manual_pages(client, operation: str, kwargs: dict):
        """Fallback for operations without a paginator; never discard unknown tokens."""
        parameters = dict(kwargs)
        seen = set()
        model = client.meta.service_model.operation_model(
            client.meta.method_to_api_mapping[operation])
        inputs = model.input_shape.members if model.input_shape else {}
        token_pairs = (("NextToken", "NextToken"), ("nextToken", "nextToken"),
                       ("NextMarker", "Marker"), ("Marker", "Marker"),
                       ("NextContinuationToken", "ContinuationToken"),
                       ("ContinuationToken", "ContinuationToken"),
                       ("LastEvaluatedTableName", "ExclusiveStartTableName"))
        while True:
            response = getattr(client, operation)(**parameters)
            yield response
            continuation = next(((out, inp, response[out]) for out, inp in token_pairs
                                 if response.get(out)), None)
            if not continuation:
                if response.get("IsTruncated") or response.get("Truncated"):
                    raise ValueError(f"Unsupported pagination: {operation}")
                return
            _, input_key, token = continuation
            if input_key not in inputs or str(token) in seen:
                raise ValueError(f"Unexpected or repeated pagination token: {operation}")
            seen.add(str(token))
            parameters[input_key] = token

    def safe_call(self, service: str, operation: str, region: str, **kwargs):
        try:
            return self.call(service, operation, region, **kwargs)
        except AWS_ERRORS as exc:
            code, message = error_message(exc)
            self.issue(service, region, operation, message, code=code)
            return None

    def safe_pages(self, service: str, operation: str, region: str, result_key: str,
                   **kwargs) -> list:
        values = []
        try:
            for value in self.pages(service, operation, region, result_key, **kwargs):
                values.append(value)
        except (*AWS_ERRORS, ValueError) as exc:
            code, message = error_message(exc)
            self.issue(service, region, operation, message, code=code)
        return values

    def emit(self, service: str, resource_type: str, resource_id: str, region: str,
             **fields) -> dict:
        row = {"row_type": "inventory", "account_id": self.account_id,
               "service": service, "resource_type": resource_type,
               "resource_id": resource_id, "region": region, **fields}
        if not row.get("resource_name"):
            row["resource_name"], row["name_source"] = resource_name(
                None, row.get("tags"), resource_id)
        row.setdefault("cost_scope", "current_inventory_no_historical_cost_allocation")
        self.rows.append(row)
        return row

    def issue(self, service: str, region: str, operation: str, message: str,
              resource_id: str = "", code: str = ""):
        row = {"row_type": "error", "account_id": self.account_id,
               "service": service, "region": region, "resource_id": resource_id,
               "operation": operation, "error_code": code, "notes": message}
        self.issues.append(row)
        print(f"  Warning {service}/{region} {operation}: {code} {message}",
              file=sys.stderr)
