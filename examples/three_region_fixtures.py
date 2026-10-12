"""SDK-validated, fabricated inventory for all 35 families in three regions.

Every response is queued in botocore Stubber. No live client can fall through.
Global services are collected once; S3 has one bucket in each region.
"""
from copy import deepcopy
from decimal import Decimal

from aws_inventory.costs import SERVICE_FILTERS, EC2_OTHER
from examples.all_services_fixtures import AllServicesAWS, CREATED
from examples.simulate_export import (
    SimulatedAWS, ACCOUNT, WEST, CENTRAL, END, COST_MONTHS, STANDALONE,
    ROOT_STANDALONE, UNATTACHED, _instance, _volume, _product,
)

NORTH = "eu-north-1"
REGIONS = (CENTRAL, NORTH, WEST)
EXTENDED_SDK = ("fsx", "backup", "apigateway", "apigatewayv2", "kafka", "kinesis", "emr",
                "glue", "athena", "sagemaker", "bedrock", "events", "stepfunctions", "ecr", "kms", "secretsmanager", "wafv2")


def relocate(value, region):
    if isinstance(value, str):
        return value.replace(WEST, region).replace(CENTRAL, region)
    if isinstance(value, list):
        return [relocate(v, region) for v in value]
    if isinstance(value, dict):
        return {k: relocate(v, region) for k, v in value.items()}
    return value


class RecordedInventory(AllServicesAWS):
    def __init__(self):
        self.records = []
        self._inventory(False)

    def add(self, sdk, region, operation, response, expected):
        self.records.append((sdk, region, operation, response, expected))


