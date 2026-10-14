"""Fabricated, SDK-validated responses for every implemented inventory service.

Used only inside simulate_export.offline_environment. Collectors and pricing are
the production implementations: unsupported estimates are deliberately not faked.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

from aws_inventory.costs import SERVICE_FILTERS
from examples.simulate_export import (
    ACCOUNT, WEST, CENTRAL, END, COST_MONTHS, STANDALONE, NODE_A,
    SimulatedAWS, _product,
)


CREATED = datetime(2026, 6, 1, tzinfo=timezone.utc)
VPC, SUBNET, SG = "vpc-00000000000000001", "subnet-00000000000000001", "sg-00000000000000001"
EXTRA_MONTHLY_COSTS = {
    "cloudfront": "33.60", "cloudwatch": "18.50", "dynamodb": "28.20", "ecs": "31.40",
    "efs": "15.00", "elasticache": "49.00", "elb": "22.80", "lambda": "6.70",
    "opensearch": "96.00", "rds": "117.20", "redshift": "180.00", "route53": "2.50",
    "s3": "12.60", "sns": "0.45", "sqs": "1.20", "vpc": "24.00",
}


def arn(service, resource, region=WEST):
    return f"arn:aws:{service}:{region}:{ACCOUNT}:{resource}"


class AllServicesAWS(SimulatedAWS):
    def client_specifications(self):
        regional = ("ecs", "rds", "lambda", "dynamodb", "efs", "elasticache", "elbv2", "elb",
                    "opensearch", "redshift", "sqs", "sns", "logs")
        return (super().client_specifications()
                + [(sdk, region) for region in (CENTRAL, WEST) for sdk in regional]
                + [(sdk, WEST) for sdk in ("cloudfront", "route53", "s3")])

    def _identity(self):
        super()._identity()
        self.add("ec2", WEST, "describe_regions", {"Regions": [
            {"RegionName": r, "Endpoint": f"ec2.{r}.amazonaws.com", "OptInStatus": "opt-in-not-required"}
            for r in (CENTRAL, WEST)]}, {"AllRegions": False})

    def _costs(self, custom_filter, *, deny_costs=False):
        expression = {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [ACCOUNT]}}
        if custom_filter:
            expression = {"And": [expression, deepcopy(custom_filter)]}
        request = {"TimePeriod": {"Start": self.period_start, "End": END}, "Granularity": "MONTHLY",
                   "Metrics": ["AmortizedCost"], "Filter": expression,
                   "GroupBy": [{"Type": "DIMENSION", "Key": key} for key in ("SERVICE", "REGION")]}
        self.expected_cost_filter = deepcopy(expression)
        if deny_costs:
            self.stubbers[("ce", "us-east-1")].add_client_error(
                "get_cost_and_usage", service_error_code="AccessDeniedException",
                service_message="Synthetic all-services billing denial", expected_params=request)
            return
        periods = []
        for index, (start, end, base) in enumerate(COST_MONTHS):
            groups = list(base)
            for selector, amount in sorted(EXTRA_MONTHLY_COSTS.items()):
                billing_service = SERVICE_FILTERS[selector]["Dimensions"]["Values"][0]
                region = "" if selector in {"cloudfront", "route53"} else CENTRAL if selector == "ecs" else WEST
                adjusted = (Decimal(amount) * (Decimal("0.90") + Decimal("0.05") * index)).quantize(Decimal("0.01"))
                groups.append((billing_service, region, str(adjusted)))
            periods.append((start, end, groups))
        for page_index in range(2):
            response = {"GroupDefinitions": request["GroupBy"], "ResultsByTime": [
                {"TimePeriod": {"Start": start, "End": end}, "Estimated": False, "Total": {},
                 "Groups": [{"Keys": [service, region], "Metrics": {"AmortizedCost": {"Amount": amount, "Unit": "USD"}}}
                            for service, region, amount in groups[page_index::2]]}
                for start, end, groups in periods]}
            if page_index == 0:
                response["NextPageToken"] = "all-services-cost-page-2"
            self.add("ce", "us-east-1", "get_cost_and_usage", response,
                     {**request, **({"NextPageToken": "all-services-cost-page-2"} if page_index else {})})

    def _inventory(self, deny_snapshots):
        super()._inventory(deny_snapshots)
        for region in (CENTRAL, WEST):
            self._ecs(region)
            self._managed(region)
            self._load_balancers(region)
            self._network(region)
        self._global_resources()

    def _ecs(self, region):
        if region == WEST:
            self.add("ecs", region, "list_clusters", {"clusterArns": []}, {})
            return
        cluster = arn("ecs", "cluster/demo-batch", region)
        container = arn("ecs", "container-instance/demo-batch/" + "a" * 32, region)
        service = arn("ecs", "service/demo-batch/batch-worker", region)
        task = arn("ecs", "task/demo-batch/" + "b" * 32, region)
        self.add("ecs", region, "list_clusters", {"clusterArns": [cluster]}, {})
        self.add("ecs", region, "describe_clusters", {"clusters": [{"clusterArn": cluster, "clusterName": "demo-batch",
            "status": "ACTIVE", "registeredContainerInstancesCount": 1, "runningTasksCount": 1,
            "pendingTasksCount": 0, "activeServicesCount": 1, "tags": []}]},
            {"clusters": [cluster], "include": ["TAGS", "SETTINGS", "STATISTICS", "CONFIGURATIONS"]})
        for status in ("ACTIVE", "DRAINING", "REGISTERING", "DEREGISTERING", "REGISTRATION_FAILED"):
            self.add("ecs", region, "list_container_instances", {"containerInstanceArns": [container] if status == "ACTIVE" else []},
                     {"cluster": cluster, "status": status})
        self.add("ecs", region, "describe_container_instances", {"containerInstances": [
            {"containerInstanceArn": container, "ec2InstanceId": STANDALONE, "status": "ACTIVE", "agentConnected": True,
             "runningTasksCount": 1, "pendingTasksCount": 0, "registeredAt": CREATED, "tags": []}]},
            {"cluster": cluster, "containerInstances": [container], "include": ["TAGS"]})
        self.add("ecs", region, "list_services", {"serviceArns": [service]}, {"cluster": cluster})
        self.add("ecs", region, "describe_services", {"services": [{"serviceArn": service, "serviceName": "batch-worker",
            "clusterArn": cluster, "status": "ACTIVE", "desiredCount": 1, "runningCount": 1, "pendingCount": 0,
            "launchType": "EC2", "taskDefinition": arn("ecs", "task-definition/batch-worker:1", region), "tags": []}]},
            {"cluster": cluster, "services": [service], "include": ["TAGS"]})
        self.add("ecs", region, "list_tasks", {"taskArns": [task]}, {"cluster": cluster, "desiredStatus": "RUNNING"})
        self.add("ecs", region, "describe_tasks", {"tasks": [{"taskArn": task, "clusterArn": cluster,
            "containerInstanceArn": container, "lastStatus": "RUNNING", "desiredStatus": "RUNNING", "launchType": "EC2",
            "group": "service:batch-worker", "cpu": "512", "memory": "1024", "createdAt": CREATED, "tags": []}]},
            {"cluster": cluster, "tasks": [task], "include": ["TAGS"]})

    def _managed(self, region):
        active = region == WEST
        db = {"DBInstanceIdentifier": "orders-db", "DBInstanceArn": arn("rds", "db:orders-db"),
              "DBInstanceClass": "db.t4g.medium", "DBInstanceStatus": "available", "Engine": "postgres", "EngineVersion": "16.4",
              "AllocatedStorage": 100, "StorageType": "gp3", "StorageEncrypted": True, "MultiAZ": False,
              "LicenseModel": "postgresql-license", "AvailabilityZone": WEST + "a", "InstanceCreateTime": CREATED,
              "DBSubnetGroup": {"DBSubnetGroupName": "demo-db-subnets", "VpcId": VPC,
                                "Subnets": [{"SubnetIdentifier": SUBNET}]}, "TagList": []}
        self.add("rds", region, "describe_db_instances", {"DBInstances": [db] if active else []}, {})
        self.add("rds", region, "describe_db_clusters", {"DBClusters": []}, {})
        self.add("rds", region, "describe_db_snapshots", {"DBSnapshots": []}, {"IncludeShared": False, "IncludePublic": False})
        self.add("rds", region, "describe_db_cluster_snapshots", {"DBClusterSnapshots": []}, {"IncludeShared": False, "IncludePublic": False})
        function = {"FunctionName": "orders-api", "FunctionArn": arn("lambda", "function:orders-api"),
                    "Runtime": "python3.12", "Role": f"arn:aws:iam::{ACCOUNT}:role/demo-lambda", "Handler": "app.handler",
                    "MemorySize": 512, "Timeout": 30, "State": "Active", "CodeSize": 4096,
                    "EphemeralStorage": {"Size": 512}, "Environment": {"Variables": {"API_SECRET": "DO-NOT-EXPORT-FAKE-SECRET"}}}
        self.add("lambda", region, "list_functions", {"Functions": [function] if active else []}, {})
        if active:
            self.add("lambda", region, "list_tags", {"Tags": {}}, {"Resource": function["FunctionArn"]})
            self.add("lambda", region, "get_function_configuration", function, {"FunctionName": "orders-api"})
        self.add("dynamodb", region, "list_tables", {"TableNames": ["orders-events"] if active else []}, {})
        if active:
            self.add("dynamodb", region, "describe_table", {"Table": {"TableName": "orders-events", "TableArn": arn("dynamodb", "table/orders-events"),
                "TableStatus": "ACTIVE", "TableSizeBytes": 5 * 1024**3, "ItemCount": 120000,
                "CreationDateTime": CREATED, "BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"}}}, {"TableName": "orders-events"})
            self.add("dynamodb", region, "list_tags_of_resource", {"Tags": []}, {"ResourceArn": arn("dynamodb", "table/orders-events")})
        fs_id = "fs-00000000000000001"
        fs = {"OwnerId": ACCOUNT, "CreationToken": "demo-shared-files", "FileSystemId": fs_id,
              "FileSystemArn": arn("elasticfilesystem", "file-system/"+fs_id), "CreationTime": CREATED,
              "LifeCycleState": "available", "NumberOfMountTargets": 1, "SizeInBytes": {"Value": 50 * 1024**3},
              "PerformanceMode": "generalPurpose", "ThroughputMode": "bursting", "Encrypted": True,
              "Tags": [{"Key": "Name", "Value": "shared-files"}]}
        self.add("efs", region, "describe_file_systems", {"FileSystems": [fs] if active else []}, {})
        if active:
            mount_id = "fsmt-00000000000000001"
            self.add("efs", region, "describe_mount_targets", {"MountTargets": [{"OwnerId": ACCOUNT, "MountTargetId": mount_id,
                "FileSystemId": fs_id, "SubnetId": SUBNET, "LifeCycleState": "available", "IpAddress": "10.0.1.40", "VpcId": VPC}]}, {"FileSystemId": fs_id})
            self.add("efs", region, "describe_mount_target_security_groups", {"SecurityGroups": [SG]}, {"MountTargetId": mount_id})
        self.add("elasticache", region, "describe_cache_subnet_groups", {"CacheSubnetGroups": []}, {})
        cache_arn = arn("elasticache", "cluster:session-cache")
        self.add("elasticache", region, "describe_cache_clusters", {"CacheClusters": [{"CacheClusterId": "session-cache", "ARN": cache_arn,
            "CacheNodeType": "cache.t4g.small", "Engine": "redis", "EngineVersion": "7.1", "CacheClusterStatus": "available",
            "NumCacheNodes": 1, "CacheClusterCreateTime": CREATED, "PreferredAvailabilityZone": WEST+"a"}] if active else []}, {"ShowCacheNodeInfo": True})
        if active:
            self.add("elasticache", region, "list_tags_for_resource", {"TagList": []}, {"ResourceName": cache_arn})
        self.add("elasticache", region, "describe_replication_groups", {"ReplicationGroups": []}, {})
        self.add("elasticache", region, "describe_serverless_caches", {"ServerlessCaches": []}, {})
        self.add("opensearch", region, "list_domain_names", {"DomainNames": [{"DomainName": "search-catalog"}] if active else []}, {})
        if active:
            self.add("opensearch", region, "describe_domains", {"DomainStatusList": [{"DomainId": ACCOUNT+"/search-catalog",
                "DomainName": "search-catalog", "ARN": arn("es", "domain/search-catalog"), "Created": True, "Processing": False,
                "EngineVersion": "OpenSearch_2.17", "ClusterConfig": {"InstanceType": "t3.small.search", "InstanceCount": 2},
                "EBSOptions": {"EBSEnabled": True, "VolumeType": "gp3", "VolumeSize": 50},
                "EncryptionAtRestOptions": {"Enabled": True}}]}, {"DomainNames": ["search-catalog"]})
            self.add("opensearch", region, "list_tags", {"TagList": []}, {"ARN": arn("es", "domain/search-catalog")})
        self.add("redshift", region, "describe_clusters", {"Clusters": [{"ClusterIdentifier": "analytics-warehouse", "NodeType": "ra3.large",
            "ClusterStatus": "available", "NumberOfNodes": 2, "MultiAZ": "Disabled", "ClusterVersion": "1.0", "VpcId": VPC,
            "Encrypted": True, "ClusterCreateTime": CREATED, "Tags": []}] if active else []}, {})
        queue = f"https://sqs.{WEST}.amazonaws.com/{ACCOUNT}/order-jobs"
        self.add("sqs", region, "list_queues", {"QueueUrls": [queue] if active else []}, {"MaxResults": 1000})
        if active:
            self.add("sqs", region, "get_queue_attributes", {"Attributes": {"QueueArn": arn("sqs", "order-jobs"),
                "ApproximateNumberOfMessages": "12", "SqsManagedSseEnabled": "true", "MessageRetentionPeriod": "345600"}},
                {"QueueUrl": queue, "AttributeNames": ["QueueArn", "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible",
                 "ApproximateNumberOfMessagesDelayed", "CreatedTimestamp", "MessageRetentionPeriod", "VisibilityTimeout", "MaximumMessageSize",
                 "KmsMasterKeyId", "SqsManagedSseEnabled", "RedrivePolicy", "FifoQueue"]})
            self.add("sqs", region, "list_queue_tags", {"Tags": {}}, {"QueueUrl": queue})
        topic = arn("sns", "order-alerts")
        self.add("sns", region, "list_topics", {"Topics": [{"TopicArn": topic}] if active else []}, {})
        if active:
            self.add("sns", region, "list_tags_for_resource", {"Tags": []}, {"ResourceArn": topic})
            self.add("sns", region, "get_topic_attributes", {"Attributes": {"DisplayName": "Order alerts", "SubscriptionsConfirmed": "2"}}, {"TopicArn": topic})
        logs_arn = arn("logs", "log-group:/aws/lambda/orders-api")
        self.add("logs", region, "describe_log_groups", {"logGroups": [{"logGroupName": "/aws/lambda/orders-api", "logGroupArn": logs_arn,
            "storedBytes": 3 * 1024**3, "retentionInDays": 30, "creationTime": 1780272000000}] if active else []}, {})
        if active:
            self.add("logs", region, "list_tags_for_resource", {"tags": {}}, {"resourceArn": logs_arn})

    def _load_balancers(self, region):
        active = region == WEST
        lb = arn("elasticloadbalancing", "loadbalancer/app/public-api/0000000000000001")
        target = arn("elasticloadbalancing", "targetgroup/api-targets/0000000000000001")
        self.add("elbv2", region, "describe_load_balancers", {"LoadBalancers": [{"LoadBalancerArn": lb, "LoadBalancerName": "public-api",
            "Type": "application", "Scheme": "internet-facing", "State": {"Code": "active"}, "VpcId": VPC,
            "AvailabilityZones": [{"ZoneName": WEST+"a", "SubnetId": SUBNET}], "SecurityGroups": [SG]}] if active else []}, {})
        if active:
            self.add("elbv2", region, "describe_tags", {"TagDescriptions": [{"ResourceArn": lb}]}, {"ResourceArns": [lb]})
            self.add("elbv2", region, "describe_listeners", {"Listeners": [{"Port": 443, "Protocol": "HTTPS"}]}, {"LoadBalancerArn": lb})
        self.add("elbv2", region, "describe_target_groups", {"TargetGroups": [{"TargetGroupArn": target, "TargetGroupName": "api-targets",
            "VpcId": VPC, "LoadBalancerArns": [lb], "TargetType": "instance", "Protocol": "HTTP", "Port": 8080}] if active else []}, {})
        if active:
            self.add("elbv2", region, "describe_tags", {"TagDescriptions": [{"ResourceArn": target}]}, {"ResourceArns": [target]})
            self.add("elbv2", region, "describe_target_health", {"TargetHealthDescriptions": [{"Target": {"Id": NODE_A, "Port": 8080},
                "TargetHealth": {"State": "healthy"}}]}, {"TargetGroupArn": target})
        self.add("elb", region, "describe_load_balancers", {"LoadBalancerDescriptions": []}, {})

    def _network(self, region):
        active = region == WEST
        specifications = [
            ("describe_vpcs", "Vpcs", [{"VpcId": VPC, "State": "available", "CidrBlock": "10.0.0.0/16", "Tags": [{"Key": "Name", "Value": "application-network"}]}]),
            ("describe_subnets", "Subnets", [{"SubnetId": SUBNET, "VpcId": VPC, "CidrBlock": "10.0.1.0/24", "State": "available", "AvailabilityZone": WEST+"a"}]),
            ("describe_security_groups", "SecurityGroups", [{"GroupId": SG, "GroupName": "application-sg", "VpcId": VPC, "Description": "Synthetic application security group", "OwnerId": ACCOUNT}]),
            ("describe_network_interfaces", "NetworkInterfaces", []),
            ("describe_nat_gateways", "NatGateways", [{"NatGatewayId": "nat-00000000000000001", "VpcId": VPC, "SubnetId": SUBNET,
                                                     "State": "available", "Tags": [{"Key": "Name", "Value": "application-nat"}]}]),
            ("describe_route_tables", "RouteTables", []), ("describe_internet_gateways", "InternetGateways", []),
            ("describe_vpc_endpoints", "VpcEndpoints", []),
            ("describe_addresses", "Addresses", [{"AllocationId": "eipalloc-00000000000000001", "PublicIp": "203.0.113.10", "Domain": "vpc"}]),
            ("describe_network_acls", "NetworkAcls", []), ("describe_vpc_peering_connections", "VpcPeeringConnections", []),
            ("describe_transit_gateways", "TransitGateways", []), ("describe_transit_gateway_attachments", "TransitGatewayAttachments", []),
        ]
        for operation, key, entries in specifications:
            self.add("ec2", region, operation, {key: entries if active else []}, {})

    def _global_resources(self):
        bucket = "demo-static-assets-123456789012"
        self.add("s3", WEST, "list_buckets", {"Buckets": [{"Name": bucket, "CreationDate": CREATED, "BucketRegion": WEST}]}, {"MaxBuckets": 1000})
        params = {"Bucket": bucket, "ExpectedBucketOwner": ACCOUNT}
        self.add("s3", WEST, "get_bucket_tagging", {"TagSet": []}, params)
        self.add("s3", WEST, "get_bucket_versioning", {"Status": "Enabled"}, params)
        self.add("s3", WEST, "get_bucket_encryption", {"ServerSideEncryptionConfiguration": {"Rules": [
            {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}}, params)
        self.add("s3", WEST, "get_public_access_block", {"PublicAccessBlockConfiguration": {
            key: True for key in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")}}, params)
        distribution_arn = f"arn:aws:cloudfront::{ACCOUNT}:distribution/EDISTRIBUTION1"
        distribution = {"Id": "EDISTRIBUTION1", "ARN": distribution_arn, "Status": "Deployed", "LastModifiedTime": CREATED,
            "DomainName": "d111111abcdef8.cloudfront.net", "Aliases": {"Quantity": 0},
            "Origins": {"Quantity": 1, "Items": [{"Id": "assets-origin", "DomainName": bucket+".s3.amazonaws.com",
                                                "S3OriginConfig": {"OriginAccessIdentity": ""}}]},
            "DefaultCacheBehavior": {"TargetOriginId": "assets-origin", "ViewerProtocolPolicy": "redirect-to-https",
                                     "TrustedSigners": {"Enabled": False, "Quantity": 0}, "MinTTL": 0,
                                     "ForwardedValues": {"QueryString": False, "Cookies": {"Forward": "none"}}},
            "CacheBehaviors": {"Quantity": 0}, "CustomErrorResponses": {"Quantity": 0}, "Comment": "Synthetic static website",
            "PriceClass": "PriceClass_100", "Enabled": True, "ViewerCertificate": {"CloudFrontDefaultCertificate": True},
            "Restrictions": {"GeoRestriction": {"RestrictionType": "none", "Quantity": 0}}, "WebACLId": "",
            "HttpVersion": "http2", "IsIPV6Enabled": True, "Staging": False}
        self.add("cloudfront", WEST, "list_distributions", {"DistributionList": {"Marker": "", "MaxItems": 100,
            "IsTruncated": False, "Quantity": 1, "Items": [distribution]}}, {})
        self.add("cloudfront", WEST, "list_tags_for_resource", {"Tags": {"Items": []}}, {"Resource": distribution_arn})
        zone = {"Id": "/hostedzone/ZDEMO123", "Name": "demo.example.", "CallerReference": "offline-simulation",
                "Config": {"PrivateZone": False}, "ResourceRecordSetCount": 4}
        self.add("route53", WEST, "list_hosted_zones", {"HostedZones": [zone], "Marker": "", "IsTruncated": False, "MaxItems": "100"}, {})
        self.add("route53", WEST, "list_tags_for_resource", {"ResourceTagSet": {"ResourceType": "hostedzone", "ResourceId": "ZDEMO123"}},
                 {"ResourceType": "hostedzone", "ResourceId": "ZDEMO123"})
        self.add("route53", WEST, "get_hosted_zone", {"HostedZone": zone,
            "DelegationSet": {"NameServers": ["ns1.demo.example", "ns2.demo.example"]}}, {"Id": "ZDEMO123"})

    def _pricing(self):
        # All additional resources are intentionally unsupported by the current
        # estimator except RDS. Never inject fake hourly matches to hide that gap.
        super()._pricing()
        filters = {"regionCode": WEST, "productFamily": "Database Instance", "instanceType": "db.t4g.medium",
                   "databaseEngine": "PostgreSQL", "deploymentOption": "Single-AZ"}
        self.add("pricing", "us-east-1", "get_products", {"FormatVersion": "aws_v1", "PriceList": [
            _product("FAKE-RDS-POSTGRES", "Database Instance", "Hrs", "0.15", regionCode=WEST)]},
            {"ServiceCode": "AmazonRDS", "FormatVersion": "aws_v1", "MaxResults": 100,
             "Filters": [{"Type": "TERM_MATCH", "Field": key, "Value": value} for key, value in sorted(filters.items())]})
