"""Current AWS catalog prices without hard-coded rates or historic allocations.

Pricing dimensions stay separate (Hrs, GB-Mo, IOPS-Mo, ...).
A single hourly rate is assigned only when the catalog match is unambiguous.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .common import AWS_ERRORS, compact_json, error_message


HOUR_UNITS = {"hrs", "hours", "hour", "hr"}
ENGINE_NAMES = {
    "mysql": "MySQL", "postgres": "PostgreSQL", "mariadb": "MariaDB",
    "aurora": "Aurora MySQL", "aurora-mysql": "Aurora MySQL",
    "aurora-postgresql": "Aurora PostgreSQL", "oracle-ee": "Oracle",
    "oracle-se2": "Oracle", "sqlserver-ee": "SQL Server",
    "sqlserver-se": "SQL Server", "sqlserver-ex": "SQL Server",
    "sqlserver-web": "SQL Server",
}


def catalog_query(row: dict):
    """Return service code, filters, mode and note, or None when unsupported."""
    service, kind = row.get("service"), row.get("resource_type", "")
    details = row.get("details") or {}
    region = row.get("region")
    if service in {"elasticache", "opensearch", "redshift"} and row.get("instance_type"):
        code = {"elasticache": "AmazonElastiCache", "opensearch": "AmazonES", "redshift": "AmazonRedshift"}[service]
        attributes = {"regionCode": region, "instanceType": row["instance_type"]}
        if service == "elasticache":
            engine = {"redis": "Redis", "valkey": "Valkey", "memcached": "Memcached"}.get(row.get("engine", "").lower())
            if not engine:
                return None
            attributes["cacheEngine"] = engine
        return code, attributes, "hourly_instance", "Current node compute rate only; storage, backup, requests and transfer excluded."
    if service == "elb" and kind in {"load_balancer", "classic_load_balancer"}:
        load_type = details.get("type", "classic" if kind == "classic_load_balancer" else "")
        operation = {"application": "LoadBalancing:Application", "network": "LoadBalancing:Network", "gateway": "LoadBalancing:Gateway", "classic": "LoadBalancing"}.get(load_type)
        if operation:
            return "AWSELB", {"regionCode": region, "operation": operation}, "base_components", "Load balancer base hours only; capacity units, traffic and public IPv4 excluded."
    if service == "vpc":
        if kind == "nat_gateway" and str(details.get("AvailabilityMode", "zonal")).lower() != "zonal":
            row["price_status"] = "unsupported_regional_nat_quantity"
            return None
        specification = {
            "nat_gateway": ("AmazonEC2", "NAT Gateway"),
            "vpc_endpoint": ("AmazonVPC", "VpcEndpoint"),
            "elastic_ip": ("AmazonVPC", "IP Address"),
            "vpn_connection": ("AmazonVPC", "VPN Connection"),
        }.get(kind)
        if kind == "vpc_endpoint" and details.get("VpcEndpointType") != "Interface":
            return None
        if specification:
            code, family = specification
            return code, {"regionCode": region, "productFamily": family}, "base_components", "Only an unambiguous base hourly rate is modeled; data processing and transfer are excluded."
    if service == "ecs" and kind == "task" and details.get("launch_type") == "FARGATE":
        # Linux/x86 is an explicit condition, not a default inferred from missing metadata.
        if details.get("operating_system_family") != "LINUX" or details.get("cpu_architecture") != "X86_64":
            row["price_status"] = "missing_or_unsupported_fargate_platform"
            return None
        return "AmazonECS", {"regionCode": region, "productFamily": "Compute"}, "fargate_components", "Fargate Linux/x86 vCPU and memory only; extra ephemeral storage and discounts excluded."
    if service in {"ec2", "ebs", "rds"}:
        zone = row.get("availability_zone")
        if (details.get("outpost_arn") or details.get("outpost_arns") or
                (isinstance(zone, str) and zone and
                 not re.fullmatch(re.escape(region or "") + r"[a-z]", zone))):
            row["price_status"] = "unsupported_nonregional_location"
            return None
    if service == "ec2" and str(row.get("resource_id", "")).startswith("i-"):
        tenancy = details.get("tenancy", "default")
        if tenancy == "host" or details.get("product_codes"):
            row["price_status"] = "unsupported_host_or_marketplace"
            return None
        operation = details.get("usage_operation")
        attributes = {"regionCode": region, "instanceType": row.get("instance_type"),
                      "productFamily": "Compute Instance", "capacitystatus": "Used",
                      "tenancy": "Dedicated" if tenancy == "dedicated" else "Shared"}
        if operation:
            attributes["operation"] = operation
        else:
            platform = details.get("platform_details", "")
            os_name = {"Linux/UNIX": "Linux", "Windows": "Windows",
                       "Red Hat Enterprise Linux": "RHEL", "SUSE Linux": "SUSE"}.get(platform)
            if not os_name:
                row["price_status"] = "unknown_operating_system"
                return None
            attributes.update(operatingSystem=os_name, preInstalledSw="NA")
        lifecycle = details.get("instance_lifecycle", "on-demand")
        note = ("On-Demand compute list price, excluding EBS, networking and discounts. "
                "This is neither a billed cost nor a historical price.")
        if lifecycle == "spot":
            note += " Spot instance: this is an On-Demand comparison, not the Spot rate."
        return "AmazonEC2", attributes, "hourly_instance", note
    if service == "ebs" and str(row.get("resource_id", "")).startswith("vol-"):
        return ("AmazonEC2", {"regionCode": region, "volumeApiName": row.get("storage_type")},
                "storage_components", "EBS unit prices with separate tiers and components. "
                "Do not add capacity, IOPS and throughput rates; no hourly conversion is applied.")
    if service == "rds" and row.get("instance_type") and row.get("engine"):
        engine = row["engine"]
        if row.get("cluster_name") and engine in {"mysql", "postgres"}:
            row["price_status"] = "unsupported_multi_az_db_cluster"
            return None
        if engine not in ENGINE_NAMES:
            row["price_status"] = "unsupported_database_engine"
            return None
        aurora = engine in {'aurora', 'aurora-mysql', 'aurora-postgresql'}
        if not aurora and not isinstance(row.get('multi_az'), bool):
            row['price_status'] = 'missing_database_deployment'
            return None
        attributes = {"regionCode": region, "productFamily": "Database Instance",
                      "instanceType": row["instance_type"], "databaseEngine": ENGINE_NAMES[engine],
                      "deploymentOption": "Multi-AZ" if not aurora and row.get("multi_az") is True else "Single-AZ"}
        license_model = details.get("license_model")
        if license_model == "license-included":
            attributes["licenseModel"] = "License included"
        elif license_model == "bring-your-own-license":
            attributes["licenseModel"] = "Bring your own license"
        edition = {"oracle-ee": "Enterprise", "oracle-se2": "Standard Two",
                   "sqlserver-ee": "Enterprise", "sqlserver-se": "Standard",
                   "sqlserver-ex": "Express", "sqlserver-web": "Web"}.get(engine)
        if edition:
            attributes["databaseEdition"] = edition
        return ("AmazonRDS", attributes, "hourly_instance",
                "On-Demand DB instance list price; storage, backups, I/O and discounts excluded. "
                "Multiple catalog configurations remain explicit alternatives.")
    if service == "eks" and kind in {"cluster", "eks_cluster"}:
        return ("AmazonEKS", {"regionCode": region}, "catalog_components",
                "Shared EKS regional catalog components and alternatives, including support "
                "tiers and Auto Mode when published. Quotes appear once per region under "
                "eks_regional_catalog; no tier is automatically assigned to this cluster. "
                "EC2 nodes, volumes and networking are priced separately.")
    return None


def _iter_products(ctx, service_code: str, attributes: dict, endpoint: str) -> Iterator[dict]:
    """Decode a page at a time instead of retaining the entire raw catalog."""
    filters = [{"Type": "TERM_MATCH", "Field": field, "Value": str(value)}
               for field, value in sorted(attributes.items())]
    seen = set()
    parameters = {"ServiceCode": service_code, "Filters": filters, "MaxResults": 100,
                  "FormatVersion": "aws_v1"}
    while True:
        response = ctx.call("pricing", "get_products", endpoint, **parameters)
        for value in response.get("PriceList", []):
            product = json.loads(value) if isinstance(value, str) else value
            if not isinstance(product, dict):
                raise ValueError("AWS Price List returned a non-object product.")
            yield product
        token = response.get("NextToken")
        if not token:
            return
        if token in seen:
            raise ValueError("Repeated AWS Price List pagination token.")
        seen.add(token)
        parameters["NextToken"] = token


def get_products(ctx, service_code: str, attributes: dict, endpoint: str) -> list[dict]:
    """Return all matching products for callers that need the raw catalog."""
    return list(_iter_products(ctx, service_code, attributes, endpoint))


def extract_dimensions(products: Iterable[dict], mode: str) -> list[dict]:
    unique = {}
    now = datetime.now(timezone.utc)
    for product in products:
        info = product.get("product", {})
        attributes = info.get("attributes", {})
        for term in product.get("terms", {}).get("OnDemand", {}).values():
            effective = term.get("effectiveDate", "")
            if effective:
                instant = datetime.fromisoformat(effective.replace("Z", "+00:00"))
                if instant.tzinfo is None:
                    instant = instant.replace(tzinfo=timezone.utc)
                if instant > now:
                    continue
            for rate in term.get("priceDimensions", {}).values():
                unit = rate.get("unit", "")
                if mode == "hourly_instance" and unit.lower() not in HOUR_UNITS:
                    continue
                for currency, amount in rate.get("pricePerUnit", {}).items():
                    value = Decimal(amount)
                    if not value.is_finite() or value < 0:
                        raise ValueError("AWS Price List returned a non-finite or negative rate.")
                    dimension = {"price_effective_date": effective,
                                  "price_sku": info.get("sku", ""),
                                  "price_description": rate.get("description", ""),
                                  "price_per_unit": value, "price_unit": unit,
                                  "price_currency": currency,
                                  "price_begin_range": rate.get("beginRange", ""),
                                  "price_end_range": rate.get("endRange", ""),
                                  "price_component": info.get("productFamily", ""),
                                  "details": {"catalog_attributes": attributes,
                                              "rate_code": rate.get("rateCode"),
                                              "applies_to": rate.get("appliesTo", [])}}
                    # Deduplicate while streaming, including duplicate page entries.
                    unique.setdefault(compact_json(dimension), dimension)
    return [unique[key] for key in sorted(unique)]


def add_prices(ctx, inventory: list[dict], endpoint: str) -> list[dict]:
    quotes = []
    cache = {}
    emitted_catalogs = set()
    for row in inventory:
        query = catalog_query(row)
        if query is None:
            row.setdefault("price_status", "not_supported_or_not_independently_priced")
            continue
        service_code, attributes, mode, note = query
        row["notes"] = " ".join(part for part in (row.get("notes", ""), note) if part)
        if any(value is None or value == "" for value in attributes.values()):
            row["price_status"] = "missing_catalog_attributes"
            continue
        cache_key = (service_code, compact_json(attributes), mode)
        if cache_key not in cache:
            try:
                products = _iter_products(ctx, service_code, attributes, endpoint)
                cache[cache_key] = extract_dimensions(products, mode)
            except (*AWS_ERRORS, ValueError, InvalidOperation) as exc:
                code, message = error_message(exc)
                ctx.issue("pricing", row["region"], "get_products", message,
                          resource_id=row["resource_id"], code=code)
                cache[cache_key] = None
        rates = cache[cache_key]
        row["price_source"] = "AWS Price List Query API"
        row["price_checked_at_utc"] = ctx.generated_at
        row["price_term"] = "OnDemand"
        if rates is None:
            row["price_status"] = "api_error"
            continue
        if not rates:
            row["price_status"] = "no_matching_public_price"
            continue
        if mode == "hourly_instance":
            unambiguous = (len(rates) == 1 and
                           rates[0]["price_begin_range"] in {"0", "0.0"} and
                           rates[0]["price_end_range"].lower() in {"inf", "infinity"} and
                           not rates[0]["details"].get("applies_to"))
            status = "matched" if unambiguous else "ambiguous_catalog_candidates"
        else:
            unambiguous = False
            status = mode
        row["price_status"] = status
        if unambiguous:
            rate = rates[0]
            row.update({key: value for key, value in rate.items() if key != "details"})
            row["list_price_per_hour"] = rate["price_per_unit"]
            row["price_quantity"] = 1
        # A regional catalog is shared data. Repeating every alternative on every
        # cluster makes exports grow as clusters x catalog size and suggests an
        # attribution that the APIs have not established.
        shared_catalog = mode == "catalog_components"
        if shared_catalog:
            if cache_key in emitted_catalogs:
                continue
            emitted_catalogs.add(cache_key)
        for rate in rates:
            quote = {key: row[key] for key in (
                "account_id", "region", "service", "resource_type", "resource_id", "resource_arn",
                "resource_name", "name_source", "instance_type", "cluster_name") if key in row}
            if shared_catalog:
                quote = {"account_id": row.get("account_id", ctx.account_id),
                         "region": row["region"], "service": row["service"],
                         "resource_type": "eks_regional_catalog",
                         "resource_id": f"{service_code}:{row['region']}",
                         "resource_name": "Amazon EKS regional catalog",
                         "name_source": "catalog"}
            quote.update(row_type="price_quote", cost_scope="current_public_catalog_not_billed_cost",
                         price_source="AWS Price List Query API", price_status=status,
                         price_checked_at_utc=ctx.generated_at, price_term="OnDemand", notes=note,
                         query_context={"pricing_service_code": service_code,
                                        "pricing_filters": attributes,
                                        "pricing_scope": "regional_catalog_alternatives"
                                        if shared_catalog else "resource_configuration"}, **rate)
            if unambiguous:
                quote["list_price_per_hour"] = rate["price_per_unit"]
                quote["price_quantity"] = 1
            quotes.append(quote)
    return quotes
