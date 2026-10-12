#!/usr/bin/env python3
"""Export a unified HTML resource cost report with catalog-based estimates."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:
    raise SystemExit("Missing dependency. Run: python -m pip install -r requirements.txt")

from aws_inventory.localization import LANGUAGES, validate_timezone
from aws_inventory.common import InventoryContext, error_message
from aws_inventory.costs import fetch_costs, fetch_ec2_resource_costs, fetch_service_breakdowns
from aws_inventory.inventory_compute import COLLECTORS
from aws_inventory.inventory_services import GLOBAL_COLLECTORS, REGIONAL_COLLECTORS
from aws_inventory.pricing import add_prices
from aws_inventory.report import write_report
from aws_inventory.workbooks import validate_bundle_destination, write_service_reports
from aws_inventory.html_report import write_html_report
from aws_inventory.inventory_extended import EXTENDED_COLLECTORS, collect_global_waf

REGIONAL_COLLECTORS = {**REGIONAL_COLLECTORS, **EXTENDED_COLLECTORS}
GLOBAL_COLLECTORS = {**GLOBAL_COLLECTORS, "waf": collect_global_waf}


SERVICES = sorted(set(COLLECTORS) | set(REGIONAL_COLLECTORS) | set(GLOBAL_COLLECTORS))
METRICS = ("AmortizedCost", "UnblendedCost", "BlendedCost", "NetAmortizedCost",
           "NetUnblendedCost")
GROUPS = ("SERVICE", "REGION", "AZ", "INSTANCE_TYPE", "LINKED_ACCOUNT", "USAGE_TYPE",
          "OPERATION", "PURCHASE_TYPE", "RECORD_TYPE", "PLATFORM", "TENANCY",
          "LEGAL_ENTITY_NAME", "INVOICING_ENTITY")


def decimal_argument(value):
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("Enter a number between 0 and 24.") from exc


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Export one offline HTML report with resource costs, catalog estimates and attached-resource totals.",
        epilog="Without arguments, start interactive prompts. AWS resources are never modified.")
    p.add_argument("command", nargs="?", choices=["export"], default="export")
    p.add_argument("--interactive", action="store_true", help="Ask one question at a time")
    p.add_argument("--profile", help="Boto3 profile; otherwise use the standard credential chain")
    p.add_argument("--region", "--regions", dest="regions", nargs="+",
                   help="AWS region codes separated by spaces/commas, global, or all (default)")
    p.add_argument("--service", "--services", dest="services", nargs="+",
                   help="Services separated by spaces/commas, or all (default)")
    p.add_argument("--from", dest="date_from", help="Inclusive start, YYYY-MM-DD (UTC)")
    p.add_argument("--to", dest="date_to", help="Exclusive end, YYYY-MM-DD (UTC)")
    p.add_argument("--mode", choices=["combined", "inventory", "costs"], default="combined",
                   help="Inventory and costs, inventory only, or costs only")
    p.add_argument("--granularity", type=str.upper, choices=["MONTHLY", "DAILY", "HOURLY"],
                   default="MONTHLY", help="Default MONTHLY; HOURLY requires AWS opt-in")
    p.add_argument("--group-by", nargs="+", default=["SERVICE", "REGION"],
                   help="One or two Cost Explorer dimensions; default SERVICE REGION")
    p.add_argument("--metric", choices=METRICS, default="AmortizedCost")
    p.add_argument("--filter-file", type=Path, help="Cost Explorer JSON expression (costs only)")
    p.add_argument("--resource-costs", action="store_true",
                   help="Add costs by EC2 ID (CE opt-in, period within the last 14 days)")
    p.add_argument("--prices", action=argparse.BooleanOptionalAction, default=True,
                   help="Current On-Demand prices for supported resources (enabled by default)")
    p.add_argument("--ce-region", default="us-east-1", help="Cost Explorer endpoint region")
    p.add_argument("--pricing-region", default="us-east-1", help="Price List Query API endpoint region")
    p.add_argument("--billing-view-arn", help="Optional billing view, always filtered to the caller account")
    p.add_argument("--format", dest="report_format", choices=["html", "bundle", "raw-csv"], default="html",
                   help="Unified HTML (default); legacy workbook bundle or technical CSV available explicitly")
    p.add_argument("--estimate-hours-per-day", type=decimal_argument, default=Decimal(24),
                   help="Instance runtime assumption for HTML estimates, 0-24 (default 24)")
    p.add_argument("--eks-support", choices=["standard", "extended"], default="standard",
                   help="Explicit EKS support-tier assumption for HTML catalog estimates (default standard)")
    p.add_argument("--timezone", default="UTC",
                   help="IANA timezone for HTML timestamps, e.g. Europe/Rome. Billing periods remain UTC.")
    p.add_argument("--language", choices=LANGUAGES, default="en", help="HTML and embedded Excel language: en, it, fr, es")
    p.add_argument("--date-format", choices=["iso", "dmy", "mdy"], default="iso",
                   help="Display dates as YYYY-MM-DD, DD/MM/YYYY or MM/DD/YYYY; CLI inputs remain ISO")
    p.add_argument("--output", type=Path,
                   help="Legacy formats only: directory for bundle or file for raw-csv. HTML always uses output/ACCOUNT/YYYY-MM-DD_SERVICE_HHMM.html")
    p.add_argument("--excel", action="store_true", help="UTF-8 BOM, semicolon delimiter, decimal comma")
    p.add_argument("--overwrite", action="store_true", help="Replace an existing generated report")
    p.add_argument("--workers", type=int, default=4,
                   help="Concurrent inventory regions, 1-32 (default 4); 1 runs sequentially")
    p.add_argument("--strict", action="store_true",
                   help="Do not publish the report if any read fails")
    p.add_argument("--list-services", action="store_true", help="List services and exit without AWS access")
    return p


def split_values(values) -> list[str]:
    return list(dict.fromkeys(part.strip() for value in values for part in value.split(",")
                              if part.strip()))


def ask(label: str, default: str = "", validator=None) -> str:
    while True:
        value = input(f"{label}" + (f" [{default}]" if default else "") + ": ").strip() or default
        try:
            if not value:
                raise ValueError("Enter a value.")
            if validator:
                validator(value)
            return value
        except ValueError as exc:
            print(str(exc), file=sys.stderr)


def parse_date(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Use the YYYY-MM-DD date format.")
    return date.fromisoformat(value)


def choice_validator(choices):
    def validate_choice(value):
        if value not in choices:
            raise ValueError("Use one of: " + ", ".join(choices))
    return validate_choice


def guided(args):
    if not sys.stdin.isatty():
        raise ValueError("Interactive prompts require a terminal. Use --from, --to and other flags.")
    print("AWS report configuration. Press Enter to accept the value in brackets.")
    if not args.regions:
        args.regions = ask("Regions (e.g. eu-west-1,eu-central-1 or all)", "all").split()
    if not args.services:
        print("Services: " + ", ".join(SERVICES))
        args.services = ask("Services (e.g. ec2,eks,rds or all)", "all").split()
    if args.mode != "inventory":
        today = datetime.now(timezone.utc).date()
        this_month = today.replace(day=1)
        previous_month = (this_month - timedelta(days=1)).replace(day=1)
        if not args.date_from:
            args.date_from = ask("Inclusive start date UTC", previous_month.isoformat(), parse_date)
        if not args.date_to:
            def validate_end(value):
                if parse_date(value) <= parse_date(args.date_from):
                    raise ValueError("The end date must be after the start date.")
            default_end = max(this_month, parse_date(args.date_from) + timedelta(days=1))
            args.date_to = ask("EXCLUSIVE end date UTC", default_end.isoformat(), validate_end)
    if not args.output and args.report_format != "html":
        label, default = (("Report directory", "aws_reports") if args.report_format == "bundle"
                          else ("CSV path", "aws_inventory.csv"))
        args.output = Path(ask(label, default))
    if args.report_format == "html":
        args.language = ask("Output language (en/it/fr/es)", args.language,
                            choice_validator(LANGUAGES))
        args.date_format = ask("Output date format (iso/dmy/mdy)", args.date_format,
                               choice_validator(("iso", "dmy", "mdy")))
        args.timezone = ask("Display timezone (IANA name, e.g. Europe/Rome; billing stays UTC)",
                            args.timezone, validate_timezone)
        def validate_hours(value):
            try:
                valid = Decimal(value).is_finite() and 0 <= Decimal(value) <= 24
            except ArithmeticError:
                valid = False
            if not valid:
                raise ValueError("Runtime hours/day must be between 0 and 24.")
        args.estimate_hours_per_day = Decimal(ask("Estimated instance runtime hours/day",
                                                 str(args.estimate_hours_per_day), validate_hours))


def validate(args):
    if args.report_format == "html" and args.output is not None:
        raise ValueError("--output is not supported for HTML. Reports always use output/ACCOUNT/YYYY-MM-DD_SERVICE_HHMM.html under the current directory.")
    validate_timezone(args.timezone)
    if args.report_format != "html" and (args.timezone != "UTC" or args.language != "en" or args.date_format != "iso"):
        raise ValueError("Timezone, language and date display options currently require --format html.")
    args.regions = [item.lower() for item in split_values(args.regions or ["all"])]
    args.services = {item.lower() for item in split_values(args.services or ["all"])}
    if not args.regions or not args.services:
        raise ValueError("Select at least one region and one service.")
    if "all" in args.regions and len(args.regions) != 1:
        raise ValueError("Use all on its own for regions.")
    if "all" in args.services and len(args.services) != 1:
        raise ValueError("Use all on its own for services.")
    unknown = args.services - set(SERVICES) - {"all"}
    if unknown:
        raise ValueError("Unknown services: " + ", ".join(sorted(unknown)))
    for region in args.regions:
        if region not in {"all", "global"} and not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", region):
            raise ValueError(f"Invalid region: {region}")
    args.group_by = [item.upper() for item in split_values(args.group_by)]
    if not 1 <= len(args.group_by) <= 2 or len(set(args.group_by)) != len(args.group_by):
        raise ValueError("--group-by requires one or two distinct dimensions.")
    if set(args.group_by) - set(GROUPS):
        raise ValueError("Supported dimensions: " + ", ".join(GROUPS))
    if not 1 <= args.workers <= 32:
        raise ValueError("--workers must be between 1 and 32.")
    if not args.estimate_hours_per_day.is_finite() or not 0 <= args.estimate_hours_per_day <= 24:
        raise ValueError("--estimate-hours-per-day must be finite and between 0 and 24.")
    if args.report_format in {"html", "bundle"}:
        if args.granularity != "MONTHLY" or args.group_by != ["SERVICE", "REGION"]:
            raise ValueError("HTML and service workbooks require MONTHLY granularity and SERVICE REGION grouping. "
                             "Use --format raw-csv for custom granularity/grouping.")
    if args.report_format == "bundle":
        # Resolve the optional dependency before making any AWS request.
        try:
            import openpyxl  # noqa: F401
        except ImportError as exc:
            raise ValueError("Excel export requires openpyxl. Run .venv/bin/python -m pip install "
                             "-r requirements.txt") from exc
    if args.output is None and args.report_format != "html":
        args.output = Path("aws_reports" if args.report_format == "bundle" else "aws_inventory.csv")
    validate_output(args)
    args.filter_expression = None
    if args.filter_file:
        with args.filter_file.expanduser().open(encoding="utf-8-sig") as stream:
            args.filter_expression = json.load(stream)
        if not isinstance(args.filter_expression, dict) or not args.filter_expression:
            raise ValueError("--filter-file must contain a non-empty JSON expression.")
    if args.mode == "inventory":
        if args.resource_costs or args.date_from or args.date_to or args.filter_file:
            raise ValueError("Cost dates/filters and --resource-costs do not apply to --mode inventory.")
        return
    if not args.date_from or not args.date_to:
        raise ValueError("Set --from and --to, or use --interactive.")
    start, end = parse_date(args.date_from), parse_date(args.date_to)
    today = datetime.now(timezone.utc).date()
    if start >= end:
        raise ValueError("--from must be before --to (exclusive end).")
    if end > today + timedelta(days=1) or start > today:
        raise ValueError("The period cannot extend beyond the current UTC day.")
    if args.resource_costs or args.granularity == "HOURLY":
        if start < today - timedelta(days=14):
            raise ValueError("EC2 resource/HOURLY costs require a period within the last 14 days.")
    if args.resource_costs and not args.services.intersection({"all", "ec2", "eks", "ecs"}):
        raise ValueError("--resource-costs covers EC2 Compute: select ec2, eks, ecs or all.")


def validate_output(args):
    """Check explicit destinations early; automatic HTML paths need STS identity first."""
    if args.output is None:
        return
    args.output = args.output.expanduser().absolute()
    if args.report_format == "bundle":
        if args.output.suffix.lower() in {".csv", ".xlsx"}:
            raise ValueError("Bundle --output must be a directory, for example aws_reports. "
                             "Use --format raw-csv for a single CSV file.")
        validate_bundle_destination(args.output, overwrite=args.overwrite)
    else:
        if args.report_format == "html" and (args.output.suffix.lower() not in {".html", ".htm"}
                                              or args.output.is_symlink()):
            raise ValueError("HTML output must be a regular .html or .htm file path.")
        if args.output.exists() and not args.overwrite:
            raise FileExistsError(f"File already exists: {args.output}. Use --overwrite.")
        if args.output.is_dir():
            raise ValueError("HTML and raw-csv output require a file path for --output.")


def html_output_path(account_id, services, generated_at, display_timezone):
    """Portable filename under the launch directory, using the display timezone."""
    if not re.fullmatch(r"[0-9]{12}", account_id):
        raise ValueError("Expected a 12-digit AWS account ID for the report directory.")
    local = datetime.fromisoformat(generated_at).astimezone(ZoneInfo(display_timezone))
    service = "all" if "all" in services else next(iter(services)) if len(services) == 1 else "multi"
    if service != "all" and service != "multi" and service not in SERVICES:
        raise ValueError("Unsupported service in report filename.")
    return Path.cwd() / "output" / account_id / f"{local:%Y-%m-%d}_{service}_{local:%H%M}.html"


def inventory_selection(services: set[str]) -> set[str]:
    selected = set(SERVICES) if "all" in services else set(services)
    if selected.intersection({"eks", "ecs", "emr"}):
        selected.add("ec2")
    if "ec2" in selected:
        selected.add("ebs")
    return selected


def discover_regions(ctx, requested: list[str], endpoint: str) -> list[str]:
    if requested != ["all"]:
        return sorted(set(requested) - {"global"})
    response = ctx.call("ec2", "describe_regions", endpoint, AllRegions=False)
    regions = sorted(item["RegionName"] for item in response.get("Regions", []))
    if not regions:
        raise ValueError("AWS returned no enabled regions; set --region explicitly.")
    return regions


def run_collector(ctx, name: str, region: str, function) -> dict:
    coverage_region = "global" if GLOBAL_COLLECTORS.get(name) is function else region
    print(f"Inventory {name} / {coverage_region}...", file=sys.stderr)
    count, issues = len(ctx.rows), len(ctx.issues)
    try:
        function(ctx, region)
    except (BotoCoreError, ClientError, ValueError, KeyError, TypeError) as exc:
        code, message = error_message(exc)
        ctx.issue(name, region, "collector", message, code=code)
    return {"row_type": "coverage", "account_id": ctx.account_id, "service": name,
            "region": coverage_region, "resource_count": len(ctx.rows) - count,
            "state": "partial" if len(ctx.issues) > issues else "complete",
            "details": {"api_endpoint_region": region},
            "notes": "Current resources; zero with state=complete means the list was empty."}


def enrich_resource_costs(rows: list[dict], inventory: list[dict]):
    index = {}
    for resource in inventory:
        if resource.get("service") == "ec2" and resource.get("resource_id", "").startswith("i-"):
            index[(resource["region"], resource["resource_id"])] = resource
    for row in rows:
        raw_id = row.get("resource_id", "")
        instance_id = raw_id.rsplit("/", 1)[-1]
        resource = index.get((row.get("region"), instance_id))
        if resource:
            for key in ("resource_name", "name_source", "resource_arn", "resource_type",
                        "instance_type", "cluster_name", "availability_zone"):
                if key in resource:
                    row[key] = resource[key]
            row["relationship_source"] = "resource_id_exact_match_current_inventory"
        else:
            row["notes"] = "Resource not found in the current inventory; it may be deleted or excluded."


def collect_regions(ctx, regions: list[str], selected: set[str], workers: int) -> list[dict]:
    """Run independent regions concurrently, retaining EKS -> EC2 -> EBS order inside each."""
    collectors = {**COLLECTORS, **REGIONAL_COLLECTORS}
    priority = {"eks": 0, "ecs": 1, "ec2": 2, "ebs": 3}
    services = sorted(selected & collectors.keys(), key=lambda s: (priority.get(s, 4), s))

    def collect_region(region):
        child = ctx.fork()
        coverage = [run_collector(child, service, region, collectors[service]) for service in services]
        # Raw lookup responses are no longer needed after the regional dependency chain.
        child.cache.clear()
        return child, coverage

    coverage = []

    def merge(result):
        child, region_coverage = result
        ctx.rows.extend(child.rows)
        ctx.issues.extend(child.issues)
        coverage.extend(region_coverage)

    if workers == 1 or len(regions) < 2:
        for region in regions:
            merge(collect_region(region))
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(regions)),
                                thread_name_prefix="inventory-region") as executor:
            # map returns input order, so completion timing never changes the report.
            for result in executor.map(collect_region, regions):
                merge(result)
    return coverage


def execute(args, *, session=None) -> int:
    if session is None:
        session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
    endpoint = session.region_name or next((r for r in args.regions if r not in {"all", "global"}),
                                           "us-east-1")
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    selected = inventory_selection(args.services)
    ctx = InventoryContext(session, "", "aws", generated, selected, set(), args.regions == ["all"])
    identity = ctx.call("sts", "get_caller_identity", endpoint)
    ctx.account_id = identity["Account"]
    ctx.partition = identity["Arn"].split(":")[1]
    if args.report_format == "html":
        args.output = html_output_path(ctx.account_id, args.services, generated, args.timezone)
        validate_output(args)
    regions = discover_regions(ctx, args.regions, endpoint) if args.mode != "costs" else []
    ctx.regions = set(regions)
    ctx.requested_services = args.services
    print(f"Account {ctx.account_id} | profile: {args.profile or session.profile_name or 'standard chain'}\n"
          f"Regions: {', '.join(args.regions)} | services: {', '.join(sorted(args.services))}\n"
          f"Cost period: {args.date_from or '-'} -> {args.date_to or '-'} (exclusive end, UTC)\n"
          f"Metric: {args.metric} | granularity: {args.granularity} | Report: {args.output}\n"
          f"Concurrent regions: {args.workers}", file=sys.stderr)
    if args.mode != "costs" and selected != args.services and "all" not in args.services:
        print("Inventory includes regional dependencies: " + ", ".join(sorted(selected)), file=sys.stderr)
    context = {"caller_account_id": ctx.account_id, "profile": args.profile or session.profile_name,
               "mode": args.mode, "requested_regions": args.regions,
               "inventory_regions": regions, "requested_services": sorted(args.services),
               "inventory_services": sorted(selected) if args.mode != "costs" else [],
               "period_start": args.date_from, "period_end": args.date_to,
               "granularity": args.granularity, "metric": args.metric,
               "group_by": args.group_by, "filter": args.filter_expression,
               "inventory_time_basis": "current_snapshot", "prices_time_basis": "current_catalog",
               "prices_requested": args.prices and args.mode != "costs",
               "resource_costs_requested": args.resource_costs,
               "workers": args.workers,
               "excel": args.excel, "report_format": args.report_format,
               "estimate_hours_per_day": str(args.estimate_hours_per_day),
               "eks_support": args.eks_support, "currency": "USD", "timezone": args.timezone,
               "language": args.language, "date_format": args.date_format}
    rows = []
    cost_regions = None if args.regions == ["all"] else args.regions
    if args.mode != "inventory":
        print("Reading Cost Explorer (API requests may incur charges)...", file=sys.stderr)
        # Accept rows only after the entire query succeeds: never publish partial totals.
        try:
            costs = fetch_costs(ctx, start=args.date_from, end=args.date_to,
                                granularity=args.granularity, metric=args.metric,
                                regions=cost_regions, services=args.services, group_by=args.group_by,
                                filter_expression=args.filter_expression, ce_region=args.ce_region,
                                billing_view_arn=args.billing_view_arn)
        except (BotoCoreError, ClientError, ValueError) as exc:
            if args.report_format != "html":
                raise
            code, message = error_message(exc)
            ctx.issue("cost_explorer", args.ce_region, "get_cost_and_usage",
                      "Account billing unavailable; continuing with resource price estimates. " + message, code=code)
            costs = []
        rows.extend(costs)
        rows.append({"row_type": "coverage", "service": "cost_explorer",
                     "state": "partial" if ctx.issues else "complete", "resource_count": len(costs),
                     "notes": "Returned cost groups; zero means a successful query without groups."})
        if args.report_format == "bundle":
            print("Monthly EC2 instance-type and EBS cost breakdowns...", file=sys.stderr)
            rows.extend(fetch_service_breakdowns(
                ctx, start=args.date_from, end=args.date_to, metric=args.metric,
                regions=cost_regions, services=args.services,
                filter_expression=args.filter_expression, ce_region=args.ce_region,
                billing_view_arn=args.billing_view_arn))
    if args.mode != "costs":
        rows.extend(collect_regions(ctx, regions, selected, args.workers))
        for service in sorted(selected & GLOBAL_COLLECTORS.keys()):
            if service == "s3" or args.regions == ["all"] or "global" in args.regions:
                rows.append(run_collector(ctx, service, endpoint, GLOBAL_COLLECTORS[service]))
        rows.extend(ctx.rows)
    if args.resource_costs:
        print("EC2 resource costs (DAILY, requires AWS opt-in)...", file=sys.stderr)
        try:
            resource_costs = fetch_ec2_resource_costs(
                ctx, start=args.date_from, end=args.date_to, metric=args.metric,
                regions=cost_regions, filter_expression=args.filter_expression,
                ce_region=args.ce_region, billing_view_arn=args.billing_view_arn)
            enrich_resource_costs(resource_costs, ctx.rows)
            rows.extend(resource_costs)
            rows.append({"row_type": "coverage", "service": "ec2_resource_costs",
                         "state": "complete", "resource_count": len(resource_costs)})
        except (BotoCoreError, ClientError, ValueError) as exc:
            code, message = error_message(exc)
            ctx.issue("ce", args.ce_region, "get_cost_and_usage_with_resources", message, code=code)
    if args.prices and args.mode != "costs":
        print("Current AWS Price List On-Demand catalog...", file=sys.stderr)
        rows.extend(add_prices(ctx, ctx.rows, args.pricing_region))
    if ctx.issues and args.strict:
        print("--strict: incomplete reads; report was not published.", file=sys.stderr)
        return 1
    rows.extend(ctx.issues)
    counts = Counter(row["row_type"] for row in rows)
    rows.append({"row_type": "report", "account_id": ctx.account_id,
                 "details": {"row_counts": dict(counts)},
                 "notes": "Historical costs, current inventory and current prices use different time bases. "
                          "Do not add cost_summary and resource_cost: these views overlap."})
    writer = (write_html_report if args.report_format == "html" else
              write_service_reports if args.report_format == "bundle" else write_report)
    writer(args.output, rows, generated_at=generated, context=context,
           partial=bool(ctx.issues), excel=args.excel, overwrite=args.overwrite)
    print(f"Created {args.output}: {counts.get('inventory', 0)} resources, "
          f"{counts.get('cost_summary', 0)} cost rows, "
          f"{counts.get('price_quote', 0)} price quotes, {len(ctx.issues)} read errors.",
          file=sys.stderr)
    return 2 if ctx.issues else 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = parser()
    args = p.parse_args(argv)
    if args.list_services:
        print("all, " + ", ".join(SERVICES))
        print("all: all billing services and all implemented inventory collectors; see README.")
        return 0
    try:
        if args.interactive or not argv or argv == ["export"]:
            guided(args)
        validate(args)
        return execute(args)
    except KeyboardInterrupt:
        print("Interrupted; an in-progress extraction does not publish a report.", file=sys.stderr)
        return 130
    except EOFError:
        print("Input ended during interactive prompts. Use explicit parameters.", file=sys.stderr)
        return 1
    except (BotoCoreError, ClientError, OSError, ValueError) as exc:
        code, message = error_message(exc)
        print(f"Error {code}: {message}", file=sys.stderr)
        print("Report was not published. Check the AWS session, permissions, and parameters.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
