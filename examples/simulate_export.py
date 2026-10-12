"""Run the real CLI against synthetic, schema-checked AWS responses.

No AWS account, credentials, CLI, Docker, or network access is needed. Every
SDK request must exactly match a queued botocore Stubber response. Socket and
HTTP guards remain active during client construction, CLI execution, and cleanup.
All costs and catalog rates below are fabricated test data, not AWS quotations.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
from collections import Counter
from contextlib import ExitStack, chdir, contextmanager, redirect_stderr
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import boto3
from botocore.config import Config
from botocore.stub import Stubber

import costcsv


ACCOUNT = "123456789012"
WEST = "eu-west-1"
CENTRAL = "eu-central-1"
START, END = "2026-09-01", "2026-10-01"
BUNDLE_START = "2026-07-01"
GENERATED = "2026-10-11T12:00:00+00:00"
CLUSTER = "demo-platform"
NODEGROUP = "general-workers"
ASG = "demo-platform-general-workers-asg"
NODE_A, NODE_B = "i-00000000000000001", "i-00000000000000002"
STANDALONE = "i-00000000000000003"
ROOT_A, DATA_A, ROOT_B = ("vol-00000000000000001", "vol-00000000000000002",
                        "vol-00000000000000003")
ROOT_STANDALONE, UNATTACHED = "vol-00000000000000004", "vol-00000000000000005"
EC2_COMPUTE = "Amazon Elastic Compute Cloud - Compute"
EKS_SERVICE = "Amazon Elastic Kubernetes Service"
COST_MONTHS = (
    ("2026-07-01", "2026-08-01", (
        (EC2_COMPUTE, CENTRAL, "15.00"), ("EC2 - Other", CENTRAL, "1.60"),
        (EC2_COMPUTE, WEST, "160.00"), ("EC2 - Other", WEST, "10.12"),
        (EKS_SERVICE, WEST, "74.40"),
    )),
    ("2026-08-01", "2026-09-01", (
        (EC2_COMPUTE, CENTRAL, "16.00"), ("EC2 - Other", CENTRAL, "2.10"),
        (EC2_COMPUTE, WEST, "170.00"), ("EC2 - Other", WEST, "11.12"),
        (EKS_SERVICE, WEST, "74.40"),
    )),
    (START, END, (
        (EC2_COMPUTE, CENTRAL, "18.00"), ("EC2 - Other", CENTRAL, "-1.20"),
        (EC2_COMPUTE, WEST, "172.80"), ("EC2 - Other", WEST, "13.12"),
        (EKS_SERVICE, WEST, "72.00"),
    )),
)
PREVIEW_FIELDS = (
    "simulation_only", "row_type", "region", "service", "resource_type", "resource_id", "resource_name",
    "name_source", "cluster_name", "instance_type", "storage_gib", "attached_to",
    "amount", "unit", "period_hours", "average_cost_per_hour", "list_price_per_hour",
    "price_per_unit", "price_unit", "price_currency", "price_status",
)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        instant = cls.fromisoformat(GENERATED)
        return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)


class NetworkForbidden(AssertionError):
    """Raised before any simulation network request can leave the process."""


@contextmanager
def offline_environment():
    """Ignore local AWS profiles and block DNS, sockets, and botocore HTTP."""
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append("blocked network operation")
        raise NetworkForbidden("Network access is forbidden in the offline simulation")

    environment = {
        "AWS_ACCESS_KEY_ID": "FAKE_ACCESS_KEY_FOR_OFFLINE_TESTS",
        "AWS_SECRET_ACCESS_KEY": "FAKE_SECRET_KEY_FOR_OFFLINE_TESTS",
        "AWS_SESSION_TOKEN": "FAKE_SESSION_TOKEN_FOR_OFFLINE_TESTS",
        "AWS_DEFAULT_REGION": WEST,
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
    }
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, environment, clear=True))
        for target in ("socket.socket.connect", "socket.socket.connect_ex",
                       "socket.socket.sendto", "socket.create_connection",
                       "socket.getaddrinfo", "botocore.httpsession.URLLib3Session.send"):
            stack.enter_context(patch(target, forbidden))
        yield attempts


def _instance(identifier, region, instance_type, volume_ids, name=None):
    instance = {
        "InstanceId": identifier, "InstanceType": instance_type,
        "State": {"Name": "running", "Code": 16},
        "Placement": {"AvailabilityZone": region + "a", "Tenancy": "default"},
        "VpcId": "vpc-00000000000000001", "SubnetId": "subnet-00000000000000001",
        "PrivateIpAddress": {NODE_A: "10.0.1.10", NODE_B: "10.0.1.11",
                             STANDALONE: "10.1.1.10"}[identifier],
        "SecurityGroups": [{"GroupId": "sg-00000000000000001", "GroupName": "demo"}],
        "PlatformDetails": "Linux/UNIX", "UsageOperation": "RunInstances",
        "Architecture": "x86_64", "RootDeviceType": "ebs", "RootDeviceName": "/dev/xvda",
        "ImageId": "ami-00000000000000001", "EbsOptimized": True,
        "LaunchTime": datetime(2026, 8, 15, tzinfo=timezone.utc),
        "BlockDeviceMappings": [
            {"DeviceName": "/dev/xvda" if index == 0 else "/dev/sdf",
             "Ebs": {"VolumeId": volume_id, "Status": "attached",
                     "DeleteOnTermination": index == 0}}
            for index, volume_id in enumerate(volume_ids)
        ],
    }
    if name:
        instance["Tags"] = [{"Key": "Name", "Value": name}]
    return instance


def _volume(identifier, region, size, instance_id=None, device="/dev/xvda"):
    return {
        "VolumeId": identifier, "Size": size, "VolumeType": "gp3",
        "AvailabilityZone": region + "a", "Encrypted": True,
        "Iops": 3000, "Throughput": 125,
        "State": "in-use" if instance_id else "available",
        "CreateTime": datetime(2026, 8, 15, tzinfo=timezone.utc),
        "Attachments": ([{"InstanceId": instance_id, "VolumeId": identifier,
                          "Device": device, "State": "attached",
                          "DeleteOnTermination": device == "/dev/xvda"}]
                        if instance_id else []),
    }


def _product(sku, family, unit, rate, **attributes):
    return json.dumps({
        "product": {"sku": sku, "productFamily": family, "attributes": attributes},
        "terms": {"OnDemand": {sku + ".SYNTHETIC": {
            "effectiveDate": "2026-09-01T00:00:00Z",
            "priceDimensions": {sku + ".RATE": {
                "rateCode": sku + ".RATE", "description": "SYNTHETIC TEST RATE: " + family,
                "unit": unit, "beginRange": "0", "endRange": "Inf",
                "pricePerUnit": {"USD": rate},
            }},
        }}},
    })


class SimulatedAWS:
    """Strict session facade exposing only pre-created, fully stubbed clients."""

    region_name = WEST
    profile_name = "offline-simulation"

    def __init__(self, *, deny_snapshots=False, custom_filter=None, report_format="raw-csv", deny_costs=False):
        if report_format not in {"raw-csv", "bundle", "html"}:
            raise ValueError(f"Unsupported simulation format: {report_format}")
        self.report_format = report_format
        self.period_start = BUNDLE_START if report_format in {"bundle", "html"} else START
        self.clients = {}
        self.stubbers = {}
        self.calls = []
        self.session_requests = []
        real_session = boto3.Session(
            aws_access_key_id="FAKE_ACCESS_KEY_FOR_OFFLINE_TESTS",
            aws_secret_access_key="FAKE_SECRET_KEY_FOR_OFFLINE_TESTS",
            aws_session_token="FAKE_SESSION_TOKEN_FOR_OFFLINE_TESTS", region_name=WEST,
        )
        for service, region in self.client_specifications():
            client = real_session.client(
                service, region_name=region,
                config=Config(retries={"total_max_attempts": 1}, connect_timeout=1, read_timeout=1),
            )

            def record(params, model, _service=service, _region=region, **kwargs):
                self.calls.append({"service": _service, "region": _region,
                                   "operation": model.name, "parameters": deepcopy(params)})

            client.meta.events.register("before-parameter-build.*.*", record)
            self.clients[(service, region)] = client
            self.stubbers[(service, region)] = Stubber(client)
        self._identity()
        self._costs(custom_filter, deny_costs=deny_costs)
        if report_format == "bundle":
            self._service_breakdowns(custom_filter)
        self._inventory(deny_snapshots)
        self._pricing()

    def client_specifications(self):
        return ([("sts", WEST), ("ce", "us-east-1"), ("pricing", "us-east-1")]
                + [(service, region) for region in (CENTRAL, WEST)
                   for service in ("ec2", "eks", "autoscaling")])

    def __enter__(self):
        for stubber in self.stubbers.values():
            stubber.activate()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.assert_consumed()
        finally:
            for stubber in self.stubbers.values():
                stubber.deactivate()
            for client in self.clients.values():
                client.close()

    def client(self, service, region_name=None, **kwargs):
        key = service, region_name or self.region_name
        self.session_requests.append(key)
        if key not in self.clients:
            raise AssertionError(f"No simulated AWS client is configured for {key!r}")
        return self.clients[key]

    def assert_consumed(self):
        for key, stubber in self.stubbers.items():
            try:
                stubber.assert_no_pending_responses()
            except AssertionError as exc:
                raise AssertionError(f"Unused simulated responses for {key}: {exc}") from exc

    def add(self, service, region, operation, response, expected):
        self.stubbers[(service, region)].add_response(operation, response, expected)

    def _identity(self):
        self.add("sts", WEST, "get_caller_identity", {
            "Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/offline-simulation",
            "UserId": "AIDAOFFLINESIMULATION",
        }, {})

    def _costs(self, custom_filter, *, deny_costs=False):
        account_filter = {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [ACCOUNT]}}
        if custom_filter:
            account_filter = {"And": [account_filter, deepcopy(custom_filter)]}
        region_filter = {"Dimensions": {"Key": "REGION", "Values": [CENTRAL, WEST]}}
        service_filter = {"Or": [
            {"Dimensions": {"Key": "SERVICE", "Values": [EC2_COMPUTE, "EC2 - Other"]}},
            {"Dimensions": {"Key": "SERVICE", "Values": [
                "Amazon Elastic Container Service for Kubernetes", EKS_SERVICE]}},
        ]}
        request = {
            "TimePeriod": {"Start": self.period_start, "End": END}, "Granularity": "MONTHLY",
            "Metrics": ["AmortizedCost"],
            "GroupBy": [{"Type": "DIMENSION", "Key": key} for key in ("SERVICE", "REGION")],
            "Filter": {"And": [{"And": [account_filter, region_filter]}, service_filter]},
        }
        self.expected_cost_filter = deepcopy(request["Filter"])
        if deny_costs:
            self.stubbers[("ce", "us-east-1")].add_client_error(
                "get_cost_and_usage", service_error_code="AccessDeniedException",
                service_message="Synthetic billing access denied", expected_params=request)
            return

        months = COST_MONTHS if self.report_format in {"bundle", "html"} else COST_MONTHS[-1:]

        def page(value_slice, token=None):
            response = {
                "GroupDefinitions": request["GroupBy"],
                "ResultsByTime": [{
                    "TimePeriod": {"Start": start, "End": end}, "Estimated": False,
                    "Total": {},
                    "Groups": [{"Keys": [service, region], "Metrics": {
                        "AmortizedCost": {"Amount": amount, "Unit": "USD"}}}
                        for service, region, amount in values[value_slice]],
                } for start, end, values in months],
            }
            if token:
                response["NextPageToken"] = token
            return response

        self.add("ce", "us-east-1", "get_cost_and_usage",
                 page(slice(None, 3), "cost-page-2"), request)
        self.add("ce", "us-east-1", "get_cost_and_usage",
                 page(slice(3, None)), {**request, "NextPageToken": "cost-page-2"})

    def _service_breakdowns(self, custom_filter):
        account_filter = {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [ACCOUNT]}}
        if custom_filter:
            account_filter = {"And": [account_filter, deepcopy(custom_filter)]}
        base = {"And": [account_filter,
                        {"Dimensions": {"Key": "REGION", "Values": [CENTRAL, WEST]}}]}
        period = {"Start": BUNDLE_START, "End": END}

        def breakdown_request(expression, group_by):
            return {"TimePeriod": period, "Granularity": "MONTHLY", "Metrics": ["AmortizedCost"],
                    "Filter": expression,
                    "GroupBy": [{"Type": "DIMENSION", "Key": key} for key in group_by]}

        def breakdown_response(request, service, key):
            return {
                "GroupDefinitions": request["GroupBy"],
                "ResultsByTime": [{
                    "TimePeriod": {"Start": start, "End": end}, "Estimated": False, "Total": {},
                    "Groups": [{"Keys": [region, key(region)], "Metrics": {
                        "AmortizedCost": {"Amount": amount, "Unit": "USD"}}}
                        for billing_service, region, amount in values if billing_service == service],
                } for start, end, values in COST_MONTHS],
            }

        compute_filter = {"And": [base, {"Dimensions": {"Key": "SERVICE", "Values": [EC2_COMPUTE]}}]}
        compute_request = breakdown_request(compute_filter, ("REGION", "INSTANCE_TYPE"))
        self.add("ce", "us-east-1", "get_cost_and_usage",
                 breakdown_response(compute_request, EC2_COMPUTE,
                                    lambda region: "t3.small" if region == CENTRAL else "m6i.large"),
                 compute_request)
        ebs_service = {"Dimensions": {"Key": "SERVICE", "Values": ["EC2 - Other"]}}
        usage_types = ["EU-EBS:VolumeUsage.gp3", "EUC1-EBS:VolumeUsage.gp3"]
        self.add("ce", "us-east-1", "get_dimension_values", {
            "DimensionValues": [{"Value": value} for value in usage_types],
            "ReturnSize": len(usage_types), "TotalSize": len(usage_types),
        }, {"TimePeriod": period, "Dimension": "USAGE_TYPE", "Context": "COST_AND_USAGE",
            "Filter": {"And": [base, ebs_service]}})
        ebs_filter = {"And": [base, {"And": [
            ebs_service, {"Dimensions": {"Key": "USAGE_TYPE", "Values": sorted(usage_types)}},
        ]}]}
        ebs_request = breakdown_request(ebs_filter, ("REGION", "SERVICE"))
        self.add("ce", "us-east-1", "get_cost_and_usage",
                 breakdown_response(ebs_request, "EC2 - Other", lambda region: "EC2 - Other"),
                 ebs_request)

    def _inventory(self, deny_snapshots):
        self.add("eks", CENTRAL, "list_clusters", {"clusters": []}, {})
        self.add("eks", WEST, "list_clusters", {"clusters": [CLUSTER]}, {})
        self.add("eks", WEST, "describe_cluster", {"cluster": {
            "name": CLUSTER, "arn": f"arn:aws:eks:{WEST}:{ACCOUNT}:cluster/{CLUSTER}",
            "status": "ACTIVE", "version": "1.34", "platformVersion": "eks.1",
            "createdAt": datetime(2026, 8, 1, tzinfo=timezone.utc),
            "roleArn": f"arn:aws:iam::{ACCOUNT}:role/demo-eks-role",
            "endpoint": "https://demo.invalid.example",
            "resourcesVpcConfig": {"vpcId": "vpc-00000000000000001",
                                   "subnetIds": ["subnet-00000000000000001"],
                                   "securityGroupIds": ["sg-00000000000000001"]},
        }}, {"name": CLUSTER})
        self.add("eks", WEST, "list_nodegroups", {"nodegroups": [NODEGROUP]},
                 {"clusterName": CLUSTER})
        self.add("eks", WEST, "describe_nodegroup", {"nodegroup": {
            "nodegroupName": NODEGROUP, "clusterName": CLUSTER,
            "nodegroupArn": f"arn:aws:eks:{WEST}:{ACCOUNT}:nodegroup/{CLUSTER}/{NODEGROUP}/demo",
            "status": "ACTIVE", "version": "1.34", "capacityType": "ON_DEMAND",
            "amiType": "AL2023_x86_64_STANDARD", "instanceTypes": ["m6i.large"], "diskSize": 32,
            "subnets": ["subnet-00000000000000001"],
            "scalingConfig": {"minSize": 2, "maxSize": 4, "desiredSize": 2},
            "resources": {"autoScalingGroups": [{"name": ASG}]},
        }}, {"clusterName": CLUSTER, "nodegroupName": NODEGROUP})
        for operation, result in (("list_fargate_profiles", "fargateProfileNames"),
                                  ("list_addons", "addons")):
            self.add("eks", WEST, operation, {result: []}, {"clusterName": CLUSTER})
        asg = {
            "AutoScalingGroupName": ASG,
            "AutoScalingGroupARN": f"arn:aws:autoscaling:{WEST}:{ACCOUNT}:autoScalingGroup:demo:autoScalingGroupName/{ASG}",
            "MinSize": 2, "MaxSize": 4, "DesiredCapacity": 2, "DefaultCooldown": 300,
            "AvailabilityZones": [WEST + "a"], "HealthCheckType": "EC2",
            "CreatedTime": datetime(2026, 8, 1, tzinfo=timezone.utc),
            "VPCZoneIdentifier": "subnet-00000000000000001",
            "Instances": [{"InstanceId": identifier, "AvailabilityZone": WEST + "a",
                           "LifecycleState": "InService", "HealthStatus": "Healthy",
                           "ProtectedFromScaleIn": False} for identifier in (NODE_A, NODE_B)],
        }
        self.add("autoscaling", WEST, "describe_auto_scaling_groups", {"AutoScalingGroups": [asg]}, {})
        self.add("autoscaling", CENTRAL, "describe_auto_scaling_groups", {"AutoScalingGroups": []}, {})
        instances = {
            WEST: [_instance(NODE_A, WEST, "m6i.large", [ROOT_A, DATA_A], "worker-a"),
                   _instance(NODE_B, WEST, "m6i.large", [ROOT_B])],
            CENTRAL: [_instance(STANDALONE, CENTRAL, "t3.small", [ROOT_STANDALONE], "batch-runner")],
        }
        volumes = {
            WEST: [_volume(ROOT_A, WEST, 32, NODE_A), _volume(DATA_A, WEST, 100, NODE_A, "/dev/sdf"),
                   _volume(ROOT_B, WEST, 32, NODE_B)],
            CENTRAL: [_volume(ROOT_STANDALONE, CENTRAL, 20, STANDALONE),
                      _volume(UNATTACHED, CENTRAL, 200)],
        }
        for region in (CENTRAL, WEST):
            if region == WEST:
                self.add("ec2", region, "describe_instances", {
                    "Reservations": [{"Instances": instances[region][:1]}], "NextToken": "instance-page-2",
                }, {})
                self.add("ec2", region, "describe_instances", {
                    "Reservations": [{"Instances": instances[region][1:]}],
                }, {"NextToken": "instance-page-2"})
            else:
                self.add("ec2", region, "describe_instances", {
                    "Reservations": [{"Instances": instances[region]}],
                }, {})
            self.add("ec2", region, "describe_volumes", {"Volumes": volumes[region]}, {})
            instance_type = "m6i.large" if region == WEST else "t3.small"
            self.add("ec2", region, "describe_instance_types", {"InstanceTypes": [{
                "InstanceType": instance_type, "VCpuInfo": {"DefaultVCpus": 2},
                "MemoryInfo": {"SizeInMiB": 8192 if region == WEST else 2048},
            }]}, {"InstanceTypes": [instance_type]})
            if deny_snapshots and region == WEST:
                self.stubbers[("ec2", region)].add_client_error(
                    "describe_snapshots", service_error_code="UnauthorizedOperation",
                    service_message="Synthetic denial of ec2:DescribeSnapshots", http_status_code=403,
                    expected_params={"OwnerIds": ["self"]},
                )
            else:
                self.add("ec2", region, "describe_snapshots", {"Snapshots": []}, {"OwnerIds": ["self"]})

    def _pricing(self):
        def add_query(service, attributes, products, next_products=None):
            request = {"ServiceCode": service, "FormatVersion": "aws_v1", "MaxResults": 100,
                       "Filters": [{"Type": "TERM_MATCH", "Field": field, "Value": value}
                                   for field, value in sorted(attributes.items())]}
            response = {"PriceList": products, "FormatVersion": "aws_v1"}
            if next_products is not None:
                response["NextToken"] = "price-page-2"
            self.add("pricing", "us-east-1", "get_products", response, request)
            if next_products is not None:
                self.add("pricing", "us-east-1", "get_products", {
                    "PriceList": next_products, "FormatVersion": "aws_v1",
                }, {**request, "NextToken": "price-page-2"})

        def compute_attributes(region, instance_type):
            return {"regionCode": region, "instanceType": instance_type,
                    "productFamily": "Compute Instance", "capacitystatus": "Used",
                    "tenancy": "Shared", "operation": "RunInstances"}

        add_query("AmazonEC2", compute_attributes(CENTRAL, "t3.small"), [
            _product("FAKE-T3-CENTRAL", "Compute Instance", "Hrs", "0.025", regionCode=CENTRAL),
        ])
        add_query("AmazonEC2", {"regionCode": CENTRAL, "volumeApiName": "gp3"}, [
            _product("FAKE-GP3-CENTRAL", "Storage", "GB-Mo", "0.10", regionCode=CENTRAL),
        ])
        add_query("AmazonEKS", {"regionCode": WEST}, [
            _product("FAKE-EKS-STANDARD", "EKS standard support", "Hrs", "0.10", regionCode=WEST),
            _product("FAKE-EKS-EXTENDED", "EKS extended support", "Hrs", "0.60", regionCode=WEST),
        ])
        west_compute = _product("FAKE-M6I-WEST", "Compute Instance", "Hrs", "0.12", regionCode=WEST)
        # A duplicate rate on page two must not make the quote ambiguous.
        add_query("AmazonEC2", compute_attributes(WEST, "m6i.large"), [west_compute], [west_compute])
        add_query("AmazonEC2", {"regionCode": WEST, "volumeApiName": "gp3"}, [
            _product("FAKE-GP3-WEST", "Storage", "GB-Mo", "0.08", regionCode=WEST),
            _product("FAKE-GP3-IOPS-WEST", "Provisioned IOPS", "IOPS-Mo", "0.005", regionCode=WEST),
        ])


def cli_parameters(output: Path, *, overwrite=False, strict=False, workers=2, filter_file=None,
                   report_format="raw-csv", all_services=False):
    start = BUNDLE_START if report_format in {"bundle", "html"} else START
    parameters = [
        "export", "--region", WEST, CENTRAL, "--service", "ec2", "eks", "ebs",
        "--from", start, "--to", END, "--mode", "combined", "--granularity", "MONTHLY",
        "--group-by", "SERVICE", "REGION", "--metric", "AmortizedCost", "--prices",
        "--ce-region", "us-east-1", "--pricing-region", "us-east-1",
        "--workers", str(workers), "--excel", "--format", report_format,
    ]
    if report_format != "html":
        parameters.extend(("--output", str(output)))
    if all_services:
        parameters[parameters.index("--region") + 1:parameters.index("--from")] = ["all", "--service", "all"]
    if overwrite:
        parameters.append("--overwrite")
    if strict:
        parameters.append("--strict")
    if filter_file is not None:
        parameters.extend(("--filter-file", str(filter_file)))
    return parameters


def run_simulation(output: Path | None = None, *, working_directory=None, overwrite=False, strict=False, deny_snapshots=False,
                   workers=2, custom_filter=None, report_format="raw-csv", deny_costs=False,
                   all_services=False, language="en", date_format="iso", display_timezone="UTC"):
    """Execute the real CLI and selected writer against fully offline SDK fixtures."""
    if report_format == "html" and output is not None:
        raise ValueError("HTML simulation uses the fixed account directory; do not supply an output path.")
    filter_file = None
    with ExitStack() as stack:
        if working_directory is not None:
            stack.enter_context(chdir(working_directory))
        output = (costcsv.html_output_path(ACCOUNT, {"all"} if all_services else {"ec2", "eks", "ebs"},
                                          GENERATED, display_timezone)
                  if report_format == "html" else Path(output).absolute())
        if custom_filter is not None:
            import tempfile
            temporary = stack.enter_context(tempfile.TemporaryDirectory(prefix="costcsv-filter-"))
            filter_file = Path(temporary) / "filter.json"
            filter_file.write_text(json.dumps(custom_filter), encoding="utf-8")
        attempts = stack.enter_context(offline_environment())
        factory = SimulatedAWS
        if all_services:
            if report_format != "html":
                raise ValueError("The all-services simulation currently produces HTML only.")
            from examples.three_region_fixtures import ThreeRegionAWS
            factory = ThreeRegionAWS
        aws = stack.enter_context(factory(deny_snapshots=deny_snapshots,
                                               custom_filter=custom_filter,
                                               report_format=report_format, deny_costs=deny_costs))
        stack.enter_context(patch("costcsv.boto3.Session", return_value=aws))
        stack.enter_context(patch("costcsv.datetime", FrozenDateTime))
        stack.enter_context(patch("aws_inventory.pricing.datetime", FrozenDateTime))
        captured_rows = []
        captured_context = {}

        def simulated_writer(real_writer):
            def write_simulated_report(path, rows, *, context, **kwargs):
                marked_rows = [
                    {**row, "notes": "OFFLINE SIMULATION: " + row.get("notes", "")}
                    if row.get("row_type") == "report" else row
                    for row in rows
                ]
                marked_context = {**context, "simulation_only": True,
                                  "simulation_warning": "Fabricated offline fixture data; not AWS prices or billing."}
                captured_rows.extend(deepcopy(marked_rows))
                captured_context.update(deepcopy(marked_context))
                return real_writer(path, marked_rows, context=marked_context, **kwargs)
            return write_simulated_report

        for writer_name in ("write_report", "write_service_reports", "write_html_report"):
            if hasattr(costcsv, writer_name):
                stack.enter_context(patch("costcsv." + writer_name,
                                          simulated_writer(getattr(costcsv, writer_name))))
        parameters = cli_parameters(output, overwrite=overwrite, strict=strict,
                                    workers=workers, filter_file=filter_file,
                                    report_format=report_format, all_services=all_services)
        parameters.extend(("--language", language, "--date-format", date_format, "--timezone", display_timezone))
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = costcsv.main(parameters)
        if attempts:
            raise NetworkForbidden(f"Unexpected network attempts: {len(attempts)}")
        result = {"exit_code": exit_code, "parameters": parameters, "stderr": stderr.getvalue(),
                  "calls": aws.calls, "client_requests": aws.session_requests,
                  "network_attempts": len(attempts), "expected_cost_filter": aws.expected_cost_filter,
                  "rows": captured_rows, "context": captured_context, "output": str(output)}
    return result


def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream, delimiter=";"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("html", "bundle", "raw-csv"), default="html",
                        help="Unified HTML cost report (default), or a legacy format")
    parser.add_argument("--output", type=Path,
                        help="Legacy formats only: directory for bundle or file for raw-csv")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--language", choices=costcsv.LANGUAGES, default="en")
    parser.add_argument("--date-format", choices=("iso", "dmy", "mdy"), default="iso")
    parser.add_argument("--timezone", default="UTC")
    parser.add_argument("--service", "--services", choices=("compute", "all"), default="compute",
                        help="Compute example (default), or fixtures for every implemented collector (HTML)")
    args = parser.parse_args(argv)
    if args.service == "all" and args.format != "html":
        parser.error("--service all requires --format html")
    if args.format == "html":
        if args.output is not None:
            parser.error("HTML output is fixed at output/ACCOUNT/YYYY-MM-DD_SERVICE_HHMM.html; --output is not supported")
        args.output = costcsv.html_output_path(ACCOUNT, {"all"} if args.service == "all" else {"ec2", "eks", "ebs"},
                                              GENERATED, args.timezone)
    elif args.output is None:
        args.output = Path("examples/simulated_reports" if args.format == "bundle"
                           else "examples/simulated_inventory.csv")
    manifest_path = args.output.with_suffix(".manifest.json")
    preview_path = args.output.with_name(args.output.stem + "_preview.csv")
    artifacts = [args.output] if args.format == "html" else [args.output, manifest_path]
    if args.format == "raw-csv":
        artifacts.append(preview_path)
    for artifact in artifacts:
        if artifact.exists() and not args.overwrite:
            parser.error(f"Output already exists: {artifact}; pass --overwrite to replace it")
    print("OFFLINE SIMULATION: resources, costs, and catalog rates are fabricated. "
          "Network access is blocked; no AWS request will be sent.")
    result = run_simulation(None if args.format == "html" else args.output, overwrite=args.overwrite, report_format=args.format,
                            all_services=args.service == "all", language=args.language,
                            date_format=args.date_format, display_timezone=args.timezone,
                            workers=3 if args.service == "all" else 2)
    print(result["stderr"], end="")
    if result["exit_code"] != 0:
        return result["exit_code"]
    rows = result["rows"]
    inventory_services = sorted({row["service"] for row in rows if row["row_type"] == "inventory"})
    if args.service == "all" and set(inventory_services) != set(costcsv.SERVICES):
        raise AssertionError("The all-services fixture did not exercise every implemented collector.")
    totals = sum((Decimal(str(row["amount"])) for row in rows
                  if row["row_type"] == "cost_summary"), Decimal(0))
    monthly_totals = {}
    for row in rows:
        if row["row_type"] == "cost_summary":
            month = row["period_start"][:7]
            monthly_totals[month] = monthly_totals.get(month, Decimal(0)) + Decimal(str(row["amount"]))
    start = BUNDLE_START if args.format in {"bundle", "html"} else START
    period_hours = (datetime.fromisoformat(END) - datetime.fromisoformat(start)).days * 24
    manifest = {
        "simulation_only": True,
        "warning": "All resources, costs, and catalog rates are fabricated; these are not AWS prices.",
        "generated_at_utc": GENERATED, "account_id": ACCOUNT,
        "aws_cli_required": False, "aws_credentials_required": False,
        "network_attempts": result["network_attempts"], "aws_api_requests_sent": 0,
        "report_format": args.format,
        "simulated_inventory_services": inventory_services,
        "cli_parameters": result["parameters"],
        "row_counts": dict(sorted(Counter(row["row_type"] for row in rows).items())),
        "period_start": start, "period_end_exclusive": END, "period_hours": period_hours,
        "cost_summary_total_usd": str(totals),
        "monthly_cost_summary_usd": {month: str(total) for month, total in sorted(monthly_totals.items())},
        "api_calls_simulated": len(result["calls"]),
        "operations_simulated": dict(sorted(Counter(
            f"{call['service']}:{call['region']}:{call['operation']}" for call in result["calls"]
        ).items())),
        "topology": {
            WEST: {"cluster": CLUSTER, "nodegroup": NODEGROUP, "autoscaling_group": ASG,
                   "nodes": [{"id": NODE_A, "name": "worker-a", "disks_gib": [32, 100]},
                             {"id": NODE_B, "name": None, "disks_gib": [32]}]},
            CENTRAL: {"standalone_instance": {"id": STANDALONE, "name": "batch-runner", "disks_gib": [20]},
                      "unattached_volume": {"id": UNATTACHED, "size_gib": 200}},
        },
    }
    # Exclusive creation prevents a concurrent writer from being overwritten.
    creation_mode = "w" if args.overwrite else "x"
    if args.format != "html":
        with manifest_path.open(creation_mode, encoding="utf-8") as stream:
            stream.write(json.dumps(manifest, indent=2) + "\n")
    if args.format == "raw-csv":
        with preview_path.open(creation_mode, encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=PREVIEW_FIELDS, delimiter=";", lineterminator="\n")
            writer.writeheader()
            for row in read_csv(args.output):
                if row["row_type"] in {"cost_summary", "inventory", "price_quote"}:
                    writer.writerow({field: "true" if field == "simulation_only" else row[field]
                                     for field in PREVIEW_FIELDS})
    print(json.dumps({key: manifest[key] for key in (
        "simulation_only", "network_attempts", "aws_api_requests_sent", "row_counts",
        "cost_summary_total_usd", "api_calls_simulated")}, indent=2))
    if args.service == "all":
        print("Simulated inventory services: " + ", ".join(inventory_services))
    if args.format == "html":
        print(f"Unified HTML report: {args.output}")
    elif args.format == "bundle":
        print(f"Service reports: {args.output}\nSimulation manifest: {manifest_path}")
    else:
        print(f"Full CSV: {args.output}\nPreview CSV: {preview_path}\nManifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
