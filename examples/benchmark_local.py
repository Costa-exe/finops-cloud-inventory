"""Measure synthetic regional I/O overlap and CSV serialization without AWS.

This is a reproducible local benchmark, not a forecast of real AWS performance.
The network guard used by the integration simulation stays active throughout.
"""

from __future__ import annotations

import argparse
import io
import json
import platform
import tempfile
import time
from contextlib import redirect_stderr
from pathlib import Path
from threading import Lock
from unittest.mock import patch

import costcsv
from aws_inventory import report
from aws_inventory.common import InventoryContext
from examples.simulate_export import ACCOUNT, GENERATED, offline_environment


def measure_regions(workers, *, region_count, latency_seconds):
    active = maximum_active = 0
    lock = Lock()

    def collector(service):
        def collect(ctx, region):
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                time.sleep(latency_seconds)
                ctx.emit(service, "synthetic_resource", f"{service}:{region}", region)
            finally:
                with lock:
                    active -= 1
        return collect

    regions = [f"simulated-region-{index:02d}" for index in range(region_count)]
    services = {"eks", "ec2", "ebs"}
    ctx = InventoryContext(None, ACCOUNT, "aws", GENERATED, services, set(regions), False)
    collectors = {service: collector(service) for service in services}
    with (patch.dict(costcsv.COLLECTORS, collectors, clear=True),
          patch.dict(costcsv.REGIONAL_COLLECTORS, {}, clear=True),
          redirect_stderr(io.StringIO())):
        started = time.perf_counter()
        coverage = costcsv.collect_regions(ctx, regions, services, workers)
        elapsed = time.perf_counter() - started
    return {"workers": workers, "elapsed_seconds": round(elapsed, 6),
            "maximum_concurrent_collectors": maximum_active,
            "inventory_rows": len(ctx.rows), "coverage_rows": len(coverage)}, ctx.rows


def measure_csv(row_count):
    context_serializations = 0
    original_json = report.compact_json

    def record_json(value):
        nonlocal context_serializations
        if isinstance(value, dict) and value.get("benchmark_data"):
            context_serializations += 1
        return original_json(value)

    rows = [{"row_type": "inventory", "account_id": ACCOUNT, "region": "eu-west-1",
             "service": "ec2", "resource_type": "instance", "resource_id": f"i-demo-{index:08d}",
             "resource_name": f"synthetic-worker-{index}", "instance_type": "m6i.large",
             "storage_gib": 100, "tags": {"Name": f"synthetic-worker-{index}"},
             "details": {"simulation_only": True, "private_ip": "192.0.2.1"}}
            for index in range(row_count)]
    with tempfile.TemporaryDirectory(prefix="costcsv-benchmark-") as directory:
        path = Path(directory) / "synthetic.csv"
        with patch("aws_inventory.report.compact_json", record_json):
            started = time.perf_counter()
            report.write_report(path, rows, generated_at=GENERATED,
                                context={"benchmark_data": True, "simulation_only": True},
                                partial=False, excel=True, overwrite=False)
            elapsed = time.perf_counter() - started
        size = path.stat().st_size
    return {"rows": row_count, "elapsed_seconds": round(elapsed, 6), "bytes": size,
            "shared_query_context_serializations": context_serializations}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regions", type=int, default=8)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--latency-ms", type=float, default=10)
    parser.add_argument("--csv-rows", type=int, default=10000)
    args = parser.parse_args(argv)
    if not (1 <= args.regions <= 64 and 1 <= args.workers <= 32 and
            0 <= args.latency_ms <= 100 and 1 <= args.csv_rows <= 100000):
        parser.error("Use regions 1-64, workers 1-32, latency-ms 0-100 and csv-rows 1-100000.")
    with offline_environment() as network_attempts:
        options = {"region_count": args.regions, "latency_seconds": args.latency_ms / 1000}
        sequential, first = measure_regions(1, **options)
        parallel, second = measure_regions(args.workers, **options)
        if first != second:
            raise AssertionError("Parallel collection changed inventory content or ordering")
        csv_result = measure_csv(args.csv_rows)
        result = {"simulation_only": True, "python": platform.python_version(),
                  "warning": "Synthetic I/O latency; not a real AWS speed benchmark or security audit.",
                  "parameters": vars(args), "sequential": sequential, "parallel": parallel,
                  "same_inventory_content_and_order": True,
                  "synthetic_io_speedup": round(sequential["elapsed_seconds"] /
                                                parallel["elapsed_seconds"], 2),
                  "csv_export": csv_result, "network_attempts": len(network_attempts)}
        if network_attempts:
            raise AssertionError("Benchmark attempted network access")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
