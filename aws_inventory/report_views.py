"""Readable service reports with exact monthly billing and current inventory.

This module performs no I/O and makes no AWS requests. Billing views overlap;
only the original account cost_summary rows contribute to overview totals.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext

from .costs import EC2_OTHER, SERVICE_FILTERS, _period_hours


@dataclass(frozen=True)
class Column:
    key: str
    title: str
    kind: str = "text"


@dataclass
class Table:
    name: str
    columns: tuple[Column, ...]
    rows: list[dict]
    note: str = ""


@dataclass
class ServiceReport:
    key: str
    title: str
    tables: list[Table]


SERVICE_TITLES = {
    "ec2": "Amazon EC2 Compute", "ec2_other": "EC2 - Other",
    "ebs": "Amazon EBS", "eks": "Amazon EKS", "ecs": "Amazon ECS",
    "elb": "Elastic Load Balancing", "vpc": "Amazon VPC", "rds": "Amazon RDS",
    "s3": "Amazon S3", "lambda": "AWS Lambda", "dynamodb": "Amazon DynamoDB",
    "efs": "Amazon EFS", "elasticache": "Amazon ElastiCache",
    "opensearch": "Amazon OpenSearch", "redshift": "Amazon Redshift",
    "sqs": "Amazon SQS", "sns": "Amazon SNS", "cloudwatch": "Amazon CloudWatch",
    "cloudfront": "Amazon CloudFront", "route53": "Amazon Route 53",
    "account_costs": "Costs without a service dimension",
}
BILLING_KEYS = {name: key for key, expression in SERVICE_FILTERS.items()
                for name in expression["Dimensions"]["Values"]}
BILLING_KEYS[EC2_OTHER] = "ec2_other"

TITLES = {
    "region": "Region", "service": "Billing service", "resource_type": "Resource type",
    "resource_name": "Name", "resource_id": "Resource ID", "resource_arn": "ARN",
    "name_source": "Name source", "state": "State", "availability_zone": "Zone",
    "instance_type": "Instance type", "cluster_name": "Cluster", "parent_id": "Parent ID",
    "relationship_source": "Relationship source", "related_resource_ids": "Related resources",
    "vcpu": "vCPUs", "memory_mib": "Memory (MiB)", "storage_gib": "Storage (GiB)",
    "storage_bytes": "Storage (bytes)", "storage_type": "Storage type",
    "iops": "IOPS", "throughput_mib_s": "Throughput (MiB/s)", "encrypted": "Encrypted",
    "vpc_id": "VPC", "subnet_ids": "Subnets", "private_ips": "Private IPs",
    "public_ips": "Public IPs", "security_group_ids": "Security groups",
    "attached_to": "Attached to", "device_names": "Devices", "created_at": "Created (UTC)",
    "engine": "Engine", "engine_version": "Version", "multi_az": "Multi-AZ",
    "list_price_per_hour": "Current On-Demand / hour", "price_currency": "Price currency",
    "price_status": "Price status", "metric": "Cost metric", "unit": "Currency",
    "total": "Period total", "status": "Data status", "notes": "Notes",
    "instances": "Instances", "running": "Running", "operation": "Operation",
    "resource_count": "Rows collected", "error_code": "Error code",
    "month": "Month", "period_start": "Period start (inclusive)",
    "period_end": "Period end (exclusive)", "period_hours": "Calendar hours",
    "amount": "Billed amount", "average_cost_per_hour": "Billed average / hour",
}
NUMBER_KEYS = frozenset({"vcpu", "memory_mib", "storage_gib", "storage_bytes", "iops",
                         "throughput_mib_s", "instances", "running", "resource_count", "period_hours"})
DATE_KEYS = frozenset({"created_at", "period_start", "period_end", "price_checked_at_utc",
                       "price_effective_date"})

SNAPSHOT_NOTE = (
    "Current AWS inventory snapshot. Names, sizes, states and relationships are not historical. "
    "A name falls back to the resource ID when AWS provides no native name or Name tag."
)
BILLING_NOTE = (
    "Actual Cost Explorer amounts, grouped by calendar month. Blank means no returned amount, "
    "not a confirmed zero. Currency and metric are kept separate; credits remain negative. "
    "Partial first/last months cover only the requested dates; end dates are exclusive. "
    "Other sheets repeat views of these costs and must not be added together."
)
RESOURCE_COST_NOTE = (
    "Only AWS resource-level EC2 Compute billing is shown. Blank costs are unavailable and "
    "are never allocated from a regional total or inferred from current catalog rates. "
    "Resource cost data requires AWS opt-in and is limited to the available recent period; "
    "a monthly cell contains only returned days. Current names may differ from historical names. "
    "This view excludes EBS and networking and overlaps regional/type costs."
)
EBS_NOTE = (
    "EBS billing is a usage-type subset of EC2 - Other, repeated here for convenience. "
    "It is already included in the overview and must not be added to EC2 - Other totals. "
    "Volume IDs and current sizes do not establish historical cost per volume."
)


def _column(key: str, title: str | None = None, kind: str | None = None) -> Column:
    if kind is None:
        kind = ("number" if key in NUMBER_KEYS else "date" if key in DATE_KEYS
                else "rate" if key in {"list_price_per_hour", "price_per_unit", "average_cost_per_hour"}
                else "money" if key in {"total", "amount"} else "text")
    return Column(key, title or TITLES.get(key, key.replace("_", " ").title()), kind)


def _plain(value):
    """Readable scalar cells; structured attributes have their own long-form sheet."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return "; ".join(f"{key}: {_plain(item)}" for key, item in sorted(value.items()))
    if isinstance(value, (list, tuple, set)):
        values = sorted(value, key=str) if isinstance(value, set) else value
        return "; ".join(str(_plain(item)) for item in values if item is not None)
    return value


