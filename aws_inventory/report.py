"""Fixed-schema CSV, exact decimal amounts, redaction, and atomic publication."""

from __future__ import annotations

import csv
import os
import tempfile
from collections import OrderedDict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .common import compact_json


FIELDS = (
    "schema_version", "generated_at_utc", "report_status", "row_type", "account_id",
    "region", "service", "resource_type", "resource_id", "resource_arn",
    "resource_name", "name_source", "state", "availability_zone", "instance_type",
    "engine", "engine_version", "cluster_name", "parent_id", "related_resource_ids",
    "relationship_source", "vpc_id", "subnet_ids", "private_ips", "public_ips",
    "security_group_ids", "vcpu", "memory_mib", "storage_gib", "storage_bytes",
    "storage_type", "iops", "throughput_mib_s", "encrypted", "multi_az", "attached_to",
    "device_names", "created_at", "period_start", "period_end", "period_hours",
    "group_1_key", "group_1_value", "group_2_key", "group_2_value",
    "cost_scope", "metric", "amount", "unit", "estimated", "average_cost_per_hour",
    "hourly_cost_basis", "price_source", "price_status", "price_term",
    "price_checked_at_utc", "price_effective_date", "price_sku", "price_description",
    "price_per_unit", "price_unit", "price_currency", "price_begin_range",
    "price_end_range", "list_price_per_hour", "price_quantity", "price_component",
    "operation", "error_code", "resource_count", "tags", "details", "notes",
    "query_context",
)
FIELD_SET = frozenset(FIELDS)
CONTEXT_CACHE_SIZE = 128

# Supplement the collectors' explicit field allowlists. This cannot discover secrets
# embedded in arbitrary descriptions; keep credentials out of resource metadata.
SENSITIVE_KEYS = frozenset({
    "password", "passwd", "secret", "secretaccesskey", "accesskeyid", "awsaccesskeyid",
    "awssecretaccesskey", "sessiontoken", "awssessiontoken", "token", "accesstoken",
    "refreshtoken", "apikey", "privatekey", "clientsecret", "authorization",
    "credentials", "credential", "connectionstring", "databasepassword", "dbpassword",
})


def redact_sensitive(value):
    if isinstance(value, dict):
        result = {}
        # AWS often represents metadata as {Key: ..., Value: ...} rather than a map.
        tag_key = value.get("Key", value.get("key", ""))
        secret_tag = _sensitive_key(tag_key)
        for key, item in value.items():
            if _sensitive_key(key) or (secret_tag and key in {"Value", "value", "Values", "values"}):
                result[key] = "[REDACTED]"
            else:
                result[key] = redact_sensitive(item)
        return result
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item) for item in value]
    return value


def _sensitive_key(key) -> bool:
    normalized = "".join(char for char in str(key).lower() if char.isalnum())
    return normalized in SENSITIVE_KEYS or normalized.endswith(("password", "secret", "token"))

NUMERIC_FIELDS = {
    "vcpu", "memory_mib", "storage_gib", "storage_bytes", "iops", "throughput_mib_s",
    "amount", "average_cost_per_hour", "period_hours", "price_per_unit",
    "list_price_per_hour", "price_quantity", "resource_count", "price_begin_range",
    "price_end_range",
}


def cell(value, key: str, excel: bool):
    if value is None:
        return ""
    if key in NUMERIC_FIELDS:
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError(f"Invalid numeric CSV type for {key}: {type(value).__name__}.")
        value = format(value, "f") if isinstance(value, Decimal) else str(value)
        if not value:
            return ""
        if key == "price_end_range" and value.lower() in {"inf", "infinity"}:
            return value
        try:
            if not Decimal(value).is_finite():
                raise ValueError(f"Non-finite numeric CSV value for {key}.")
        except InvalidOperation as exc:
            raise ValueError(f"Invalid numeric CSV value for {key}.") from exc
        return value.replace(".", ",") if excel else value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple, set)):
        return compact_json(redact_sensitive(value))
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        value = format(value, "f")
    value = str(value)
    # Preserve negative numeric values while neutralizing spreadsheet formulas in text.
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def write_report(path: Path, rows: list[dict], *, generated_at: str, context: dict,
                 partial: bool, excel: bool, overwrite: bool):
    path = path.expanduser().absolute()
    if path.exists() and not overwrite:
        raise FileExistsError(f"File already exists: {path}. Use --overwrite to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig" if excel else "utf-8",
                                         newline="", prefix=f".{path.name}.", suffix=".tmp",
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.writer(stream, delimiter=";" if excel else ",", lineterminator="\n")
            writer.writerow(FIELDS)
            # Most rows share a query. Serialize it once instead of once per resource.
            context_cache = OrderedDict()
            order = {"report": 0, "coverage": 1, "cost_summary": 2, "inventory": 3,
                     "resource_cost": 4, "price_quote": 5, "error": 6}
            sorted_rows = sorted(rows, key=lambda row: (
                order.get(row.get("row_type"), 9), str(row.get("region", "")),
                str(row.get("service", "")), str(row.get("resource_type", "")),
                str(row.get("resource_id", "")), str(row.get("period_start", "")),
                str(row.get("group_1_value", "")), str(row.get("group_2_value", "")),
                str(row.get("price_sku", "")), str(row.get("price_description", "")),
                str(row.get("price_begin_range", "")), str(row.get("operation", ""))))
            for raw in sorted_rows:
                row = dict(raw)
                # Retain service-specific fields instead of silently dropping data.
                extra = {key: row.pop(key) for key in list(row) if key not in FIELD_SET}
                if extra:
                    details = row.get("details") or {}
                    row["details"] = {**(details if isinstance(details, dict)
                                           else {"data": details}), "extra_fields": extra}
                row.update(schema_version="1.0", generated_at_utc=generated_at,
                           report_status="partial" if partial else "complete")
                extra_context = raw.get("query_context")
                context_key = id(extra_context)
                if context_key not in context_cache:
                    context_cache[context_key] = compact_json(
                        redact_sensitive({**context, **(extra_context or {})}))
                    if len(context_cache) > CONTEXT_CACHE_SIZE:
                        context_cache.popitem(last=False)
                else:
                    context_cache.move_to_end(context_key)
                row["query_context"] = context_cache[context_key]
                writer.writerow([cell(row.get(key), key, excel) for key in FIELDS])
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            # Atomic create-if-absent also protects against concurrent writers.
            os.link(temporary, path)
            temporary.unlink()
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
