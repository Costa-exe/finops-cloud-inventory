"""Cost Explorer billed costs without estimated prices or invented allocations.

Every query is scoped to the caller's account, including Organizations
management accounts. Aggregate and per-instance rows are overlapping views
and must never be added together.

AWS references:
https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostAndUsage.html
https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostAndUsageWithResources.html
https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetDimensionValues.html
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from functools import lru_cache

from .common import AWS_ERRORS, error_message


EC2_COMPUTE = "Amazon Elastic Compute Cloud - Compute"
EC2_OTHER = "EC2 - Other"


def _dimension(key: str, values: list[str]) -> dict:
    return {"Dimensions": {"Key": key, "Values": values}}


# EBS has no separate SERVICE dimension: _service_filter discovers its USAGE_TYPEs.
# VPC includes NAT and endpoint usage billed under EC2 - Other.
SERVICE_FILTERS = {
    "ec2": _dimension("SERVICE", [EC2_COMPUTE, EC2_OTHER]),
    "eks": _dimension("SERVICE", ["Amazon Elastic Container Service for Kubernetes",
                                   "Amazon Elastic Kubernetes Service"]),
    "ecs": _dimension("SERVICE", ["Amazon Elastic Container Service"]),
    "elb": _dimension("SERVICE", ["Amazon Elastic Load Balancing"]),
    "vpc": _dimension("SERVICE", ["Amazon Virtual Private Cloud"]),
    "rds": _dimension("SERVICE", ["Amazon Relational Database Service"]),
    "s3": _dimension("SERVICE", ["Amazon Simple Storage Service"]),
    "lambda": _dimension("SERVICE", ["AWS Lambda"]),
    "dynamodb": _dimension("SERVICE", ["Amazon DynamoDB"]),
    "efs": _dimension("SERVICE", ["Amazon Elastic File System"]),
    "elasticache": _dimension("SERVICE", ["Amazon ElastiCache"]),
    "opensearch": _dimension("SERVICE", ["Amazon OpenSearch Service",
                                          "Amazon Elasticsearch Service"]),
    "redshift": _dimension("SERVICE", ["Amazon Redshift"]),
    "sqs": _dimension("SERVICE", ["Amazon Simple Queue Service"]),
    "sns": _dimension("SERVICE", ["Amazon Simple Notification Service"]),
    "cloudwatch": _dimension("SERVICE", ["AmazonCloudWatch"]),
    "cloudfront": _dimension("SERVICE", ["Amazon CloudFront"]),
    "route53": _dimension("SERVICE", ["Amazon Route 53"]),
}

SERVICE_FILTERS.update({selector: _dimension("SERVICE", names) for selector, names in {
    "fsx": ["Amazon FSx"], "backup": ["AWS Backup"], "apigateway": ["Amazon API Gateway"],
    "msk": ["Amazon Managed Streaming for Apache Kafka"], "kinesis": ["Amazon Kinesis"],
    "emr": ["Amazon Elastic MapReduce"], "glue": ["AWS Glue"], "athena": ["Amazon Athena"],
    "sagemaker": ["Amazon SageMaker"], "bedrock": ["Amazon Bedrock", "Amazon Bedrock Service", "Amazon Bedrock Foundation Models"],
    "eventbridge": ["Amazon EventBridge"], "stepfunctions": ["AWS Step Functions"],
    "ecr": ["Amazon EC2 Container Registry (ECR)"], "kms": ["AWS Key Management Service"],
    "secretsmanager": ["AWS Secrets Manager"], "waf": ["AWS WAF"],
}.items()})

SUPPORTED_GROUPS = {
    "SERVICE", "REGION", "AZ", "INSTANCE_TYPE", "LINKED_ACCOUNT", "USAGE_TYPE",
    "OPERATION", "PURCHASE_TYPE", "RECORD_TYPE", "PLATFORM", "TENANCY",
    "LEGAL_ENTITY_NAME", "INVOICING_ENTITY", "RESOURCE_ID",
}
COST_METRICS = {
    "AmortizedCost", "UnblendedCost", "BlendedCost", "NetAmortizedCost",
    "NetUnblendedCost",
}
GLOBAL_REGION_VALUES = {"", "global", "noregion", "no region", "no-region"}


def _and(*expressions: dict | None) -> dict:
    parts = [deepcopy(expression) for expression in expressions if expression]
    if not parts:
        raise ValueError("The Cost Explorer filter must include the caller's account.")
    return parts[0] if len(parts) == 1 else {"And": parts}


def _or(expressions: list[dict]) -> dict | None:
    if not expressions:
        return None
    return expressions[0] if len(expressions) == 1 else {"Or": expressions}


def _pages(ctx, operation: str, ce_region: str, request: dict):
    """Paginate Cost Explorer; a repeated token invalidates the entire query."""
    parameters = deepcopy(request)
    seen = set()
    while True:
        response = ctx.call("ce", operation, ce_region, **parameters)
        yield response
        token = response.get("NextPageToken")
        if not token:
            return
        if token in seen:
            raise ValueError(f"Cost Explorer {operation}: repeated pagination token.")
        seen.add(token)
        parameters["NextPageToken"] = token


def _dimension_values(ctx, *, dimension: str, start: str, end: str,
                      expression: dict, ce_region: str,
                      billing_view_arn: str | None) -> list[str]:
    request = {"TimePeriod": {"Start": start, "End": end},
               "Dimension": dimension, "Context": "COST_AND_USAGE", "Filter": expression}
    if billing_view_arn:
        request["BillingViewArn"] = billing_view_arn
    cache_key = ("ce_dimension_values", ce_region, json.dumps(request, sort_keys=True))
    if cache_key not in ctx.cache:
        values = set()
        for page in _pages(ctx, "get_dimension_values", ce_region, request):
            values.update(item["Value"] for item in page.get("DimensionValues", []))
        # Cache only complete responses after successful pagination.
        ctx.cache[cache_key] = sorted(values)
    return list(ctx.cache[cache_key])


def _resolve_regions(ctx, *, regions: list[str] | None, start: str, end: str,
                     expression: dict, ce_region: str,
                     billing_view_arn: str | None) -> list[str] | None:
    if regions is None or regions == ["all"]:
        return None
    selected = set(regions)
    if "all" in selected:
        raise ValueError("Use all by itself when selecting regions.")
    if "global" in selected:
        selected.remove("global")
        values = _dimension_values(
            ctx, dimension="REGION", start=start, end=end, expression=expression,
            ce_region=ce_region, billing_view_arn=billing_view_arn)
        selected.update(value for value in values
                        if value.strip().lower() in GLOBAL_REGION_VALUES)
    return sorted(selected)


def _service_filter(ctx, *, services: set[str], start: str, end: str,
                    expression: dict, ce_region: str,
                    billing_view_arn: str | None) -> dict | None:
    if "all" in services:
        if len(services) != 1:
            raise ValueError("Use all by itself when selecting services.")
        return None
    unknown = services - SERVICE_FILTERS.keys() - {"ebs"}
    if unknown or not services:
        raise ValueError("Invalid cost service selectors: " + ", ".join(sorted(unknown)))
    expressions = [deepcopy(SERVICE_FILTERS[name]) for name in sorted(services)
                   if name != "ebs"]
    if services.intersection({"ebs", "vpc"}) and "ec2" not in services:
        other_filter = _dimension("SERVICE", [EC2_OTHER])
        values = _dimension_values(
            ctx, dimension="USAGE_TYPE", start=start, end=end,
            expression=_and(expression, other_filter), ce_region=ce_region,
            billing_view_arn=billing_view_arn)
        matching = [value for value in values
                    if ("ebs" in services and "ebs:" in value.lower())
                    or ("vpc" in services and any(marker in value.lower()
                                                  for marker in ("natgateway", "vpcendpoint")))]
        if matching:
            expressions.append(_and(other_filter, _dimension("USAGE_TYPE", matching)))
    # Selecting EC2 already includes all EC2 - Other usage, so adding another
    # EBS/NAT expression would only duplicate existing filter coverage.
    return _or(expressions)


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


@lru_cache(maxsize=256)
def _period_hours(start: str, end: str) -> Decimal:
    """Reuse calendar interval calculations across thousands of grouped rows."""
    interval = _timestamp(end) - _timestamp(start)
    with localcontext() as decimal_context:
        decimal_context.prec = 38
        seconds = (Decimal(interval.days * 86400 + interval.seconds)
                   + Decimal(interval.microseconds) / Decimal(1_000_000))
        hours = seconds / Decimal(3600)
        if hours <= 0:
            raise ValueError("Cost Explorer returned a non-positive time interval.")
        return hours


def _hourly_average(amount: str, start: str, end: str) -> dict:
    try:
        cost = Decimal(amount)
        if not cost.is_finite():
            raise ValueError("Cost Explorer returned a non-finite amount.")
        with localcontext() as decimal_context:
            decimal_context.prec = 38
            hours = _period_hours(start, end)
            return {"amount": format(cost, "f"), "period_hours": format(hours, "f"),
                    "average_cost_per_hour": format(cost / hours, "f"),
                    "hourly_cost_basis": "period_average_not_unit_price"}
    except InvalidOperation as exc:
        raise ValueError("Cost Explorer returned an invalid amount.") from exc


def _validate(ctx, start: str, end: str, granularity: str, metric: str,
              group_by: list[str]) -> None:
    if not ctx.account_id:
        raise ValueError("The caller's account is required to query Cost Explorer.")
    if _timestamp(start) >= _timestamp(end):
        raise ValueError("The start must precede the exclusive end of the period.")
    if granularity not in {"DAILY", "MONTHLY", "HOURLY"}:
        raise ValueError("Invalid Cost Explorer granularity.")
    if metric not in COST_METRICS:
        raise ValueError("A monetary Cost Explorer metric is required.")
    if (not 1 <= len(group_by) <= 2 or len(set(group_by)) != len(group_by)
            or set(group_by) - SUPPORTED_GROUPS):
        raise ValueError("Cost Explorer requires one or two distinct supported dimensions.")


def _read_cost_rows(ctx, *, operation: str, ce_region: str, request: dict,
                    context: dict, resource_costs: bool) -> list[dict]:
    rows = []
    expected_groups = [group["Key"] for group in request["GroupBy"]]
    metric = request["Metrics"][0]
    for page in _pages(ctx, operation, ce_region, request):
        definitions = page.get("GroupDefinitions") or request["GroupBy"]
        group_keys = [definition["Key"] for definition in definitions]
        if group_keys != expected_groups:
            raise ValueError("Cost Explorer returned unexpected dimensions.")
        for interval in page.get("ResultsByTime", []):
            period = interval["TimePeriod"]
            # With GroupBy, consume Groups only; adding Total would double-count.
            # Never invent zero costs for an interval that contains no groups.
            for group in interval.get("Groups", []):
                values = group["Keys"]
                if len(values) != len(group_keys):
                    raise ValueError("Cost Explorer returned an incomplete group.")
                dimensions = dict(zip(group_keys, values))
                measurement = group.get("Metrics", {}).get(metric)
                if not measurement or "Amount" not in measurement or "Unit" not in measurement:
                    raise ValueError(f"Cost Explorer did not return the {metric} metric.")
                row = {"row_type": "resource_cost" if resource_costs else "cost_summary",
                       "cost_scope": "ec2_resource" if resource_costs else "account_aggregate",
                       "account_id": ctx.account_id,
                       "period_start": period["Start"], "period_end": period["End"],
                       "service": "ec2" if resource_costs else dimensions.get("SERVICE", ""),
                       "region": "", "metric": metric, "unit": measurement["Unit"],
                       "estimated": interval.get("Estimated"),
                       "query_context": context,
                       **_hourly_average(measurement["Amount"], period["Start"], period["End"])}
                if "REGION" in dimensions:
                    raw_region = dimensions["REGION"]
                    row["region"] = ("global" if raw_region.strip().lower() in GLOBAL_REGION_VALUES
                                     else raw_region)
                elif context["resolved_regions"] and len(context["resolved_regions"]) == 1:
                    value = context["resolved_regions"][0]
                    row["region"] = "global" if value.strip().lower() in GLOBAL_REGION_VALUES else value
                for index, (key, value) in enumerate(zip(group_keys, values), start=1):
                    row[f"group_{index}_key"] = key
                    row[f"group_{index}_value"] = value
                if resource_costs:
                    row["resource_id"] = dimensions["RESOURCE_ID"]
                    row["resource_type"] = "ec2_instance"
                    if not row["resource_id"]:
                        row["resource_type"] = "unallocated"
                        row["notes"] = "AWS returned EC2 Compute costs without a resource ID."
                rows.append(row)
    return rows


def _query_context(ctx, *, start: str, end: str, granularity: str, metric: str,
                   regions: list[str] | None, resolved_regions: list[str] | None,
                   services: set[str], expression: dict, group_by: list[str],
                   ce_region: str, billing_view_arn: str | None, operation: str) -> dict:
    return {"cost_source": f"ce:{operation}", "cost_account_scope": ctx.account_id,
            "cost_period_start": start, "cost_period_end_exclusive": end,
            "cost_granularity": granularity, "cost_metric": metric,
            "cost_requested_regions": regions, "resolved_regions": resolved_regions,
            "cost_requested_services": sorted(services), "cost_group_by": group_by,
            "cost_effective_filter": expression, "ce_region": ce_region,
            "billing_view_arn": billing_view_arn,
            "hourly_average_denominator": "calendar_hours_in_returned_time_interval",
            "cost_service_scope_notes": {
                "ec2": "Compute and all EC2 - Other usage, including EBS and networking.",
                "ebs": "Only USAGE_TYPE values containing EBS: under EC2 - Other.",
                "vpc": "Amazon VPC and NatGateway/VpcEndpoint USAGE_TYPE values under "
                       "EC2 - Other; this does not cover all networking or data transfer.",
                "eks_ecs": "The billing service does not automatically include EC2 nodes "
                           "or EBS volumes; costs are not allocated to individual clusters.",
            }}


def fetch_costs(ctx, *, start: str, end: str, granularity: str, metric: str,
                regions: list[str] | None, services: set[str], group_by: list[str],
                filter_expression: dict | None, ce_region: str,
                billing_view_arn: str | None = None) -> list[dict]:
    """Read complete groups for the period; errors never return partial cost data."""
    _validate(ctx, start, end, granularity, metric, group_by)
    if "RESOURCE_ID" in group_by:
        raise ValueError("For RESOURCE_ID, use fetch_ec2_resource_costs / --resource-costs.")
    base = _and(_dimension("LINKED_ACCOUNT", [ctx.account_id]), filter_expression)
    resolved = _resolve_regions(ctx, regions=regions, start=start, end=end,
                                expression=base, ce_region=ce_region,
                                billing_view_arn=billing_view_arn)
    if resolved == []:
        return []
    base = _and(base, _dimension("REGION", resolved) if resolved is not None else None)
    service_filter = _service_filter(ctx, services=services, start=start, end=end,
                                     expression=base, ce_region=ce_region,
                                     billing_view_arn=billing_view_arn)
    if service_filter is None and "all" not in services:
        # EBS discovery succeeded, but found no usage types in the requested period.
        return []
    expression = _and(base, service_filter)
    request = {"TimePeriod": {"Start": start, "End": end}, "Granularity": granularity,
               "Metrics": [metric], "Filter": expression,
               "GroupBy": [{"Type": "DIMENSION", "Key": key} for key in group_by]}
    if billing_view_arn:
        request["BillingViewArn"] = billing_view_arn
    context = _query_context(
        ctx, start=start, end=end, granularity=granularity, metric=metric,
        regions=regions, resolved_regions=resolved, services=services,
        expression=expression, group_by=group_by, ce_region=ce_region,
        billing_view_arn=billing_view_arn, operation="get_cost_and_usage")
    return _read_cost_rows(ctx, operation="get_cost_and_usage", ce_region=ce_region,
                           request=request, context=context, resource_costs=False)


def fetch_service_breakdowns(ctx, *, start: str, end: str, metric: str,
                             regions: list[str] | None, services: set[str],
                             filter_expression: dict | None, ce_region: str,
                             billing_view_arn: str | None = None) -> list[dict]:
    """Read optional monthly details without allocating aggregate costs to resources.

    EC2 instance-type costs contain Compute only. EBS costs are the discovered
    EBS usage subset of EC2 - Other, so they overlap the primary service totals.
    Each detail query is accepted only after all its pages succeed; one failed
    query does not discard another completed, independent detail query.
    """
    _validate(ctx, start, end, "MONTHLY", metric, ["REGION", "SERVICE"])
    if not services or services - SERVICE_FILTERS.keys() - {"all", "ebs"}:
        raise ValueError("Invalid cost service selectors.")
    if "all" in services and len(services) != 1:
        raise ValueError("Use all by itself when selecting services.")
    requested = []
    if services.intersection({"all", "ec2"}):
        requested.append(("ec2", "ec2_instance_type", ["REGION", "INSTANCE_TYPE"]))
    if services.intersection({"all", "ec2", "ebs"}):
        requested.append(("ebs", "ebs_region", ["REGION", "SERVICE"]))
    if not requested:
        return []

    def record_failure(service: str, scope: str, exc: Exception):
        code, message = error_message(exc)
        ctx.issue(service, ce_region, "get_cost_and_usage",
                  f"Monthly detail {scope} is unavailable: {message}", code=code)

    try:
        base = _and(_dimension("LINKED_ACCOUNT", [ctx.account_id]), filter_expression)
        resolved = _resolve_regions(
            ctx, regions=regions, start=start, end=end, expression=base,
            ce_region=ce_region, billing_view_arn=billing_view_arn)
        if resolved == []:
            return []
        base = _and(base, _dimension("REGION", resolved) if resolved is not None else None)
    except (*AWS_ERRORS, ValueError) as exc:
        for service, scope, _ in requested:
            record_failure(service, scope, exc)
        return []

    rows = []
    for service, scope, groups in requested:
        try:
            if service == "ec2":
                service_filter = _dimension("SERVICE", [EC2_COMPUTE])
                note = ("EC2 Compute only; excludes EBS and networking. Instance-type "
                        "groups overlap the regional EC2 Compute totals.")
            else:
                service_filter = _service_filter(
                    ctx, services={"ebs"}, start=start, end=end, expression=base,
                    ce_region=ce_region, billing_view_arn=billing_view_arn)
                if service_filter is None:
                    # Successful discovery without EBS usage is not a zero-cost group.
                    continue
                note = ("EBS USAGE_TYPE subset of EC2 - Other; overlaps that service's "
                        "regional totals. Do not add this detail to EC2 - Other.")
            expression = _and(base, service_filter)
            request = {"TimePeriod": {"Start": start, "End": end},
                       "Granularity": "MONTHLY", "Metrics": [metric],
                       "Filter": expression,
                       "GroupBy": [{"Type": "DIMENSION", "Key": key} for key in groups]}
            if billing_view_arn:
                request["BillingViewArn"] = billing_view_arn
            context = _query_context(
                ctx, start=start, end=end, granularity="MONTHLY", metric=metric,
                regions=regions, resolved_regions=resolved, services={service},
                expression=expression, group_by=groups, ce_region=ce_region,
                billing_view_arn=billing_view_arn, operation="get_cost_and_usage")
            context.update({"cost_breakdown_scope": scope,
                            "report_requested_services": sorted(services),
                            "overlaps_primary_cost_summary": True})
            context["cost_service_scope_notes"][service] = note
            detail_rows = _read_cost_rows(
                ctx, operation="get_cost_and_usage", ce_region=ce_region,
                request=request, context=context, resource_costs=False)
            for row in detail_rows:
                row.update({"row_type": "cost_breakdown", "service": service,
                            "cost_scope": scope, "notes": note})
                if service == "ec2":
                    row["instance_type"] = row["group_2_value"]
            rows.extend(detail_rows)
        except (*AWS_ERRORS, ValueError) as exc:
            record_failure(service, scope, exc)
    return rows


def fetch_ec2_resource_costs(ctx, *, start: str, end: str, metric: str,
                             regions: list[str] | None, filter_expression: dict | None,
                             ce_region: str, billing_view_arn: str | None = None) -> list[dict]:
    """Opt-in DAILY EC2 Compute view; the CLI enforces the last-14-days limit."""
    group_by = ["RESOURCE_ID", "REGION"]
    _validate(ctx, start, end, "DAILY", metric, group_by)
    base = _and(_dimension("LINKED_ACCOUNT", [ctx.account_id]),
                _dimension("SERVICE", [EC2_COMPUTE]), filter_expression)
    resolved = _resolve_regions(ctx, regions=regions, start=start, end=end,
                                expression=base, ce_region=ce_region,
                                billing_view_arn=billing_view_arn)
    if resolved == []:
        return []
    expression = _and(base, _dimension("REGION", resolved) if resolved is not None else None)
    request = {"TimePeriod": {"Start": start, "End": end}, "Granularity": "DAILY",
               "Metrics": [metric], "Filter": expression,
               "GroupBy": [{"Type": "DIMENSION", "Key": key} for key in group_by]}
    if billing_view_arn:
        request["BillingViewArn"] = billing_view_arn
    context = _query_context(
        ctx, start=start, end=end, granularity="DAILY", metric=metric,
        regions=regions, resolved_regions=resolved, services={"ec2"}, expression=expression,
        group_by=group_by, ce_region=ce_region, billing_view_arn=billing_view_arn,
        operation="get_cost_and_usage_with_resources")
    context["resource_cost_scope"] = "EC2 Compute only; excludes EBS and networking"
    return _read_cost_rows(ctx, operation="get_cost_and_usage_with_resources",
                           ce_region=ce_region, request=request, context=context,
                           resource_costs=True)
