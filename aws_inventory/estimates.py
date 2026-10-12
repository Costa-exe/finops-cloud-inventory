"""Explicit current-capacity cost scenarios, with resource billing when available.

Rates come exclusively from collected Price List quotes. Inventory is a current
snapshot, not proof that the same capacity existed during a historical period.
"""
from __future__ import annotations

import calendar
import hashlib
import re
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext

from .costs import _period_hours
from .report import redact_sensitive


ZERO = Decimal(0)
HOURS = {"hrs", "hours", "hour", "hr"}
MONTH_UNITS = {"gb-mo", "gb-month", "gib-mo", "gib-month"}
WRAPPERS = {"auto_scaling_group", "nodegroup", "addon", "fargate_profile",
            "mount_target", "target_group", "container_instance"}


def number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Expected a finite cost or quantity.")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Expected a finite cost or quantity.") from exc
    if not result.is_finite():
        raise ValueError("Costs and quantities must be finite.")
    return result


def instant(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def month_windows(context, generated_at):
    now = instant(generated_at)
    start = instant(context.get("period_start") or now.replace(day=1, hour=0, minute=0, second=0).isoformat())
    next_month = datetime(now.year + now.month // 12, now.month % 12 + 1, 1, tzinfo=timezone.utc)
    end = instant(context.get("period_end") or next_month.isoformat())
    if start >= end:
        raise ValueError("The scenario start must precede its exclusive end.")
    cursor = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    windows = []
    while cursor < end:
        following = datetime(cursor.year + cursor.month // 12, cursor.month % 12 + 1, 1, tzinfo=timezone.utc)
        left, right = max(start, cursor), min(end, following)
        windows.append({"month": cursor.strftime("%Y-%m"), "start": left.isoformat(), "end": right.isoformat(),
                        "hours": _period_hours(left.isoformat(), right.isoformat()),
                        "month_hours": Decimal(calendar.monthrange(cursor.year, cursor.month)[1] * 24)})
        cursor = following
        if len(windows) > 1200:
            raise ValueError("The scenario exceeds 1,200 months.")
    return windows


def identity(row):
    return (str(row.get("service", "")), str(row.get("region", "")), str(row.get("resource_id", "")))


def key_for(identifier):
    return "r-" + hashlib.sha256("\0".join(identifier).encode()).hexdigest()[:20]


def details(row):
    return row.get("details") if isinstance(row.get("details"), dict) else {}


def values(value):
    return list(value) if isinstance(value, (list, tuple, set)) else [value] if value else []


def configuration(row):
    fields = ("state", "instance_type", "engine", "engine_version", "vcpu", "memory_mib", "storage_gib",
              "storage_bytes", "storage_type", "iops", "throughput_mib_s", "encrypted", "multi_az",
              "availability_zone", "vpc_id", "subnet_ids", "security_group_ids", "created_at", "details",
              "price_status", "price_source", "price_checked_at_utc")
    return {key: row[key] for key in fields if row.get(key) is not None and row.get(key) != ""}


def full_rate(quotes):
    """Only one unqualified rate; alternative SKUs/tiers are never guessed."""
    candidates = []
    for quote in quotes:
        if (str(quote.get("price_begin_range", "0")) not in {"0", "0.0"}
                or str(quote.get("price_end_range", "Inf")).lower() not in {"inf", "infinity"}
                or details(quote).get("applies_to")):
            return None
        signature = (quote.get("price_sku"), str(quote.get("price_per_unit")), quote.get("price_currency"))
        if not any(existing[0] == signature for existing in candidates):
            candidates.append((signature, quote))
    if len(candidates) != 1:
        return None
    result = candidates[0][1]
    if number(result["price_per_unit"]) < 0:
        raise ValueError("Public catalog rates cannot be negative.")
    return result


def resource_plan(row, quotes, regional_quotes, *, hours_per_day, eks_support):
    plan = {"currency": "", "hourly": ZERO, "monthly": ZERO, "known": False,
            "complete": True, "components": [], "notes": [], "rate_label": "Rate unavailable"}

    def component(label, quote, quantity, basis):
        rate, quantity = number(quote["price_per_unit"]), number(quantity)
        currency = quote.get("price_currency") or ""
        if rate < 0 or quantity < 0 or not currency:
            raise ValueError("A public price component needs a nonnegative rate, quantity and currency.")
        if plan["currency"] and currency != plan["currency"]:
            plan["notes"].append("Catalog components use different currencies; component excluded.")
            plan["complete"] = False
            return
        plan["currency"] = currency
        plan[basis] += rate * quantity
        plan["known"] = True
        plan["components"].append({"label": label, "rate": rate, "quantity": quantity,
                                   "unit": quote.get("price_unit", ""), "basis": basis,
                                   "sku": quote.get("price_sku", ""),
                                   "effective_date": quote.get("price_effective_date", "")})

    service, kind = row.get("service"), row.get("resource_type")
    if service == "ebs" and kind == "volume":
        size = row.get("storage_gib")
        capacity = full_rate([q for q in quotes if str(q.get("price_unit", "")).lower() in MONTH_UNITS
                              and "snapshot" not in str(q.get("price_component", "")).lower()])
        if capacity is None or size is None:
            plan["notes"].append("Missing or ambiguous volume capacity price/size.")
            plan["complete"] = False
        else:
            component("Provisioned storage", capacity, size, "monthly")
            plan["rate_label"] = f"{plan['currency']} {capacity['price_per_unit']}/GiB-month"
        volume_type = row.get("storage_type")
        if volume_type in {"gp3", "io1", "io2"}:
            configured = row.get("iops")
            iops = max(ZERO, number(configured) - (3000 if volume_type == "gp3" else 0)) if configured is not None else None
            throughput = (max(ZERO, number(row["throughput_mib_s"]) - 125)
                          if row.get("throughput_mib_s") is not None else None)
            for label, quantity, units in [
                ("Additional IOPS", iops, {"iops-mo", "iops-month"}),
                ("Additional throughput", throughput if volume_type == "gp3" else ZERO,
                 {"mbps-mo", "mbps-month", "mibps-mo", "mibps-month"}),
            ]:
                if quantity == 0:
                    continue
                rate = full_rate([q for q in quotes if str(q.get("price_unit", "")).lower() in units])
                if quantity is None or rate is None:
                    plan["complete"] = False
                    plan["notes"].append(f"{label}: missing quantity, missing price or tiered/ambiguous catalog; excluded.")
                else:
                    component(label, rate, quantity, "monthly")
        elif volume_type not in {"gp2", "st1", "sc1"}:
            plan["complete"] = False
            plan["notes"].append("Capacity only; any I/O or other volume-type charges are excluded.")
        plan["notes"].append("Provisioned capacity persists even when its instance is stopped. Partial months are prorated by calendar hours.")
        if volume_type == "gp3":
            plan["notes"].append("gp3 includes 3,000 IOPS and 125 MiB/s; only provisioned excess is priced.")
    elif service == "eks" and kind in {"cluster", "eks_cluster"}:
        candidates = []
        for quote in regional_quotes:
            attributes = details(quote).get("catalog_attributes", {})
            description = " ".join(str(value) for value in [quote.get("price_component", ""),
                quote.get("price_description", ""), *attributes.values()]).lower()
            if str(quote.get("price_unit", "")).lower() not in HOURS:
                continue
            if any(marker in description for marker in ("auto mode", "automode", "fargate", "provisioned control", "capabilities")):
                continue
            extended = "extended" in description
            standard = "standard" in description or "percluster" in description or "per cluster" in description
            if (eks_support == "extended" and extended) or (eks_support == "standard" and standard and not extended):
                candidates.append(quote)
        rate = full_rate(candidates)
        if rate is not None:
            component(f"EKS {eks_support} support (assumed)", rate, 1, "hourly")
            plan["rate_label"] = f"{plan['currency']} {rate['price_per_unit']}/h"
        else:
            plan["complete"] = False
            plan["notes"].append(f"No unique catalog rate for assumed {eks_support} support.")
        plan["notes"].append(f"Assumed {eks_support} support; version lifecycle is not verified. Control plane runs 24 h/day; Auto Mode and other EKS add-ons are excluded.")
    elif service == "ecs" and kind == "task" and details(row).get("launch_type") == "FARGATE":
        def fargate_rates(marker):
            matches = []
            for quote in quotes:
                attrs = details(quote).get("catalog_attributes", {})
                description = " ".join([str(quote.get("price_description", "")), *map(str, attrs.values())]).lower()
                if marker in description and "fargate" in description and not any(x in description for x in ("arm", "windows", "spot", "storage")):
                    if str(quote.get("price_unit", "")).lower() in HOURS | {"vcpu-hours", "gb-hours", "vcpu-hrs", "gb-hrs"}:
                        matches.append(quote)
            return full_rate(matches)
        for label, marker, quantity in (("Fargate vCPU", "vcpu", row.get("vcpu")),
                                         ("Fargate memory GiB", "gb-hours", number(row["memory_mib"])/1024 if row.get("memory_mib") is not None else None)):
            quote = fargate_rates(marker)
            if quote is not None and quantity is not None:
                component(label, quote, number(quantity)*hours_per_day/24, "hourly")
        plan["complete"] = False
        plan["notes"].append("Fargate current task capacity and assumed runtime; ephemeral storage above the included capacity, platform extras and transfer excluded.")
        if plan["known"]:
            raw_hourly = sum(c["rate"] * (number(row["vcpu"]) if c["label"] == "Fargate vCPU" else number(row["memory_mib"])/1024) for c in plan["components"])
            plan["rate_label"] = f"{plan['currency']} {raw_hourly}/h"
    elif service in {"elb", "vpc"}:
        eligible = [q for q in quotes if str(q.get("price_unit", "")).lower() in HOURS]
        if kind == "elastic_ip":
            state_marker = "inuse" if details(row).get("AssociationId") or details(row).get("NetworkInterfaceId") else "idle"
            eligible = [q for q in eligible if state_marker in str(details(q).get("catalog_attributes", {}).get("usagetype", "")).lower()]
        rate = full_rate(eligible)
        quantity = 1
        if kind == "vpc_endpoint":
            # Interface endpoints are billed per AZ, not per IP/interface.
            quantity = len(set(values(row.get("subnet_ids")))) or None
        if rate is not None and quantity is not None:
            component("Base provisioned hours", rate, quantity, "hourly")
            plan["rate_label"] = f"{plan['currency']} {number(rate['price_per_unit'])*quantity}/h"
        else:
            plan["notes"].append("No unambiguous supported unit price and usage quantity. " + str(row.get("price_status", "")))
        plan["complete"] = False
        plan["notes"].append("Base provisioned hours only, 24 h/day. Traffic, capacity units, data processing and other network charges excluded; endpoint quantity uses one subnet per AZ.")
    else:
        rate = full_rate([q for q in quotes if str(q.get("price_unit", "")).lower() in HOURS])
        if rate is None and row.get("list_price_per_hour") is not None and row.get("price_status") == "matched":
            rate = {**row, "price_per_unit": row["list_price_per_hour"], "price_unit": "Hrs"}
        if rate is not None:
            factor = hours_per_day / 24
            if row.get("state") in {"stopped", "stopping", "terminated", "shutting-down", "deleted"}:
                factor = ZERO
                plan["notes"].append("Current stopped/deleted state assumed throughout the scenario; compute usage is zero.")
            count = 1
            if service == "elasticache":
                count = details(row).get("node_count")
            elif service == "redshift":
                count = details(row).get("number_of_nodes")
            elif service == "opensearch":
                count = details(row).get("cluster_config", {}).get("InstanceCount")
            if count is None:
                plan["complete"] = False
                plan["notes"].append("Node count unavailable; no cluster compute estimate.")
                return plan
            component("Instance runtime", rate, factor*number(count), "hourly")
            plan["rate_label"] = f"{plan['currency']} {number(rate['price_per_unit'])*number(count)}/h"
            if service in {"elasticache", "redshift", "opensearch"}:
                plan["complete"] = False
                plan["notes"].append("Current data-node compute only; dedicated masters, warm nodes, storage, backups and usage charges excluded.")
            plan["notes"].append(f"Current On-Demand rate, with {hours_per_day} runtime hours/day; no discounts or historical runtime inferred.")
            if details(row).get("instance_lifecycle") == "spot":
                plan["notes"].append("Spot resource: estimate uses On-Demand comparison, not the Spot rate.")
            if service == "rds":
                plan["complete"] = False
                plan["notes"].append("RDS compute only; DB storage, backups and I/O are not priced by this collector.")
        else:
            plan["complete"] = False
            plan["notes"].append("No unambiguous supported unit price and usage quantity. " + str(row.get("price_status", "")))
    return plan


def monthly_cost(plan, window, actual):
    left, right = instant(window["start"]), instant(window["end"])
    relevant = []
    for row in actual:
        begin, end = instant(row["period_start"]), instant(row["period_end"])
        if end <= left or begin >= right:
            continue
        if begin < left or end > right or begin >= end:
            raise ValueError("Resource billing intervals must fall inside their selected month window.")
        relevant.append((begin, end, row))
    currencies = {row["unit"] for _, _, row in relevant}
    if len(currencies) > 1:
        return {"amount": None, "currency": "", "basis": "unavailable", "complete": False,
                "billed": None, "estimated": None, "note": "Resource billing contains multiple currencies; no conversion inferred."}
    currency = next(iter(currencies), plan["currency"])
    billed, covered, previous = ZERO, ZERO, None
    provisional = False
    for begin, end, row in sorted(relevant, key=lambda item: (item[0], item[1])):
        if previous is not None and begin < previous:
            raise ValueError("Overlapping resource billing intervals would double-count costs.")
        billed += number(row["amount"])
        covered += _period_hours(begin.isoformat(), end.isoformat())
        previous = end
        provisional = provisional or bool(row.get("estimated"))
    missing = window["hours"] - covered
    can_estimate = plan["known"] and plan["currency"] == currency
    estimate = ((plan["hourly"] * missing + plan["monthly"] * missing / window["month_hours"])
                if can_estimate else ZERO)
    known = bool(relevant) or can_estimate
    complete = (missing == 0 or (can_estimate and plan["complete"]))
    basis = "mixed" if relevant and missing > 0 and can_estimate else "billed" if relevant else "estimated" if can_estimate else "unavailable"
    return {"amount": billed + estimate if known else None, "currency": currency, "basis": basis,
            "billed": billed if relevant else None, "estimated": estimate if can_estimate and missing > 0 else None,
            "complete": complete, "covered_hours": covered, "modeled_hours": missing,
            "aws_provisional": provisional,
            "note": ("AWS amounts for returned intervals; list price fills uncovered intervals." if basis == "mixed" else
                     "Returned resource billing." if basis == "billed" else
                     "Current capacity modeled over the selected dates; not historical billed spending." if basis == "estimated" else
                     "No attributable resource cost or supported catalog estimate.")}


def _build_model(rows, *, generated_at, context, partial):
    windows = month_windows(context, generated_at)
    hours_per_day = number(context.get("estimate_hours_per_day", "24"))
    if not 0 <= hours_per_day <= 24:
        raise ValueError("Runtime hours/day must be between 0 and 24.")
    eks_support = context.get("eks_support", "standard")
    if eks_support not in {"standard", "extended"}:
        raise ValueError("EKS support assumption must be standard or extended.")
    inventory = {identity(row): row for row in rows if row.get("row_type") == "inventory"}
    quotes, regional, actual = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in rows:
        if row.get("row_type") == "price_quote":
            quotes[identity(row)].append(row)
            if row.get("resource_type") == "eks_regional_catalog":
                regional[row.get("region")].append(row)
        elif row.get("row_type") == "resource_cost":
            rid = str(row.get("resource_id", ""))
            if rid.startswith("arn:"):
                arn = rid.split(":", 5)
                if (len(arn) == 6 and arn[2] == "ec2" and arn[3] == row.get("region")
                        and arn[4] == context.get("caller_account_id") and arn[5].startswith("instance/")):
                    rid = arn[5].split("/", 1)[1]
            ident = ("ec2", str(row.get("region", "")), rid)
            actual[ident].append(row)
            if ident not in inventory:
                inventory[ident] = {**row, "row_type": "inventory", "service": "ec2",
                                    "resource_id": rid, "resource_type": "instance",
                                    "resource_name": row.get("resource_name") or rid or "Unallocated EC2 billing",
                                    "state": "Not in current inventory"}
    resources, by_identity, structural = [], {}, []
    for ident, row in sorted(inventory.items()):
        if row.get("resource_type") in WRAPPERS:
            structural.append({"service": ident[0], "region": ident[1], "id": ident[2],
                               "name": str(row.get("resource_name") or ident[2]), "type": row["resource_type"],
                               "parent": row.get("parent_id"), "related": values(row.get("related_resource_ids")),
                               "configuration": configuration(row)})
            continue
        plan = resource_plan(row, quotes[ident], regional[row.get("region")],
                             hours_per_day=hours_per_day, eks_support=eks_support)
        resource = {"key": key_for(ident), "service": ident[0], "region": ident[1], "id": ident[2],
                    "name": str(row.get("resource_name") or ident[2]), "type": row.get("resource_type", ""),
                    "state": row.get("state", ""), "instance_type": row.get("instance_type", ""),
                    "size_gib": row.get("storage_gib"), "storage_type": row.get("storage_type", ""),
                    "vcpu": row.get("vcpu"), "memory_mib": row.get("memory_mib"),
                    "cluster": values(row.get("cluster_name")), "plan": plan, "children": [],
                    "arn": row.get("resource_arn", ""), "relations": [],
                    "configuration": configuration(row),
                    "months": {w["month"]: monthly_cost(plan, w, actual[ident]) for w in windows},
                    "notes": []}
        resources.append(resource)
        by_identity[ident] = resource

    def link(parent, child):
        if child is not None and child["key"] != parent["key"] and child["key"] not in parent["children"]:
            parent["children"].append(child["key"])

    lookup = defaultdict(list)
    for ident, row in inventory.items():
        for alias in set(filter(None, [ident[2], row.get("resource_arn")])):
            lookup[(ident[1], alias)].append(ident)

    def resolve(region, identifier):
        candidates = lookup.get((region, identifier), []) or lookup.get(("global", identifier), [])
        return by_identity.get(candidates[0]) if len(candidates) == 1 else None

    def reference(region, relation, identifier):
        candidates = lookup.get((region, identifier), []) or lookup.get(("global", identifier), [])
        original = inventory[candidates[0]] if len(candidates) == 1 else {}
        target = resolve(region, identifier)
        inferred = {"sg-": "security_group", "vpc-": "vpc", "subnet-": "subnet",
                    "vol-": "volume", "i-": "instance", "eni-": "network_interface"}
        kind = original.get("resource_type") or next((kind for prefix, kind in inferred.items()
                                                      if identifier.startswith(prefix)), "resource")
        return {"relation": relation, "id": identifier, "key": target["key"] if target else None,
                "name": original.get("resource_name") or identifier,
                "type": kind, "service": original.get("service", ""), "region": region}

    for ident, row in inventory.items():
        resource = by_identity.get(ident)
        if resource is None:
            continue
        references = []
        for field in ("parent_id", "related_resource_ids", "attached_to", "vpc_id", "subnet_ids", "security_group_ids"):
            references.extend((field, value) for value in values(row.get(field)) if isinstance(value, str))
        if ident[0] == "cloudfront":
            if details(row).get("web_acl_id"):
                references.append(("web_acl", details(row)["web_acl_id"]))
            for origin in details(row).get("origins", []):
                references.append(("origin", origin.get("DomainName", "")))
        for relation, identifier in dict.fromkeys(references):
            if not identifier or identifier == ident[2]:
                continue
            resource["relations"].append(reference(ident[1], relation, identifier))
        parent = by_identity.get((ident[0], ident[1], row.get("parent_id")))
        if parent:
            link(parent, resource)
        if ident[0] == "rds" and row.get("cluster_name"):
            parent = by_identity.get(("rds", ident[1], row["cluster_name"]))
            if parent:
                link(parent, resource)
        if ident[0] == "emr":
            for identifier in values(row.get("related_resource_ids")):
                child = by_identity.get(("ec2", ident[1], identifier))
                if child:
                    link(resource, child)
        if ident[0] == "ebs" and row.get("resource_type") == "volume":
            for instance_id in values(row.get("attached_to")):
                parent = by_identity.get(("ec2", ident[1], instance_id))
                if parent:
                    link(parent, resource)
        if ident[0] == "ec2" and row.get("resource_type") == "instance":
            volume_ids = set(values(row.get("related_resource_ids")))
            volume_ids.update(block.get("volume_id") for block in details(row).get("block_devices", []))
            for volume_id in sorted(filter(None, volume_ids)):
                if not str(volume_id).startswith("vol-"):
                    continue
                child = by_identity.get(("ebs", ident[1], volume_id))
                if child:
                    link(resource, child)
                else:
                    resource["notes"].append(f"Linked volume {volume_id} was not collected; its cost is excluded.")
        if ident[0] in {"eks", "ecs"} and row.get("resource_type") in {"cluster", "eks_cluster"}:
            for other_ident, other in by_identity.items():
                if other_ident[1] == ident[1] and other_ident[0] in {"ec2", "ebs"}:
                    if resource["name"] in other["cluster"] or ident[2] in other["cluster"]:
                        link(resource, other)
    # Target groups are structural inventory; connect their ALBs to resolved targets.
    for ident, row in inventory.items():
        if row.get("resource_type") == "target_group":
            for parent_id in values(row.get("attached_to")):
                parent = resolve(ident[1], parent_id)
                if parent:
                    for target_id in values(row.get("related_resource_ids")):
                        target = resolve(ident[1], target_id)
                        if target:
                            link(parent, target)
    index = {r["key"]: r for r in resources}
    for resource in resources:
        seen, pending = set(), [resource["key"]]
        while pending:
            key = pending.pop()
            if key in seen:
                continue
            seen.add(key)
            pending.extend(index[key]["children"])
        resource["included_keys"] = sorted(seen)
        resource["totals"] = {}
        for window in windows:
            month, totals, bases = window["month"], {}, set()
            complete = not resource["notes"]
            for key in seen:
                entry = index[key]["months"][month]
                complete = complete and entry["complete"] and not index[key]["notes"]
                if entry["amount"] is not None:
                    totals[entry["currency"]] = totals.get(entry["currency"], ZERO) + entry["amount"]
                    bases.add(entry["basis"])
            resource["totals"][month] = {"amounts": totals, "complete": complete,
                                         "basis": next(iter(bases)) if len(bases) == 1 else "mixed" if bases else "unavailable"}
    for wrapper in structural:
        wrapper["relations"] = [reference(wrapper["region"], "related_resource_ids", identifier)
                                for identifier in dict.fromkeys([wrapper["parent"], *wrapper["related"]])
                                if isinstance(identifier, str) and identifier]
    summary = {}
    for window in windows:
        month, currencies = window["month"], {}
        for resource in resources:
            entry = resource["months"][month]
            if entry["amount"] is None:
                continue
            group = currencies.setdefault(entry["currency"], {"total": ZERO, "billed": ZERO, "estimated": ZERO,
                                                               "by_service": {}, "by_region": {}})
            group["total"] += entry["amount"]
            group["billed"] += entry["billed"] or ZERO
            group["estimated"] += entry["estimated"] or ZERO
            for bucket, key in (("by_service", resource["service"]), ("by_region", resource["region"])):
                group[bucket][key] = group[bucket].get(key, ZERO) + entry["amount"]
        summary[month] = currencies
    billing = []
    for row in rows:
        if row.get("row_type") == "cost_summary":
            billing.append({"month": row["period_start"][:7], "region": row.get("region", ""),
                            "service": row.get("service", ""), "amount": number(row["amount"]),
                            "currency": row["unit"], "metric": row["metric"], "estimated": row.get("estimated", False)})
    from .costs import SERVICE_FILTERS, EC2_OTHER
    billing_services = {key: expression["Dimensions"]["Values"] for key, expression in SERVICE_FILTERS.items()}
    billing_services["ebs"] = [EC2_OTHER]
    billing_services["vpc"] = list(billing_services["vpc"]) + [EC2_OTHER]
    return {"generated_at": generated_at, "account": context.get("caller_account_id", ""),
            "partial": partial, "simulation_only": bool(context.get("simulation_only")),
            "windows": windows, "resources": resources, "structural": structural, "summary": summary, "billing": billing,
            "billing_services": billing_services,
            "assumptions": {"hours_per_day": hours_per_day, "eks_support": eks_support,
                            "inventory_basis": "Current snapshot applied to all selected dates as a cost scenario, not historical usage.",
                            "excluded": "Unpriced components, data transfer, taxes, discounts and request/usage charges without measured usage. Shared components are included in each parent view but counted once in the overall model."},
            "issues": [row for row in rows if row.get("row_type") == "error"], "context": context}


def build_cost_model(rows, *, generated_at, context, partial=False):
    # Sufficient precision for pricing arithmetic; never round to cents internally.
    with localcontext() as decimal_context:
        decimal_context.prec = 38
        safe_rows = [redact_sensitive({k: v for k, v in row.items() if k != "query_context"}) for row in rows]
        return _build_model(safe_rows, generated_at=generated_at,
                            context=redact_sensitive(context), partial=partial)