class ThreeRegionAWS(AllServicesAWS):
    def client_specifications(self):
        services = {sdk for sdk, region in AllServicesAWS.client_specifications(self) if region in {WEST, CENTRAL}} - {"sts", "cloudfront", "route53"}
        return ([("sts", WEST), ("ce", "us-east-1"), ("pricing", "us-east-1"),
                 ("cloudfront", WEST), ("route53", WEST), ("wafv2", "us-east-1")]
                + [(sdk, region) for region in REGIONS for sdk in sorted(services | set(EXTENDED_SDK))])

    def _identity(self):
        SimulatedAWS._identity(self)
        self.add("ec2", WEST, "describe_regions", {"Regions": [
            {"RegionName": r, "Endpoint": f"ec2.{r}.amazonaws.com", "OptInStatus": "opt-in-not-required"}
            for r in REGIONS]}, {"AllRegions": False})

    def _costs(self, custom_filter, *, deny_costs=False):
        expression = {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [ACCOUNT]}}
        if custom_filter:
            expression = {"And": [expression, deepcopy(custom_filter)]}
        self.expected_cost_filter = expression
        request = {"TimePeriod": {"Start": self.period_start, "End": END}, "Granularity": "MONTHLY",
                   "Metrics": ["AmortizedCost"], "Filter": expression,
                   "GroupBy": [{"Type": "DIMENSION", "Key": k} for k in ("SERVICE", "REGION")]}
        if deny_costs:
            self.stubbers[("ce", "us-east-1")].add_client_error("get_cost_and_usage", service_error_code="AccessDeniedException", expected_params=request)
            return
        months = []
        for month_index, (start, end, _) in enumerate(COST_MONTHS):
            groups = []
            for index, selector in enumerate(sorted(set(SERVICE_FILTERS) | {"ebs"})):
                name = EC2_OTHER if selector == "ebs" else SERVICE_FILTERS[selector]["Dimensions"]["Values"][0]
                regions = ("",) if selector in {"cloudfront", "route53"} else REGIONS
                for region_index, region in enumerate(regions):
                    amount = Decimal(index+1)*Decimal("3.23") + region_index*7 + month_index*10
                    groups.append({"Keys": [name, region], "Metrics": {"AmortizedCost": {"Amount": str(amount), "Unit": "USD"}}})
            months.append({"TimePeriod": {"Start": start, "End": end}, "Estimated": False, "Total": {}, "Groups": groups})
        for page in range(2):
            periods = [{**m, "Groups": m["Groups"][page::2]} for m in months]
            self.add("ce", "us-east-1", "get_cost_and_usage", {"ResultsByTime": periods,
                **({"NextPageToken": "next-monthly-page"} if page == 0 else {})},
                {**request, **({"NextPageToken": "next-monthly-page"} if page else {})})

    def _inventory(self, deny_snapshots):
        if deny_snapshots:
            raise ValueError("Use the compute fixture to simulate snapshot permission denial.")
        recorded = RecordedInventory().records
        for region in REGIONS:
            for sdk, original_region, operation, original_response, expected in recorded:
                if sdk in {"cloudfront", "route53", "s3"}:
                    continue
                if original_region != (CENTRAL if sdk == "ecs" else WEST):
                    continue
                response, params = relocate(deepcopy(original_response), region), relocate(expected, region)
                if sdk == "ec2" and operation == "describe_instances" and params:
                    response["Reservations"][0]["Instances"].append(_instance(STANDALONE, region, "t3.small", [ROOT_STANDALONE], "batch-runner"))
                if sdk == "ec2" and operation == "describe_volumes":
                    response["Volumes"] += [_volume(ROOT_STANDALONE, region, 20, STANDALONE), _volume(UNATTACHED, region, 200)]
                if sdk == "ec2" and operation == "describe_instance_types":
                    params = {"InstanceTypes": ["m6i.large", "t3.small"]}
                    response["InstanceTypes"].append({"InstanceType": "t3.small", "VCpuInfo": {"DefaultVCpus": 2}, "MemoryInfo": {"SizeInMiB": 2048}})
                if sdk == "ecs" and operation == "list_tasks":
                    response["taskArns"].append(response["taskArns"][0].replace("b"*32, "c"*32))
                if sdk == "ecs" and operation == "describe_tasks":
                    params["tasks"].append(params["tasks"][0].replace("b"*32, "c"*32))
                    task = deepcopy(response["tasks"][0])
                    task.update(taskArn=task["taskArn"].replace("b"*32, "c"*32), launchType="FARGATE", cpu="1024", memory="2048", group="family:demo-fargate", taskDefinitionArn=f"arn:aws:ecs:{region}:{ACCOUNT}:task-definition/demo-fargate:1")
                    task.pop("containerInstanceArn", None)
                    response["tasks"].append(task)
                if sdk == "ec2" and operation == "describe_vpc_endpoints":
                    response["VpcEndpoints"] = [{"VpcEndpointId": "vpce-00000000000000001", "VpcEndpointType": "Interface", "VpcId": "vpc-00000000000000001", "SubnetIds": ["subnet-00000000000000001"], "State": "available"}]
                if sdk == "ec2" and operation == "describe_transit_gateways":
                    response["TransitGateways"] = [{"TransitGatewayId": "tgw-00000000000000001", "State": "available"}]
                if sdk == "ec2" and operation == "describe_transit_gateway_attachments":
                    response["TransitGatewayAttachments"] = [{"TransitGatewayAttachmentId": "tgw-attach-00000000000000001", "TransitGatewayId": "tgw-00000000000000001", "ResourceId": "vpc-00000000000000001", "ResourceType": "vpc", "State": "available"}]
                self.add(sdk, region, operation, response, params)
                if sdk == "ecs" and operation == "describe_tasks":
                    self.add("ecs", region, "describe_task_definition", {"taskDefinition": {"taskDefinitionArn": task["taskDefinitionArn"], "family": "demo-fargate", "runtimePlatform": {"operatingSystemFamily": "LINUX", "cpuArchitecture": "X86_64"}}}, {"taskDefinition": task["taskDefinitionArn"]})
            self.add("ec2", region, "describe_vpn_connections", {"VpnConnections": [{"VpnConnectionId": "vpn-00000000000000001", "State": "available", "Type": "ipsec.1", "TransitGatewayId": "tgw-00000000000000001"}]}, {})
            self._extended(region)
        for sdk, region, operation, response, params in recorded:
            if sdk in {"cloudfront", "route53"}:
                if sdk == "cloudfront" and operation == "list_distributions":
                    response = deepcopy(response)
                    distribution = response["DistributionList"]["Items"][0]
                    distribution["WebACLId"] = f"arn:aws:wafv2:us-east-1:{ACCOUNT}:global/webacl/static-site/12345678-1234-1234-1234-123456789012"
                    distribution["Origins"]["Items"][0]["DomainName"] = f"demo-static-assets-{ACCOUNT}-{WEST}.s3.amazonaws.com"
                self.add(sdk, region, operation, response, params)
        s3_records = [r for r in recorded if r[0] == "s3"]
        bucket = "demo-static-assets-123456789012"
        self.add("s3", WEST, "list_buckets", {"Buckets": [{"Name": bucket+"-"+r, "CreationDate": CREATED, "BucketRegion": r} for r in REGIONS]}, {"MaxBuckets": 1000})
        for region in REGIONS:
            for sdk, _, operation, response, params in s3_records[1:]:
                self.add(sdk, region, operation, response, {**params, "Bucket": bucket+"-"+region})
        self.add("wafv2", "us-east-1", "list_web_acls", {"WebACLs": [{"Name": "static-site", "Id": "12345678-1234-1234-1234-123456789012", "ARN": f"arn:aws:wafv2:us-east-1:{ACCOUNT}:global/webacl/static-site/12345678-1234-1234-1234-123456789012", "LockToken": "12345678-1234-1234-1234-123456789012"}]}, {"Scope": "CLOUDFRONT"})

    def _extended(self, region):
        def arn(service, resource):
            return f"arn:aws:{service}:{region}:{ACCOUNT}:{resource}"
        def add(sdk, op, response, **params):
            self.add(sdk, region, op, response, params)
        fs = "fs-0123456789abcdef0"
        add("fsx", "describe_file_systems", {"FileSystems": [{"FileSystemId": fs, "ResourceARN": arn("fsx", "file-system/"+fs), "FileSystemType": "LUSTRE", "Lifecycle": "AVAILABLE", "StorageCapacity": 1200, "StorageType": "SSD", "VpcId": "vpc-00000000000000001", "SubnetIds": ["subnet-00000000000000001"]}]})
        add("fsx", "describe_volumes", {"Volumes": []})
        add("backup", "list_backup_vaults", {"BackupVaultList": [{"BackupVaultName": "daily-backups", "BackupVaultArn": arn("backup", "backup-vault:daily-backups"), "NumberOfRecoveryPoints": 1}]})
        add("backup", "list_recovery_points_by_backup_vault", {"RecoveryPoints": [{"RecoveryPointArn": arn("backup", "recovery-point:demo"), "ResourceArn": arn("ec2", "volume/"+ROOT_STANDALONE), "ResourceType": "EBS", "BackupSizeInBytes": 10*1024**3, "Status": "COMPLETED"}]}, BackupVaultName="daily-backups")
        add("apigateway", "get_rest_apis", {"items": [{"id": "demoapi123", "name": "orders-rest"}]})
        add("apigateway", "get_stages", {"item": [{"stageName": "production", "cacheClusterEnabled": False}]}, restApiId="demoapi123")
        add("apigatewayv2", "get_apis", {"Items": [{"ApiId": "http123", "Name": "orders-http", "ProtocolType": "HTTP", "RouteSelectionExpression": "$request.method $request.path"}]})
        add("apigatewayv2", "get_integrations", {"Items": [{"IntegrationId": "lambda1", "IntegrationType": "AWS_PROXY", "IntegrationUri": arn("lambda", "function:orders-api")}]}, ApiId="http123")
        add("kafka", "list_clusters_v2", {"ClusterInfoList": [{"ClusterArn": arn("kafka", "cluster/demo-stream/"+"a"*36), "ClusterName": "demo-stream", "ClusterType": "PROVISIONED", "State": "ACTIVE", "Provisioned": {"NumberOfBrokerNodes": 3, "BrokerNodeGroupInfo": {"InstanceType": "kafka.m5.large", "ClientSubnets": ["subnet-00000000000000001"], "SecurityGroups": ["sg-00000000000000001"]}}}]})
        add("kinesis", "list_streams", {"StreamNames": ["orders-stream"], "HasMoreStreams": False})
        add("kinesis", "describe_stream_summary", {"StreamDescriptionSummary": {"StreamName": "orders-stream", "StreamARN": arn("kinesis", "stream/orders-stream"), "StreamStatus": "ACTIVE", "RetentionPeriodHours": 24, "StreamCreationTimestamp": CREATED, "OpenShardCount": 2, "EnhancedMonitoring": [], "StreamModeDetails": {"StreamMode": "PROVISIONED"}}}, StreamName="orders-stream")
        add("emr", "list_clusters", {"Clusters": [{"Id": "j-DEMO123", "Name": "analytics-jobs", "Status": {"State": "WAITING"}}]}, ClusterStates=["STARTING", "BOOTSTRAPPING", "RUNNING", "WAITING"])
        add("emr", "describe_cluster", {"Cluster": {"Id": "j-DEMO123", "Name": "analytics-jobs", "Status": {"State": "WAITING"}, "ReleaseLabel": "emr-7.0.0"}}, ClusterId="j-DEMO123")
        # Do not falsely attach the ECS worker to EMR: separate workload, no invented EC2 evidence.
        add("emr", "list_instances", {"Instances": []}, ClusterId="j-DEMO123")
        add("glue", "get_jobs", {"Jobs": [{"Name": "orders-etl", "WorkerType": "G.1X", "NumberOfWorkers": 2, "GlueVersion": "4.0", "DefaultArguments": {"--password": "DO-NOT-EXPORT-JOB-SECRET"}}]})
        add("athena", "list_work_groups", {"WorkGroups": [{"Name": "analytics", "State": "ENABLED", "CreationTime": CREATED}]})
        endpoint = "orders-predictor"
        add("sagemaker", "list_endpoints", {"Endpoints": [{"EndpointName": endpoint, "EndpointArn": arn("sagemaker", "endpoint/"+endpoint), "CreationTime": CREATED, "LastModifiedTime": CREATED, "EndpointStatus": "InService"}]})
        add("sagemaker", "describe_endpoint", {"EndpointName": endpoint, "EndpointArn": arn("sagemaker", "endpoint/"+endpoint), "EndpointConfigName": "orders-config", "EndpointStatus": "InService", "CreationTime": CREATED, "LastModifiedTime": CREATED}, EndpointName=endpoint)
        add("sagemaker", "describe_endpoint_config", {"EndpointConfigName": "orders-config", "EndpointConfigArn": arn("sagemaker", "endpoint-config/orders-config"), "CreationTime": CREATED, "ProductionVariants": [{"VariantName": "AllTraffic", "ModelName": "orders-model", "InitialInstanceCount": 1, "InstanceType": "ml.m5.large"}]}, EndpointConfigName="orders-config")
        add("bedrock", "list_provisioned_model_throughputs", {"provisionedModelSummaries": []})
        add("bedrock", "list_inference_profiles", {"inferenceProfileSummaries": [{"inferenceProfileName": "demo-assistant", "inferenceProfileId": "demo12345678", "inferenceProfileArn": arn("bedrock", "application-inference-profile/demo12345678"), "status": "ACTIVE", "type": "APPLICATION", "models": [{"modelArn": f"arn:aws:bedrock:{region}::foundation-model/demo.model-v1"}]}]}, typeEquals="APPLICATION")
        add("events", "list_event_buses", {"EventBuses": [{"Name": "orders", "Arn": arn("events", "event-bus/orders")}]})
        add("events", "list_rules", {"Rules": [{"Name": "new-order", "Arn": arn("events", "rule/orders/new-order"), "State": "ENABLED"}]}, EventBusName="orders")
        add("events", "list_targets_by_rule", {"Targets": [{"Id": "orders-function", "Arn": arn("lambda", "function:orders-api"), "Input": "DO-NOT-EXPORT-EVENT-PAYLOAD"}]}, EventBusName="orders", Rule="new-order")
        add("stepfunctions", "list_state_machines", {"stateMachines": [{"stateMachineArn": arn("states", "stateMachine:orders-workflow"), "name": "orders-workflow", "type": "STANDARD", "creationDate": CREATED}]})
        add("ecr", "describe_repositories", {"repositories": [{"repositoryName": "orders-api", "repositoryArn": arn("ecr", "repository/orders-api"), "imageTagMutability": "IMMUTABLE"}]})
        add("ecr", "describe_images", {"imageDetails": [{"imageDigest": "sha256:"+"a"*64, "imageTags": ["v1"], "imageSizeInBytes": 120*1024**2}]}, repositoryName="orders-api")
        key = "12345678-1234-1234-1234-123456789012"
        add("kms", "list_keys", {"Keys": [{"KeyId": key, "KeyArn": arn("kms", "key/"+key)}]})
        add("kms", "describe_key", {"KeyMetadata": {"KeyId": key, "Arn": arn("kms", "key/"+key), "KeyManager": "CUSTOMER", "KeyState": "Enabled", "KeySpec": "SYMMETRIC_DEFAULT", "KeyUsage": "ENCRYPT_DECRYPT"}}, KeyId=key)
        add("secretsmanager", "list_secrets", {"SecretList": [{"ARN": arn("secretsmanager", "secret:orders-db-abcdef"), "Name": "orders-db", "KmsKeyId": key, "RotationEnabled": True}]})
        waf = arn("wafv2", "regional/webacl/orders/"+key)
        add("wafv2", "list_web_acls", {"WebACLs": [{"Name": "orders", "Id": key, "ARN": waf, "LockToken": key}]}, Scope="REGIONAL")
        add("wafv2", "list_resources_for_web_acl", {"ResourceArns": [arn("elasticloadbalancing", "loadbalancer/app/public-api/0000000000000001")]}, WebACLArn=waf, ResourceType="APPLICATION_LOAD_BALANCER")

    def _pricing(self):
        def quote(code, attrs, prices):
            products = [_product("FAKE-"+code+str(i)+"-"+attrs["regionCode"]+"-"+str(attrs.get("instanceType", attrs.get("productFamily", "default"))), family, unit, rate, **{**attrs, **extra}) for i, (family, unit, rate, extra) in enumerate(prices)]
            self.add("pricing", "us-east-1", "get_products", {"FormatVersion": "aws_v1", "PriceList": products},
                     {"ServiceCode": code, "FormatVersion": "aws_v1", "MaxResults": 100, "Filters": [{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in sorted(attrs.items())]})
        for region in REGIONS:
            quote("AmazonEKS", {"regionCode": region}, [("EKS standard support", "Hrs", "0.10", {}), ("EKS extended support", "Hrs", "0.60", {})])
            quote("AmazonECS", {"regionCode": region, "productFamily": "Compute"}, [("Compute", "hours", "0.04", {"usagetype": "Fargate-vCPU-Hours:perCPU"}), ("Compute", "hours", "0.004", {"usagetype": "Fargate-GB-Hours"})])
            for kind, rate in (("m6i.large", "0.12"), ("t3.small", "0.025")):
                quote("AmazonEC2", {"regionCode": region, "instanceType": kind, "productFamily": "Compute Instance", "capacitystatus": "Used", "tenancy": "Shared", "operation": "RunInstances"}, [("Compute Instance", "Hrs", rate, {})])
            quote("AmazonEC2", {"regionCode": region, "volumeApiName": "gp3"}, [("Storage", "GB-Mo", "0.08", {}), ("Provisioned IOPS", "IOPS-Mo", "0.005", {})])
            quote("AmazonElastiCache", {"regionCode": region, "instanceType": "cache.t4g.small", "cacheEngine": "Redis"}, [("Cache Instance", "Hrs", "0.05", {})])
            quote("AWSELB", {"regionCode": region, "operation": "LoadBalancing:Application"}, [("Load Balancer", "Hrs", "0.025", {}), ("LCU", "LCU-Hrs", "0.008", {})])
            quote("AmazonES", {"regionCode": region, "instanceType": "t3.small.search"}, [("Compute Instance", "Hrs", "0.06", {})])
            quote("AmazonRDS", {"regionCode": region, "productFamily": "Database Instance", "instanceType": "db.t4g.medium", "databaseEngine": "PostgreSQL", "deploymentOption": "Single-AZ"}, [("Database Instance", "Hrs", "0.15", {})])
            quote("AmazonRedshift", {"regionCode": region, "instanceType": "ra3.large"}, [("Compute Instance", "Hrs", "0.25", {})])
            quote("AmazonEC2", {"regionCode": region, "productFamily": "NAT Gateway"}, [("NAT Gateway", "Hrs", "0.045", {}), ("NAT Gateway", "GB", "0.045", {})])
            quote("AmazonVPC", {"regionCode": region, "productFamily": "VpcEndpoint"}, [("VpcEndpoint", "Hrs", "0.01", {}), ("VpcEndpoint", "GB", "0.01", {})])
            quote("AmazonVPC", {"regionCode": region, "productFamily": "IP Address"}, [("IP Address", "Hrs", "0.005", {"usagetype": "PublicIPv4:IdleAddress"}), ("IP Address", "Hrs", "0.005", {"usagetype": "PublicIPv4:InUseAddress"})])
            quote("AmazonVPC", {"regionCode": region, "productFamily": "VPN Connection"}, [("VPN Connection", "Hrs", "0.05", {})])
