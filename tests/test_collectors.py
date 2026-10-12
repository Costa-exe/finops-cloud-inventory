"""Offline regression tests for batching, partial caches, and regional concurrency."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from threading import Barrier, Event, Lock
from unittest.mock import patch

import costcsv
from aws_inventory.common import InventoryContext
from aws_inventory.inventory_compute import collect_ec2, collect_eks, collect_elb
from examples.simulate_export import ACCOUNT, GENERATED, WEST, offline_environment


def _context(services, regions=None):
    # No SDK session exists: unexpected client use cannot reach AWS.
    return InventoryContext(None, ACCOUNT, "aws", GENERATED, set(services),
                            set(regions or [WEST]), False)


class CollectorOptimizationTests(unittest.TestCase):
    def test_elb_batches_21_resources_and_matches_reversed_tag_responses(self):
        variants = (
            ("elbv2", "LoadBalancerArn", "load_balancer", "ResourceArns", "ResourceArn"),
            ("elbv2", "TargetGroupArn", "target_group", "ResourceArns", "ResourceArn"),
            ("elb", "LoadBalancerName", "classic_load_balancer", "LoadBalancerNames",
             "LoadBalancerName"),
        )
        for sdk, id_key, resource_type, parameter, response_key in variants:
            with self.subTest(resource_type=resource_type), offline_environment() as attempts:
                ctx = _context({"elb"})
                identifiers = [f"resource-{number:02d}" for number in range(21)]
                tag_calls = []

                def pages(service, operation, region, result_key, **kwargs):
                    expected = "TargetGroups" if resource_type == "target_group" else (
                        "LoadBalancerDescriptions" if sdk == "elb" else "LoadBalancers")
                    if service == sdk and result_key == expected:
                        return [{id_key: identifier} for identifier in identifiers]
                    return []

                def call(service, operation, region, **kwargs):
                    if operation == "describe_target_health":
                        return {"TargetHealthDescriptions": []}
                    self.assertEqual((service, operation, region), (sdk, "describe_tags", WEST))
                    self.assertEqual(set(kwargs), {parameter})
                    batch = kwargs[parameter]
                    tag_calls.append(list(batch))
                    return {"TagDescriptions": [
                        {response_key: identifier,
                         "Tags": [{"Key": "owner", "Value": f"owner-of-{identifier}"}]}
                        for identifier in reversed(batch)
                    ]}

                with patch.object(ctx, "safe_pages", side_effect=pages), \
                        patch.object(ctx, "safe_call", side_effect=call):
                    collect_elb(ctx, WEST)

                self.assertEqual([len(batch) for batch in tag_calls], [20, 1])
                self.assertEqual([item for batch in tag_calls for item in batch], identifiers)
                self.assertEqual(len(ctx.rows), 21)
                for row in ctx.rows:
                    self.assertEqual(row["resource_type"], resource_type)
                    self.assertEqual(row["tags"], {"owner": f"owner-of-{row['resource_id']}"})
                self.assertEqual(attempts, [])

    def test_partial_asg_listing_is_shared_across_nodegroups_and_marks_ec2_partial(self):
        ctx = _context({"eks", "ec2"})
        groups = [
            {"AutoScalingGroupName": f"asg-{suffix}", "Instances": [{"InstanceId": f"i-{suffix}"}]}
            for suffix in ("a", "b")
        ]
        asg_reads = []

        def pages(service, operation, region, result_key, **kwargs):
            if operation == "describe_auto_scaling_groups":
                self.assertEqual(kwargs, {})
                asg_reads.append(region)
                # A later page fails; already-read groups must remain usable.
                ctx.issue(service, region, operation, "Synthetic later-page failure", code="AccessDenied")
                return groups
            if operation == "list_clusters":
                return ["demo"]
            if operation == "list_nodegroups":
                return ["nodes-a", "nodes-b"]
            if operation == "describe_instances":
                return [{"Instances": [{"InstanceId": "i-a"}, {"InstanceId": "i-b"}]}]
            return []

        def call(service, operation, region, **kwargs):
            if operation == "describe_cluster":
                return {"cluster": {"name": "demo"}}
            self.assertEqual(operation, "describe_nodegroup")
            suffix = kwargs["nodegroupName"][-1]
            return {"nodegroup": {"nodegroupName": kwargs["nodegroupName"],
                                  "resources": {"autoScalingGroups": [{"name": f"asg-{suffix}"}]}}}

        with offline_environment() as attempts, redirect_stderr(io.StringIO()), \
                patch.object(ctx, "safe_pages", side_effect=pages), \
                patch.object(ctx, "safe_call", side_effect=call):
            eks_coverage = costcsv.run_collector(ctx, "eks", WEST, collect_eks)
            ec2_coverage = costcsv.run_collector(ctx, "ec2", WEST, collect_ec2)

        self.assertEqual(asg_reads, [WEST])
        self.assertEqual((eks_coverage["state"], ec2_coverage["state"]), ("partial", "partial"))
        self.assertEqual([(issue["service"], issue["error_code"]) for issue in ctx.issues],
                         [("autoscaling", "AccessDenied"), ("ec2", "IncompleteCachedInventory")])
        nodegroups = [row for row in ctx.rows if row["resource_type"] == "nodegroup"]
        self.assertEqual(len(nodegroups), 2)
        for row, suffix in zip(nodegroups, ("a", "b")):
            self.assertEqual(row["details"]["instance_ids"], [f"i-{suffix}"])
        instances = [row for row in ctx.rows if row["resource_type"] == "instance"]
        self.assertEqual(len(instances), 2)
        self.assertEqual([row["cluster_name"] for row in instances], [["demo"], ["demo"]])
        self.assertEqual(attempts, [])

    def test_regional_workers_are_bounded_ordered_and_isolate_dependency_caches(self):
        regions = ["region-3", "region-1", "region-4", "region-2"]
        selected = {"eks", "ec2", "ebs"}
        ctx = _context(selected, regions)
        ctx.cache["parent_only"] = "must not reach child collectors"
        barrier = Barrier(2)
        lock = Lock()
        finished = {region: Event() for region in regions}
        contexts = {}
        completion_order = []
        active = 0
        peak = 0

        def handler(service):
            def collect(child, region):
                nonlocal active, peak
                if service == "eks":
                    self.assertEqual(child.cache, {})
                    child.cache.update(owner=region, chain=[])
                    with lock:
                        contexts[region] = child
                        active += 1
                        peak = max(peak, active)
                    # If regional work becomes sequential, this fails instead of silently passing.
                    barrier.wait(timeout=5)
                    index = regions.index(region)
                    if index % 2 == 0:
                        self.assertTrue(finished[regions[index + 1]].wait(timeout=5))

                self.assertEqual(child.cache["owner"], region)
                expected = {"eks": [], "ec2": ["eks"], "ebs": ["eks", "ec2"]}[service]
                self.assertEqual(child.cache["chain"], expected)
                child.cache["chain"].append(service)
                child.emit(service, "test_resource", f"{region}/{service}", region)

                if service == "ebs":
                    with lock:
                        active -= 1
                        completion_order.append(region)
                    finished[region].set()
            return collect

        collectors = {service: handler(service) for service in selected}
        with offline_environment() as attempts, redirect_stderr(io.StringIO()), \
                patch.object(costcsv, "COLLECTORS", collectors), \
                patch.object(costcsv, "REGIONAL_COLLECTORS", {}):
            coverage = costcsv.collect_regions(ctx, regions, selected, workers=2)

        self.assertEqual(peak, 2)
        self.assertEqual(active, 0)
        self.assertEqual(completion_order[0], regions[1])
        expected_order = [(region, service) for region in regions for service in ("eks", "ec2", "ebs")]
        self.assertEqual([(row["region"], row["service"]) for row in ctx.rows], expected_order)
        self.assertEqual([(row["region"], row["service"]) for row in coverage], expected_order)
        self.assertTrue(all(row["state"] == "complete" and row["resource_count"] == 1 for row in coverage))
        self.assertEqual(len({id(child.cache) for child in contexts.values()}), len(regions))
        self.assertTrue(all(child.cache == {} for child in contexts.values()))
        self.assertEqual(ctx.cache, {"parent_only": "must not reach child collectors"})
        self.assertEqual(attempts, [])


if __name__ == "__main__":
    unittest.main()