def _table(name: str, keys, rows: list[dict], note: str = "", *, titles=None) -> Table:
    columns = tuple(key if isinstance(key, Column) else _column(key, (titles or {}).get(key))
                    for key in keys)
    projected = [{column.key: _plain(row.get(column.key)) for column in columns} for row in rows]
    return Table(name, columns, projected, note)


def _service_key(name: str) -> str:
    if name in SERVICE_TITLES:
        return name
    if name in BILLING_KEYS:
        return BILLING_KEYS[name]
    if not name:
        return "account_costs"
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:42] or "service"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]
    return f"billing_{slug}_{digest}"


def _datetime(value: str) -> datetime:
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def _month(row: dict) -> str:
    start = _datetime(row["period_start"])
    end = _datetime(row["period_end"])
    if end <= start:
        raise ValueError("A billing interval must have a positive duration.")
    if start.strftime("%Y-%m") != (end - timedelta(microseconds=1)).strftime("%Y-%m"):
        raise ValueError("A billing interval spans multiple months; query monthly or finer data.")
    return start.strftime("%Y-%m")


def _months(rows: list[dict], context: dict) -> list[str]:
    months = {_month(row) for row in rows
              if row.get("row_type") in {"cost_summary", "cost_breakdown", "resource_cost"}}
    start = context.get("period_start") or context.get("cost_period_start")
    end = context.get("period_end") or context.get("cost_period_end_exclusive")
    if start and end:
        first, last = _datetime(start), _datetime(end)
        if first >= last:
            raise ValueError("The report start must precede its exclusive end.")
        cursor = date(first.year, first.month, 1)
        count = 0
        while datetime.combine(cursor, datetime.min.time(), timezone.utc) < last:
            months.add(cursor.strftime("%Y-%m"))
            cursor = date(cursor.year + cursor.month // 12, cursor.month % 12 + 1, 1)
            count += 1
            if count > 1200:
                raise ValueError("The report period exceeds 1,200 months.")
    return sorted(months)


def _decimal(value) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("A billed amount must be numeric.")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("A billed amount must be numeric.") from exc
    if not amount.is_finite():
        raise ValueError("A billed amount must be finite.")
    return amount


def _add(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = max(38, left.adjusted(), right.adjusted()) - min(
            0, left.as_tuple().exponent, right.as_tuple().exponent) + 2
        return left + right


def _dimension(row: dict, name: str, fallback: str = ""):
    for index in (1, 2):
        if row.get(f"group_{index}_key") == name:
            return row.get(f"group_{index}_value") or fallback
    return fallback


def _pivot(name: str, rows: list[dict], dimensions: tuple[str, ...], months: list[str],
           *, note: str = BILLING_NOTE, empty: list[dict] | None = None) -> Table:
    groups = {}
    for source in rows:
        key = tuple(str(_plain(source.get(field)) or "") for field in (*dimensions, "unit", "metric"))
        target = groups.setdefault(key, {**dict(zip((*dimensions, "unit", "metric"), key)),
                                         "total": Decimal(0), "status": "Reported by AWS"})
        month = _month(source)
        amount = _decimal(source["amount"])
        target[month] = _add(target.get(month, Decimal(0)), amount)
        target["total"] = _add(target["total"], amount)
        target["_estimated"] = target.get("_estimated", False) or bool(source.get("estimated"))
        if source.get("status"):
            target["status"] = source["status"]
    for source in empty or []:
        key = tuple(str(_plain(source.get(field)) or "") for field in (*dimensions, "unit", "metric"))
        if key not in groups:
            groups[key] = {**dict(zip((*dimensions, "unit", "metric"), key)),
                           "total": None, "status": source.get("status", "Unavailable")}
    columns = [_column(key) for key in dimensions]
    columns.extend((_column("unit"), _column("metric")))
    columns.extend(Column(month, month, "money") for month in months)
    columns.extend((_column("total"), _column("status")))
    for target in groups.values():
        if target.get("_estimated"):
            target["status"] += "; includes AWS estimated amounts"
    return _table(name, columns, [groups[key] for key in sorted(groups)], note)


def _details(row: dict) -> dict:
    return row.get("details") if isinstance(row.get("details"), dict) else {}


def _monthly_cost_details(rows: list[dict], *, overview: bool = False, extra_note: str = "") -> Table:
    """Keep original intervals; never add hourly averages across billing groups."""
    values = []
    for source in rows:
        amount = _decimal(source["amount"])
        hours = _period_hours(source["period_start"], source["period_end"])
        with localcontext() as decimal_context:
            decimal_context.prec = max(38, len(amount.as_tuple().digits) + 8)
            average = amount / hours
        values.append({**source, "month": _month(source), "amount": amount,
                       "period_hours": hours, "average_cost_per_hour": average,
                       "status": "AWS estimated amount" if source.get("estimated") else "Reported by AWS"})
    keys = ("month", "service", "region", "unit", "metric", "period_start", "period_end",
            "period_hours", "amount", "average_cost_per_hour", "status")
    if not overview:
        keys = tuple(key for key in keys if key != "service")
    values.sort(key=lambda row: tuple(str(row.get(key, "")) for key in
                                     ("month", "service", "region", "unit", "metric", "period_start")))
    return _table("Monthly cost details", keys, values,
                  "Historical billed amount divided by calendar hours in the exact returned interval, "
                  "not an instance operating-hour price or current catalog rate. Partial months use only "
                  "their returned dates; end dates are exclusive. Each original billing interval remains "
                  "separate, including daily/hourly intervals if supplied. Do not sum hourly averages. "
                  "Amounts repeat the monthly pivot totals. " + extra_note)


def _as_list(value) -> list:
    return list(value) if isinstance(value, (list, tuple, set)) else [value] if value else []


def _flatten(value, prefix=""):
    """Flatten details without JSON blobs, preserving individual scalar values."""
    if isinstance(value, dict):
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            yield from _flatten(item, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(value, (list, tuple, set)):
        values = sorted(value, key=str) if isinstance(value, set) else value
        for index, item in enumerate(values):
            yield from _flatten(item, f"{prefix}[{index}]")
    elif value is not None:
        yield prefix, _plain(value)


def _metadata_tables(inventory: list[dict]) -> list[Table]:
    attributes, tags = [], []
    for row in inventory:
        identity = {key: row.get(key) for key in ("region", "resource_type", "resource_name", "resource_id")}
        for key, value in _flatten(_details(row)):
            attributes.append({**identity, "attribute": key, "value": value})
        # Compact main sheets intentionally select columns. Retain all other SDK
        # metadata here too, so dates, ARNs, networking and price status are not
        # lost when a service has a custom inventory layout.
        identity_keys = {"row_type", "account_id", "region", "service", "resource_type",
                         "resource_id", "resource_name", "details", "tags", "query_context"}
        for key, value in row.items():
            if key not in identity_keys:
                for attribute, scalar in _flatten(value, f"resource.{key}"):
                    attributes.append({**identity, "attribute": attribute, "value": scalar})
        for key, value in sorted((row.get("tags") or {}).items()):
            tags.append({**identity, "tag": key, "value": _plain(value)})
    result = []
    base = ("region", "resource_type", "resource_name", "resource_id")
    if tags:
        result.append(_table("Tags", (*base, "tag", "value"), tags,
                             "Current resource tags; common sensitive keys are redacted."))
    if attributes:
        result.append(_table("Attributes", (*base, "attribute", "value"), attributes,
                             "Additional current SDK attributes, one scalar value per row. "
                             "Common sensitive fields are redacted."))
    return result


def _control_tables(rows: list[dict], *, context: dict, generated_at: str,
                    partial: bool, overview: bool = False) -> list[Table]:
    result = []
    coverage = [row for row in rows if row.get("row_type") == "coverage"]
    errors = [row for row in rows if row.get("row_type") == "error"]
    if coverage or overview:
        result.append(_table("Coverage", ("service", "region", "state", "resource_count", "notes"),
                             coverage, "A successful empty inventory does not imply zero historical costs."))
    if errors or overview:
        result.append(_table("Read errors", ("service", "region", "resource_id", "operation", "error_code", "notes"),
                             errors, "Failed reads make the report partial. Missing records are not zero values."))
    if overview:
        parameters = {"generated_at_utc": generated_at,
                      "report_status": "partial" if partial else "complete", **context}
        values = [{"parameter": key, "value": value} for key, value in _flatten(parameters)]
        result.append(_table("Parameters", ("parameter", "value"), values,
                             "Exact requested dates and options. The period end is exclusive."))
    return result


def _price_table(rows: list[dict]) -> Table:
    return _table("Current prices", (
        "region", "resource_type", "resource_name", "resource_id", "price_component",
        _column("price_per_unit", "Catalog rate", "rate"), "price_unit", "price_currency",
        "price_term", "price_begin_range", "price_end_range", "price_description",
        "price_effective_date", "price_checked_at_utc", "price_status", "notes"), rows,
        "Current public catalog alternatives, not historical billed costs. Units remain unchanged "
        "(for example GB-Mo is not an hourly amount). EKS standard/extended alternatives are regional, "
        "not assigned to a cluster. Spot comparisons may use On-Demand rates; see Notes. "
        "Do not sum alternative tiers, units, currencies or duplicate resource configurations.")


def _info(title: str, *, generated_at: str, context: dict, partial: bool,
          inventory_count: int, extra: str = "") -> Table:
    entries = [("Report", title), ("Account", context.get("caller_account_id", "")),
               ("Generated (UTC)", generated_at), ("Status", "Partial" if partial else "Complete"),
               ("Period start (inclusive)", context.get("period_start", "")),
               ("Period end (exclusive)", context.get("period_end", "")),
               ("Current inventory rows", inventory_count), ("Inventory basis", SNAPSHOT_NOTE),
               ("Billing basis", BILLING_NOTE),
               ("Prices", "Current public catalog rates; not the historical price paid."),
               ("Scope", extra)]
    if context.get("simulation_only"):
        entries.insert(0, ("OFFLINE SIMULATION", "Fabricated resources, costs and prices; no real AWS account."))
    return _table("Read me", ("item", "value"),
                  [{"item": key, "value": value} for key, value in entries if value != ""],
                  "Start here. Monthly costs, current inventory and current catalog rates have different time bases.")


def _inventory_table(name: str, inventory: list[dict], keys: tuple[str, ...], note=SNAPSHOT_NOTE,
                     *, titles=None) -> Table:
    return _table(name, keys, sorted(inventory, key=lambda row: (
        str(row.get("region", "")), str(row.get("resource_name", "")), str(row.get("resource_id", "")))),
        note, titles=titles)


INSTANCE_KEYS = ("region", "resource_name", "resource_id", "name_source", "instance_type", "state",
                 "availability_zone", "vcpu", "memory_mib", "storage_gib", "cluster_name",
                 "list_price_per_hour", "price_currency", "price_status")
VOLUME_KEYS = ("region", "resource_name", "resource_id", "state", "availability_zone", "storage_type",
               "storage_gib", "iops", "throughput_mib_s", "encrypted", "attached_to", "device_names",
               "cluster_name")


def _ec2_resource_id(identifier: str, region: str, account_id: str) -> str:
    """Normalize EC2 instance ARNs only; unrelated slash suffixes are not IDs."""
    parts = identifier.split(":", 5)
    if (len(parts) != 6 or parts[0] != "arn" or parts[2] != "ec2"
            or (region and parts[3] != region) or (account_id and parts[4] != account_id)):
        return identifier
    match = re.fullmatch(r"instance/(i-[a-zA-Z0-9-]+)", parts[5])
    return match.group(1) if match else identifier


def _resource_monthly(inventory: list[dict], costs: list[dict], months: list[str], context: dict,
                      *, name: str = "Monthly by instance", note: str = RESOURCE_COST_NOTE) -> Table:
    identities = {(str(row.get("region", "")), str(row.get("resource_id", ""))): row for row in inventory}
    normalized = []
    seen = set()
    for source in costs:
        identifier, region = str(source.get("resource_id", "")), str(source.get("region", ""))
        identifier = _ec2_resource_id(identifier, region, str(context.get("caller_account_id") or ""))
        seen.add((region, identifier))
        current = identities.get((region, identifier), {})
        name_value = current.get("resource_name") or source.get("resource_name")
        normalized.append({**source, "resource_id": identifier, "resource_name": name_value or (
            "Not in current inventory" if identifier else "Unallocated by AWS"),
            "status": "AWS resource billing; returned days only"})
    metrics = sorted({(str(row.get("unit", "")), str(row.get("metric", ""))) for row in costs})
    if not metrics:
        metrics = [("", str(context.get("metric") or context.get("cost_metric") or ""))]
    unavailable = []
    for key, row in sorted(identities.items()):
        if key not in seen:
            for currency, metric in metrics:
                unavailable.append({**row, "unit": currency, "metric": metric,
                                    "status": "Unavailable: no resource-level billing returned"})
    return _pivot(name, normalized, ("region", "resource_name", "resource_id"), months,
                  note=note, empty=unavailable)


def _attached_disks(instances: list[dict], volumes: list[dict]) -> list[dict]:
    output, seen = [], set()
    instance_index = {(row.get("region"), row.get("resource_id")): row for row in instances}
    for instance in instances:
        for block in _details(instance).get("block_devices", []):
            key = (instance.get("region"), instance.get("resource_id"), block.get("volume_id"), block.get("device_name"))
            if key in seen:
                continue
            seen.add(key)
            output.append({"region": key[0], "resource_name": instance.get("resource_name"),
                           "resource_id": key[1], "volume_id": key[2], "device": key[3],
                           "storage_gib": block.get("size_gib"), "storage_type": block.get("type"),
                           "encrypted": block.get("encrypted"), "iops": block.get("iops"),
                           "throughput_mib_s": block.get("throughput_mib_s"),
                           "state": block.get("attachment_status"),
                           "delete_on_termination": block.get("delete_on_termination")})
    for volume in volumes:
        for attachment in _details(volume).get("attachments", []):
            key = (volume.get("region"), attachment.get("InstanceId"), volume.get("resource_id"), attachment.get("Device"))
            if key in seen:
                continue
            seen.add(key)
            instance = instance_index.get(key[:2], {})
            output.append({**volume, "resource_name": instance.get("resource_name", ""),
                           "resource_id": key[1], "volume_id": key[2], "device": key[3],
                           "state": attachment.get("State"),
                           "delete_on_termination": attachment.get("DeleteOnTermination")})
    return sorted(output, key=lambda row: tuple(str(row.get(key, "")) for key in
                  ("region", "resource_id", "volume_id", "device")))


def _ec2_tables(inventory: list[dict], all_inventory: list[dict], service_rows: list[dict],
                months: list[str], context: dict) -> list[Table]:
    instances = [row for row in inventory if row.get("resource_type") == "instance"]
    type_costs = [{**row, "instance_type": _dimension(row, "INSTANCE_TYPE", "Unspecified by AWS")}
                  for row in service_rows if row.get("row_type") == "cost_breakdown"
                  and row.get("cost_scope") == "ec2_instance_type"]
    resources = [row for row in service_rows if row.get("row_type") == "resource_cost"]
    tables = [_pivot("Monthly by instance type", type_costs, ("region", "instance_type"), months,
                     note=BILLING_NOTE + " EC2 Compute only; AWS billing types are not inferred from current instances."),
              _resource_monthly(instances, resources, months, context),
              _inventory_table("Instances", instances, INSTANCE_KEYS)]
    networking = ("region", "resource_name", "resource_id", "vpc_id", "subnet_ids", "private_ips",
                  "public_ips", "security_group_ids", "parent_id", "created_at")
    tables.append(_inventory_table("Instance networking", instances, networking))
    groups = defaultdict(list)
    for row in instances:
        groups[(str(row.get("region", "")), str(row.get("instance_type", "")))].append(row)
    summaries = []
    for (region, kind), members in sorted(groups.items()):
        entry = {"region": region, "instance_type": kind, "instances": len(members),
                 "running": sum(row.get("state") == "running" for row in members)}
        for key in ("vcpu", "memory_mib", "storage_gib"):
            entry[key] = (sum((_decimal(row[key]) for row in members), Decimal(0))
                          if all(row.get(key) is not None for row in members) else None)
        summaries.append(entry)
    tables.append(_table("Instance type inventory", ("region", "instance_type", "instances", "running",
                                                    "vcpu", "memory_mib", "storage_gib"), summaries,
                         "Current capacity grouped by type, not historical billed usage. Missing capacity stays blank."))
    volumes = [row for row in all_inventory if row.get("service") == "ebs" and row.get("resource_type") == "volume"]
    tables.append(_table("Attached disks", ("region", "resource_name", "resource_id", "volume_id", "device",
                                            "storage_gib", "storage_type", "encrypted", "iops", "throughput_mib_s",
                                            "state", "delete_on_termination"),
                         _attached_disks(instances, volumes), SNAPSHOT_NOTE,
                         titles={"resource_name": "Instance name", "resource_id": "Instance ID"}))
    scaling = []
    for row in inventory:
        if row.get("resource_type") == "auto_scaling_group":
            details = _details(row)
            scaling.append({**row, "minimum": details.get("MinSize"), "maximum": details.get("MaxSize"),
                            "desired": details.get("DesiredCapacity"),
                            "instances": len(_as_list(row.get("related_resource_ids")))})
    tables.append(_table("Auto scaling", ("region", "resource_name", "cluster_name", "instances",
                                          Column("minimum", "Minimum", "number"),
                                          Column("maximum", "Maximum", "number"),
                                          Column("desired", "Desired capacity", "number"),
                                          "related_resource_ids", "subnet_ids"), scaling, SNAPSHOT_NOTE))
    return tables


def _eks_tables(inventory: list[dict], all_inventory: list[dict], months: list[str], context: dict) -> list[Table]:
    clusters = [row for row in inventory if row.get("resource_type") == "cluster"]
    cluster_keys = {(row.get("region"), row.get("resource_name")) for row in clusters}
    nodes = []
    for row in all_inventory:
        if row.get("service") != "ec2" or row.get("resource_type") != "instance":
            continue
        links = _details(row).get("cluster_links", [])
        for cluster in _as_list(row.get("cluster_name")):
            if (row.get("region"), cluster) not in cluster_keys:
                continue
            sources = [link.get("source", "") for link in links if link.get("cluster_name") == cluster]
            nodegroups = [link.get("parent_id", "") for link in links if link.get("cluster_name") == cluster]
            nodes.append({**row, "cluster_name": cluster, "nodegroup": sorted(set(filter(None, nodegroups))),
                          "relationship_source": sorted(set(filter(None, sources))) or row.get("relationship_source")})
    volumes = []
    for row in all_inventory:
        if row.get("service") == "ebs" and row.get("resource_type") == "volume":
            for cluster in _as_list(row.get("cluster_name")):
                if (row.get("region"), cluster) in cluster_keys:
                    volumes.append({**row, "cluster_name": cluster})
    nodegroups = []
    for row in inventory:
        if row.get("resource_type") == "nodegroup":
            config = _details(row).get("scaling_config") or {}
            nodegroups.append({**row, "capacity_type": _details(row).get("capacity_type"),
                               "desired": config.get("desiredSize"),
                               "instances": len(_details(row).get("instance_ids", [])),
                               "auto_scaling_groups": _details(row).get("auto_scaling_groups")})
    note = ("Per-cluster historical costs are unavailable without AWS resource-level billing attribution. "
            "Regional EKS service costs are not divided across clusters; current node/disk relationships "
            "do not establish their historical billing ownership. Empty cells are not zero.")
    tables = [_resource_monthly(clusters, [], months, context, name="Monthly by cluster", note=note),
              _inventory_table("Clusters", clusters, ("region", "resource_name", "state", "engine_version",
                                                       "vpc_id", "subnet_ids", "created_at")),
              _table("Node groups", ("region", "cluster_name", "resource_name", "state", "instance_type",
                                      "capacity_type", "instances", Column("desired", "Desired nodes", "number"),
                                      "storage_gib", "auto_scaling_groups"), nodegroups,
                     SNAPSHOT_NOTE + " Storage (GiB) is configured disk size per node, not group total."),
              _inventory_table("Cluster nodes", nodes, ("region", "cluster_name", "nodegroup", "resource_name",
                                                        "resource_id", "instance_type", "state", "vcpu", "memory_mib",
                                                        "storage_gib", "relationship_source"),
                               SNAPSHOT_NOTE + " Region and cluster name must both match. Rows may repeat shared relationships."),
              _inventory_table("Cluster volumes", volumes, VOLUME_KEYS,
                               SNAPSHOT_NOTE + " Current EBS relationships only; shared volumes may occur more than once.")]
    extra = [row for row in inventory if row.get("resource_type") not in {"cluster", "nodegroup"}]
    tables.extend(_generic_inventory_tables(extra))
    return tables


def _ebs_tables(inventory: list[dict]) -> list[Table]:
    volumes = [row for row in inventory if row.get("resource_type") == "volume"]
    snapshots = [row for row in inventory if row.get("resource_type") == "snapshot"]
    return [_inventory_table("Volumes", volumes, VOLUME_KEYS),
            _inventory_table("Unattached volumes", [row for row in volumes
                                                     if row.get("state") == "available" and not row.get("attached_to")],
                             VOLUME_KEYS, SNAPSHOT_NOTE + " Available, unattached now; this does not prove unused during the billing period."),
            _inventory_table("Snapshots", snapshots, ("region", "resource_name", "resource_id", "parent_id",
                                                       "state", "storage_gib", "storage_type", "encrypted", "created_at"),
                             SNAPSHOT_NOTE + " Size is the source volume size, not incremental billed snapshot storage.")]


TYPE_NAMES = {
    "instance": "Instances", "cluster": "Clusters", "nodegroup": "Node groups", "volume": "Volumes",
    "snapshot": "Snapshots", "db_instance": "Database instances", "db_cluster": "Database clusters",
    "db_snapshot": "Database snapshots", "db_cluster_snapshot": "Cluster snapshots", "bucket": "Buckets",
    "function": "Functions", "table": "Tables", "file_system": "File systems", "mount_target": "Mount targets",
    "cache_cluster": "Cache clusters", "replication_group": "Replication groups", "serverless_cache": "Serverless caches",
    "domain": "Domains", "queue": "Queues", "topic": "Topics", "log_group": "Log groups",
    "distribution": "Distributions", "hosted_zone": "Hosted zones", "service": "Services", "task": "Tasks",
    "container_instance": "Container instances", "fargate_profile": "Fargate profiles", "addon": "Add-ons",
    "load_balancer": "Load balancers", "target_group": "Target groups", "classic_load_balancer": "Classic load balancers",
}


def _generic_inventory_tables(inventory: list[dict]) -> list[Table]:
    groups = defaultdict(list)
    for row in inventory:
        groups[str(row.get("resource_type") or "resource")].append(row)
    result = []
    optional = ("state", "engine", "engine_version", "instance_type", "cluster_name", "parent_id",
                "storage_gib", "storage_bytes", "storage_type", "encrypted", "multi_az", "vcpu",
                "memory_mib", "vpc_id", "availability_zone", "created_at", "list_price_per_hour",
                "price_currency", "price_status")
    for kind, members in sorted(groups.items()):
        keys = ["region", "resource_name", "resource_id", "name_source"]
        keys.extend(key for key in optional if any(row.get(key) is not None and row.get(key) != ""
                                                   for row in members))
        result.append(_inventory_table(TYPE_NAMES.get(kind, kind.replace("_", " ").title()), members, tuple(keys)))
        networking = ("subnet_ids", "private_ips", "public_ips", "security_group_ids", "attached_to",
                      "device_names", "related_resource_ids", "relationship_source")
        populated = [key for key in networking if any(row.get(key) for row in members)]
        if populated:
            result.append(_inventory_table(f"{TYPE_NAMES.get(kind, kind.replace('_', ' ').title())} links",
                                           members, ("region", "resource_name", "resource_id", *populated)))
    return result


def build_service_reports(rows: list[dict], *, generated_at: str, context: dict,
                          partial: bool) -> list[ServiceReport]:
    """Build service-specific sheets without allocating unknown historical costs."""
    # Apply the existing security rules before flattening tags or arbitrary details.
    from .report import redact_sensitive

    # Query contexts are shared large structures. They are not displayed on every
    # resource sheet, so avoid copying/redacting that same structure per row.
    rows = [redact_sensitive({key: value for key, value in row.items() if key != "query_context"})
            for row in rows]
    context = redact_sensitive(context)
    months = _months(rows, context)
    inventory = [row for row in rows if row.get("row_type") == "inventory"]
    costs = [row for row in rows if row.get("row_type") == "cost_summary"]
    overview_tables = [_info("AWS account overview", generated_at=generated_at, context=context,
                              partial=partial, inventory_count=len(inventory),
                              extra="Only original account cost_summary rows contribute to totals. "
                              "EC2 type/resource and EBS subset views are excluded to prevent double counting."),
                       _pivot("Monthly total", costs, (), months),
                       _pivot("Monthly by service", costs, ("service",), months),
                       _pivot("Monthly by region", costs, ("region",), months),
                       _pivot("Service and region", costs, ("service", "region"), months),
                       _monthly_cost_details(costs, overview=True)]
    overview_tables.extend(_control_tables(rows, context=context, generated_at=generated_at, partial=partial, overview=True))
    reports = [ServiceReport("overview", "AWS account overview", overview_tables)]
    grouped, original_names = defaultdict(list), {}
    for row in rows:
        category = row.get("row_type")
        name = str(row.get("service") or "")
        if category in {"report"}:
            continue
        if category in {"coverage", "error"} and name not in SERVICE_TITLES and name not in BILLING_KEYS:
            continue
        if category not in {"inventory", "cost_summary", "cost_breakdown", "resource_cost", "price_quote", "coverage", "error"}:
            continue
        key = _service_key(name)
        grouped[key].append(row)
        original_names.setdefault(key, name)
    for key in sorted(grouped):
        service_rows = grouped[key]
        own_inventory = [row for row in service_rows if row.get("row_type") == "inventory"]
        own_costs = [row for row in service_rows if row.get("row_type") == "cost_summary"]
        if key == "ebs":
            own_costs = [row for row in service_rows if row.get("row_type") == "cost_breakdown"
                         and row.get("cost_scope") == "ebs_region"]
        title = SERVICE_TITLES.get(key, original_names[key])
        extra = (EBS_NOTE if key == "ebs" else
                 "EC2 Compute only. Storage and networking billed as EC2 - Other have their own report."
                 if key == "ec2" else
                 "Contains EBS and other networking/storage billing; it is not an EBS-only total."
                 if key == "ec2_other" else
                 "EKS billing service costs exclude EC2 node and EBS disk billing. "
                 "Regional costs may include control-plane and other EKS charges; no per-cluster allocation is inferred."
                 if key == "eks" else "")
        tables = [_info(title, generated_at=generated_at, context=context, partial=partial,
                        inventory_count=len(own_inventory), extra=extra),
                  _pivot("Monthly by region", own_costs, ("region",), months,
                         note=BILLING_NOTE + (" " + EBS_NOTE if key == "ebs" else "")),
                  _monthly_cost_details(own_costs, extra_note=EBS_NOTE if key == "ebs" else "")]
        if key == "ec2":
            tables.extend(_ec2_tables(own_inventory, inventory, service_rows, months, context))
        elif key == "eks":
            tables.extend(_eks_tables(own_inventory, inventory, months, context))
        elif key == "ebs":
            tables.extend(_ebs_tables(own_inventory))
        else:
            tables.extend(_generic_inventory_tables(own_inventory))
        quotes = [row for row in service_rows if row.get("row_type") == "price_quote"]
        if quotes:
            tables.append(_price_table(quotes))
        tables.extend(_metadata_tables(own_inventory))
        tables.extend(_control_tables(service_rows, context=context, generated_at=generated_at, partial=partial))
        reports.append(ServiceReport(key, title, tables))
    return reports
