"""Managed service collectors: metadata only, never application contents."""

from __future__ import annotations

from botocore.exceptions import BotoCoreError, ClientError

from .common import chunks, error_message, resource_name, tags_dict


def _pick(data, *keys):
    return {key: data[key] for key in keys if key in data}


def _emit(ctx, service, kind, identifier, region, native_name=None, tags=None, **fields):
    normalized_tags = tags_dict(tags)
    name, source = resource_name(native_name, normalized_tags, identifier)
    return ctx.emit(service, kind, identifier, region, resource_name=name,
                    name_source=source, tags=normalized_tags, **fields)


def _optional(ctx, service, operation, region, absent_codes=(), **kwargs):
    """An absent optional configuration is not an inventory error."""
    try:
        return ctx.call(service, operation, region, **kwargs)
    except (BotoCoreError, ClientError) as exc:
        code, message = error_message(exc)
        if code in absent_codes:
            return {}
        ctx.issue(service, region, operation, message, code=code)
        return None


def _rds_tags(ctx, region, resource):
    if "TagList" in resource:
        return resource["TagList"]
    arn = (resource.get("DBInstanceArn") or resource.get("DBClusterArn")
           or resource.get("DBSnapshotArn") or resource.get("DBClusterSnapshotArn"))
    if not arn:
        return []
    return (ctx.safe_call("rds", "list_tags_for_resource", region,
                          ResourceName=arn) or {}).get("TagList", [])


def collect_rds(ctx, region):
    for db in ctx.safe_pages("rds", "describe_db_instances", region, "DBInstances"):
        identifier = db["DBInstanceIdentifier"]
        subnet = db.get("DBSubnetGroup", {})
        cluster = db.get("DBClusterIdentifier", "")
        _emit(ctx, "rds", "db_instance", identifier, region, identifier,
              _rds_tags(ctx, region, db), resource_arn=db.get("DBInstanceArn"),
              state=db.get("DBInstanceStatus"), instance_type=db.get("DBInstanceClass"),
              engine=db.get("Engine"), engine_version=db.get("EngineVersion"),
              availability_zone=db.get("AvailabilityZone"), vpc_id=subnet.get("VpcId"),
              subnet_ids=[s["SubnetIdentifier"] for s in subnet.get("Subnets", [])],
              security_group_ids=[g["VpcSecurityGroupId"] for g in db.get("VpcSecurityGroups", [])],
              cluster_name=cluster, parent_id=cluster,
              related_resource_ids=db.get("ReadReplicaDBInstanceIdentifiers", []),
              relationship_source="rds.DBClusterIdentifier" if cluster else "",
              storage_gib=db.get("AllocatedStorage"), storage_type=db.get("StorageType"),
              iops=db.get("Iops"), throughput_mib_s=db.get("StorageThroughput"),
              encrypted=db.get("StorageEncrypted"), multi_az=db.get("MultiAZ"),
              created_at=db.get("InstanceCreateTime"),
              details={"license_model": db.get("LicenseModel"),
                       "outpost_arns": [s["SubnetOutpost"]["Arn"] for s in subnet.get("Subnets", [])
                                         if s.get("SubnetOutpost", {}).get("Arn")],
                       "endpoint": db.get("Endpoint"),
                       "publicly_accessible": db.get("PubliclyAccessible"),
                       "backup_retention_days": db.get("BackupRetentionPeriod"),
                       "deletion_protection": db.get("DeletionProtection"),
                       "max_allocated_storage_gib": db.get("MaxAllocatedStorage"),
                       "replica_source": db.get("ReadReplicaSourceDBInstanceIdentifier"),
                       "pending_modifications": _pick(db.get("PendingModifiedValues", {}),
                                                       "DBInstanceClass", "AllocatedStorage",
                                                       "EngineVersion", "StorageType", "Iops")},
              notes="AllocatedStorage is allocated capacity; Aurora storage belongs to the cluster.")
    for db in ctx.safe_pages("rds", "describe_db_clusters", region, "DBClusters"):
        identifier = db["DBClusterIdentifier"]
        members = db.get("DBClusterMembers", [])
        _emit(ctx, "rds", "db_cluster", identifier, region, identifier,
              _rds_tags(ctx, region, db), resource_arn=db.get("DBClusterArn"),
              state=db.get("Status"), engine=db.get("Engine"),
              engine_version=db.get("EngineVersion"), cluster_name=identifier,
              availability_zone=db.get("AvailabilityZones"),
              security_group_ids=[g["VpcSecurityGroupId"] for g in db.get("VpcSecurityGroups", [])],
              related_resource_ids=[m["DBInstanceIdentifier"] for m in members],
              relationship_source="rds.DBClusterMembers", storage_type=db.get("StorageType"),
              storage_gib=db.get("AllocatedStorage"), iops=db.get("Iops"),
              encrypted=db.get("StorageEncrypted"), multi_az=db.get("MultiAZ"),
              created_at=db.get("ClusterCreateTime"),
              details={"engine_mode": db.get("EngineMode"), "endpoint": db.get("Endpoint"),
                       "reader_endpoint": db.get("ReaderEndpoint"),
                       "subnet_group": db.get("DBSubnetGroup"), "members": members,
                       "instance_class": db.get("DBClusterInstanceClass"),
                       "serverless_v2_scaling": db.get("ServerlessV2ScalingConfiguration"),
                       "serverless_scaling": db.get("ScalingConfigurationInfo"),
                       "backup_retention_days": db.get("BackupRetentionPeriod"),
                       "deletion_protection": db.get("DeletionProtection")},
              notes="Aurora allocated capacity does not necessarily represent billed bytes.")
    for operation, result_key, id_key, arn_key, parent_key, kind in (
        ("describe_db_snapshots", "DBSnapshots", "DBSnapshotIdentifier", "DBSnapshotArn",
         "DBInstanceIdentifier", "db_snapshot"),
        ("describe_db_cluster_snapshots", "DBClusterSnapshots", "DBClusterSnapshotIdentifier",
         "DBClusterSnapshotArn", "DBClusterIdentifier", "db_cluster_snapshot"),
    ):
        for snap in ctx.safe_pages("rds", operation, region, result_key,
                                   IncludeShared=False, IncludePublic=False):
            arn = snap.get(arn_key, "")
            if arn and arn.split(":")[4] != ctx.account_id:
                continue
            identifier = snap[id_key]
            _emit(ctx, "rds", kind, identifier, region, identifier,
                  _rds_tags(ctx, region, snap), resource_arn=arn,
                  state=snap.get("Status"), engine=snap.get("Engine"),
                  engine_version=snap.get("EngineVersion"), parent_id=snap.get(parent_key),
                  relationship_source=f"rds.{parent_key}", vpc_id=snap.get("VpcId"),
                  storage_gib=snap.get("AllocatedStorage"), storage_type=snap.get("StorageType"),
                  encrypted=snap.get("Encrypted", snap.get("StorageEncrypted")),
                  created_at=snap.get("SnapshotCreateTime"),
                  details=_pick(snap, "SnapshotType", "PercentProgress", "KmsKeyId"),
                  notes="Source database capacity; not the incremental billed snapshot size.")


def collect_s3(ctx, region):
    if not ctx.all_regions and not ctx.regions:
        return
    for bucket in ctx.safe_pages("s3", "list_buckets", region, "Buckets", MaxBuckets=1000):
        name = bucket["Name"]
        bucket_region = bucket.get("BucketRegion")
        if not bucket_region:
            location = ctx.safe_call("s3", "get_bucket_location", region,
                                     Bucket=name, ExpectedBucketOwner=ctx.account_id)
            if location is None:
                if ctx.all_regions:
                    _emit(ctx, "s3", "bucket", name, "unknown", name,
                          resource_arn=f"arn:{ctx.partition}:s3:::{name}",
                          created_at=bucket.get("CreationDate"),
                          notes="Region unavailable: details omitted; see row_type=error rows in the CSV.")
                continue
            bucket_region = location.get("LocationConstraint") or "us-east-1"
            if bucket_region == "EU":
                bucket_region = "eu-west-1"
        if not ctx.all_regions and bucket_region not in ctx.regions:
            continue
        params = {"Bucket": name, "ExpectedBucketOwner": ctx.account_id}
        tags = _optional(ctx, "s3", "get_bucket_tagging", bucket_region,
                         ("NoSuchTagSet",), **params)
        versioning = ctx.safe_call("s3", "get_bucket_versioning", bucket_region, **params)
        encryption = _optional(ctx, "s3", "get_bucket_encryption", bucket_region,
                               ("ServerSideEncryptionConfigurationNotFoundError",), **params)
        public_access = _optional(ctx, "s3", "get_public_access_block", bucket_region,
                                  ("NoSuchPublicAccessBlockConfiguration",), **params)
        rules = (encryption or {}).get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
        _emit(ctx, "s3", "bucket", name, bucket_region, name,
              (tags or {}).get("TagSet", []), resource_arn=f"arn:{ctx.partition}:s3:::{name}",
              created_at=bucket.get("CreationDate"),
              encrypted=True if rules else None,
              details={"versioning": _pick(versioning or {}, "Status", "MFADelete"),
                       "default_encryption": rules,
                       "public_access_block": (public_access or {}).get("PublicAccessBlockConfiguration"),
                       "bucket_kind": "general_purpose"},
              notes="Size not collected: no object scans or CloudWatch storage-class queries. "
                    "Default encryption does not verify existing objects. "
                    "Directory/S3 Express buckets and S3 Tables are excluded.")


def collect_lambda(ctx, region):
    for function in ctx.safe_pages("lambda", "list_functions", region, "Functions"):
        name = function["FunctionName"]
        arn = function["FunctionArn"]
        # list_functions may contain Environment: never export it.
        tags = ctx.safe_call("lambda", "list_tags", region, Resource=arn) or {}
        config = ctx.safe_call("lambda", "get_function_configuration", region,
                               FunctionName=name)
        data = config if config is not None else function
        vpc = data.get("VpcConfig", {})
        filesystems = data.get("FileSystemConfigs", [])
        _emit(ctx, "lambda", "function", name, region, name, tags.get("Tags", {}),
              resource_arn=arn, state=data.get("State"), engine=data.get("Runtime"),
              memory_mib=data.get("MemorySize"), vpc_id=vpc.get("VpcId"),
              subnet_ids=vpc.get("SubnetIds", []),
              security_group_ids=vpc.get("SecurityGroupIds", []),
              related_resource_ids=[f["Arn"] for f in filesystems],
              relationship_source="lambda.FileSystemConfigs" if filesystems else "",
              details={"architectures": data.get("Architectures"),
                       "timeout_seconds": data.get("Timeout"),
                       "package_type": data.get("PackageType"),
                       "code_size_bytes": data.get("CodeSize"),
                       "ephemeral_storage_mib": data.get("EphemeralStorage", {}).get("Size"),
                       "last_modified": data.get("LastModified"),
                       "last_update_status": data.get("LastUpdateStatus"),
                       "execution_role_arn": data.get("Role"),
                       "efs_mounts": filesystems,
                       "layers": [layer.get("Arn") for layer in data.get("Layers", [])],
                       "logging": _pick(data.get("LoggingConfig", {}), "LogGroup", "LogFormat")},
              notes="Only $LATEST configuration; environment variables and code are not exported.")


def collect_dynamodb(ctx, region):
    for name in ctx.safe_pages("dynamodb", "list_tables", region, "TableNames"):
        response = ctx.safe_call("dynamodb", "describe_table", region, TableName=name)
        if not response:
            _emit(ctx, "dynamodb", "table", name, region, name,
                  notes="DescribeTable unavailable; see row_type=error rows in the CSV.")
            continue
        table = response["Table"]
        arn = table.get("TableArn")
        tags = ctx.safe_pages("dynamodb", "list_tags_of_resource", region, "Tags",
                              ResourceArn=arn) if arn else []
        sse = table.get("SSEDescription", {})
        _emit(ctx, "dynamodb", "table", name, region, name, tags,
              resource_arn=arn, state=table.get("TableStatus"),
              storage_bytes=table.get("TableSizeBytes"), created_at=table.get("CreationDateTime"),
              details={"item_count": table.get("ItemCount"),
                       "billing_mode": table.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED"),
                       "provisioned_throughput": _pick(table.get("ProvisionedThroughput", {}),
                                                      "ReadCapacityUnits", "WriteCapacityUnits"),
                       "on_demand_throughput": table.get("OnDemandThroughput"),
                       "table_class": table.get("TableClassSummary"),
                       "encryption": sse,
                       "global_secondary_indexes": [
                           _pick(i, "IndexName", "IndexArn", "IndexStatus", "IndexSizeBytes",
                                 "ItemCount", "ProvisionedThroughput", "OnDemandThroughput")
                           for i in table.get("GlobalSecondaryIndexes", [])],
                       "local_secondary_indexes": [
                           _pick(i, "IndexName", "IndexArn", "IndexSizeBytes", "ItemCount")
                           for i in table.get("LocalSecondaryIndexes", [])],
                       "replicas": [_pick(r, "RegionName", "ReplicaStatus", "ReplicaTableClassSummary")
                                    for r in table.get("Replicas", [])],
                       "deletion_protection": table.get("DeletionProtectionEnabled")},
              notes="Approximate size and item count, updated periodically by AWS. "
                    "Indexes are listed separately in details; all DynamoDB data is encrypted at rest.")


def collect_efs(ctx, region):
    for fs in ctx.safe_pages("efs", "describe_file_systems", region, "FileSystems"):
        identifier = fs["FileSystemId"]
        size = fs.get("SizeInBytes", {})
        fs_tags = tags_dict(fs.get("Tags"))
        if fs.get("Name") and not fs_tags.get("Name"):
            fs_tags["Name"] = fs["Name"]
        _emit(ctx, "efs", "file_system", identifier, region, tags=fs_tags,
              resource_arn=fs.get("FileSystemArn"), state=fs.get("LifeCycleState"),
              availability_zone=fs.get("AvailabilityZoneName"),
              storage_bytes=size.get("Value"), encrypted=fs.get("Encrypted"),
              throughput_mib_s=fs.get("ProvisionedThroughputInMibps"),
              created_at=fs.get("CreationTime"),
              details={"size_measurement": size, "performance_mode": fs.get("PerformanceMode"),
                       "throughput_mode": fs.get("ThroughputMode"),
                       "mount_target_count": fs.get("NumberOfMountTargets"),
                       "kms_key_id": fs.get("KmsKeyId")},
              notes="SizeInBytes is the service measurement, not a file scan.")
        for mount in ctx.safe_pages("efs", "describe_mount_targets", region, "MountTargets",
                                     FileSystemId=identifier):
            mount_id = mount["MountTargetId"]
            groups = ctx.safe_call("efs", "describe_mount_target_security_groups", region,
                                   MountTargetId=mount_id) or {}
            _emit(ctx, "efs", "mount_target", mount_id, region,
                  state=mount.get("LifeCycleState"), parent_id=identifier, attached_to=[identifier],
                  relationship_source="efs.FileSystemId", vpc_id=mount.get("VpcId"),
                  subnet_ids=[mount["SubnetId"]] if mount.get("SubnetId") else [],
                  availability_zone=mount.get("AvailabilityZoneName"),
                  private_ips=[mount["IpAddress"]] if mount.get("IpAddress") else [],
                  security_group_ids=groups.get("SecurityGroups", []),
                  related_resource_ids=[mount["NetworkInterfaceId"]] if mount.get("NetworkInterfaceId") else [],
                  details=_pick(mount, "Ipv6Address", "NetworkInterfaceId"),
                  notes="Network mount target: does not prove that a host has mounted the file system.")


def _elasticache_tags(ctx, region, arn):
    return (ctx.safe_call("elasticache", "list_tags_for_resource", region,
                          ResourceName=arn) or {}).get("TagList", []) if arn else []


def collect_elasticache(ctx, region):
    subnet_groups = {g["CacheSubnetGroupName"]: g for g in
                     ctx.safe_pages("elasticache", "describe_cache_subnet_groups", region,
                                    "CacheSubnetGroups")}
    for cache in ctx.safe_pages("elasticache", "describe_cache_clusters", region,
                                "CacheClusters", ShowCacheNodeInfo=True):
        identifier = cache["CacheClusterId"]
        group = subnet_groups.get(cache.get("CacheSubnetGroupName"), {})
        replication_group = cache.get("ReplicationGroupId")
        _emit(ctx, "elasticache", "cache_cluster", identifier, region, identifier,
              _elasticache_tags(ctx, region, cache.get("ARN")), resource_arn=cache.get("ARN"),
              state=cache.get("CacheClusterStatus"), instance_type=cache.get("CacheNodeType"),
              engine=cache.get("Engine"), engine_version=cache.get("EngineVersion"),
              availability_zone=cache.get("PreferredAvailabilityZone"), vpc_id=group.get("VpcId"),
              subnet_ids=[s["SubnetIdentifier"] for s in group.get("Subnets", [])],
              security_group_ids=[g["SecurityGroupId"] for g in cache.get("SecurityGroups", [])],
              cluster_name=replication_group or identifier, parent_id=replication_group,
              relationship_source="elasticache.ReplicationGroupId" if replication_group else "",
              encrypted=cache.get("AtRestEncryptionEnabled"), created_at=cache.get("CacheClusterCreateTime"),
              details={"node_count": cache.get("NumCacheNodes"),
                       "transit_encryption_enabled": cache.get("TransitEncryptionEnabled"),
                       "configuration_endpoint": cache.get("ConfigurationEndpoint"),
                       "nodes": [_pick(n, "CacheNodeId", "CacheNodeStatus", "Endpoint",
                                       "CustomerAvailabilityZone", "CacheNodeCreateTime")
                                 for n in cache.get("CacheNodes", [])]})
    for group in ctx.safe_pages("elasticache", "describe_replication_groups", region,
                                "ReplicationGroups"):
        identifier = group["ReplicationGroupId"]
        _emit(ctx, "elasticache", "replication_group", identifier, region, identifier,
              _elasticache_tags(ctx, region, group.get("ARN")), resource_arn=group.get("ARN"),
              state=group.get("Status"), cluster_name=identifier,
              related_resource_ids=group.get("MemberClusters", []),
              relationship_source="elasticache.MemberClusters", encrypted=group.get("AtRestEncryptionEnabled"),
              multi_az=group.get("MultiAZ") == "enabled" if "MultiAZ" in group else None,
              details={"automatic_failover": group.get("AutomaticFailover"),
                       "cluster_enabled": group.get("ClusterEnabled"),
                       "transit_encryption_enabled": group.get("TransitEncryptionEnabled"),
                       "node_groups": group.get("NodeGroups", []),
                       "snapshot_retention_days": group.get("SnapshotRetentionLimit")})
    for cache in ctx.safe_pages("elasticache", "describe_serverless_caches", region,
                                "ServerlessCaches"):
        identifier = cache["ServerlessCacheName"]
        _emit(ctx, "elasticache", "serverless_cache", identifier, region, identifier,
              _elasticache_tags(ctx, region, cache.get("ARN")), resource_arn=cache.get("ARN"),
              state=cache.get("Status"), engine=cache.get("Engine"),
              engine_version=cache.get("FullEngineVersion", cache.get("MajorEngineVersion")),
              subnet_ids=cache.get("SubnetIds", []), security_group_ids=cache.get("SecurityGroupIds", []),
              created_at=cache.get("CreateTime"),
              details=_pick(cache, "Endpoint", "ReaderEndpoint", "CacheUsageLimits", "KmsKeyId",
                            "SnapshotRetentionLimit"),
              notes="CacheUsageLimits are configured limits, not measured usage.")


def collect_opensearch(ctx, region):
    response = ctx.safe_call("opensearch", "list_domain_names", region) or {}
    names = [domain["DomainName"] for domain in response.get("DomainNames", [])]
    for batch in chunks(names, 5):
        response = ctx.safe_call("opensearch", "describe_domains", region, DomainNames=batch)
        if response is None:
            for name in batch:
                _emit(ctx, "opensearch", "domain", name, region, name,
                      notes="DescribeDomains unavailable; see row_type=error rows in the CSV.")
            continue
        for domain in response.get("DomainStatusList", []):
            name = domain["DomainName"]
            arn = domain.get("ARN")
            tags = (ctx.safe_call("opensearch", "list_tags", region, ARN=arn) or {}).get("TagList", [])
            cluster = domain.get("ClusterConfig", {})
            ebs = domain.get("EBSOptions", {})
            vpc = domain.get("VPCOptions", {})
            encryption = domain.get("EncryptionAtRestOptions", {})
            _emit(ctx, "opensearch", "domain", domain.get("DomainId", name), region, name, tags,
                  resource_arn=arn, state="deleting" if domain.get("Deleted") else
                  ("processing" if domain.get("Processing") else "active"),
                  engine="OpenSearch", engine_version=domain.get("EngineVersion"),
                  instance_type=cluster.get("InstanceType"), cluster_name=name,
                  vpc_id=vpc.get("VPCId"), subnet_ids=vpc.get("SubnetIds", []),
                  security_group_ids=vpc.get("SecurityGroupIds", []),
                  availability_zone=vpc.get("AvailabilityZones", []),
                  storage_type=ebs.get("VolumeType"), iops=ebs.get("Iops"),
                  throughput_mib_s=ebs.get("Throughput"), encrypted=encryption.get("Enabled"),
                  multi_az=cluster.get("ZoneAwarenessEnabled"),
                  details={"cluster_config": cluster, "ebs_options_per_data_node": ebs,
                           "endpoint": domain.get("Endpoint"), "endpoints": domain.get("Endpoints"),
                           "node_to_node_encryption": domain.get("NodeToNodeEncryptionOptions"),
                           "encryption_at_rest": encryption},
                  notes="EBS VolumeSize in details is GiB per data node; do not sum it as a single volume. "
                        "OpenSearch Serverless is excluded.")


def collect_redshift(ctx, region):
    for cluster in ctx.safe_pages("redshift", "describe_clusters", region, "Clusters"):
        identifier = cluster["ClusterIdentifier"]
        nodes = cluster.get("ClusterNodes", [])
        _emit(ctx, "redshift", "cluster", identifier, region, identifier, cluster.get("Tags"),
              resource_arn=f"arn:{ctx.partition}:redshift:{region}:{ctx.account_id}:cluster:{identifier}",
              state=cluster.get("ClusterStatus"), instance_type=cluster.get("NodeType"),
              engine="redshift", engine_version=cluster.get("ClusterVersion"),
              cluster_name=identifier, availability_zone=cluster.get("AvailabilityZone"),
              vpc_id=cluster.get("VpcId"),
              security_group_ids=[g["VpcSecurityGroupId"] for g in cluster.get("VpcSecurityGroups", [])],
              private_ips=[n["PrivateIPAddress"] for n in nodes if n.get("PrivateIPAddress")],
              public_ips=[n["PublicIPAddress"] for n in nodes if n.get("PublicIPAddress")],
              encrypted=cluster.get("Encrypted"), created_at=cluster.get("ClusterCreateTime"),
              details={"number_of_nodes": cluster.get("NumberOfNodes"),
                       "endpoint": cluster.get("Endpoint"), "subnet_group": cluster.get("ClusterSubnetGroupName"),
                       "publicly_accessible": cluster.get("PubliclyAccessible"),
                       "nodes": [_pick(n, "NodeRole", "PrivateIPAddress", "PublicIPAddress") for n in nodes],
                       "total_storage_capacity_megabytes": cluster.get("TotalStorageCapacityInMegaBytes"),
                       "automated_snapshot_retention_days": cluster.get("AutomatedSnapshotRetentionPeriod"),
                       "multi_az": cluster.get("MultiAZ")},
              notes="Provisioned clusters; Redshift Serverless is excluded.")


def collect_sqs(ctx, region):
    for url in ctx.safe_pages("sqs", "list_queues", region, "QueueUrls", MaxResults=1000):
        name = url.rsplit("/", 1)[-1]
        # Do not request Policy: it may contain identifiers unnecessary for inventory.
        attributes = ctx.safe_call("sqs", "get_queue_attributes", region, QueueUrl=url,
                                   AttributeNames=["QueueArn", "ApproximateNumberOfMessages",
                                                   "ApproximateNumberOfMessagesNotVisible",
                                                   "ApproximateNumberOfMessagesDelayed",
                                                   "CreatedTimestamp", "MessageRetentionPeriod",
                                                   "VisibilityTimeout", "MaximumMessageSize",
                                                   "KmsMasterKeyId", "SqsManagedSseEnabled",
                                                   "RedrivePolicy", "FifoQueue"])
        data = (attributes or {}).get("Attributes", {})
        tags = (ctx.safe_call("sqs", "list_queue_tags", region, QueueUrl=url) or {}).get("Tags", {})
        _emit(ctx, "sqs", "queue", url, region, name, tags,
              resource_arn=data.get("QueueArn"),
              encrypted=(bool(data.get("KmsMasterKeyId")) or data.get("SqsManagedSseEnabled") == "true")
              if attributes is not None else None,
              details={"queue_url": url, **data},
              notes="Approximate message counts; no messages read. CreatedTimestamp is Unix UTC.")


def collect_sns(ctx, region):
    for topic in ctx.safe_pages("sns", "list_topics", region, "Topics"):
        arn = topic["TopicArn"]
        name = arn.rsplit(":", 1)[-1]
        tags = (ctx.safe_call("sns", "list_tags_for_resource", region,
                              ResourceArn=arn) or {}).get("Tags", [])
        attributes = ctx.safe_call("sns", "get_topic_attributes", region, TopicArn=arn)
        data = (attributes or {}).get("Attributes", {})
        _emit(ctx, "sns", "topic", arn, region, name, tags, resource_arn=arn,
              encrypted=bool(data.get("KmsMasterKeyId")) if attributes is not None else None,
              details=_pick(data, "DisplayName", "SubscriptionsConfirmed", "SubscriptionsPending",
                            "SubscriptionsDeleted", "FifoTopic", "ContentBasedDeduplication",
                            "KmsMasterKeyId", "SignatureVersion"),
              notes="Subscription recipients and messages are not read.")


def collect_cloudwatch(ctx, region):
    for group in ctx.safe_pages("logs", "describe_log_groups", region, "logGroups"):
        name = group["logGroupName"]
        arn = group.get("logGroupArn") or group.get("arn", "").removesuffix(":*")
        tags = (ctx.safe_call("logs", "list_tags_for_resource", region,
                              resourceArn=arn) or {}).get("tags", {}) if arn else {}
        _emit(ctx, "cloudwatch", "log_group", name, region, name, tags,
              resource_arn=arn, storage_bytes=group.get("storedBytes"),
              details={"retention_days": group.get("retentionInDays"),
                       "retention": "configured" if "retentionInDays" in group else "never_expire",
                       "kms_key_id": group.get("kmsKeyId"),
                       "log_group_class": group.get("logGroupClass"),
                       "metric_filter_count": group.get("metricFilterCount"),
                       "creation_time_unix_milliseconds": group.get("creationTime"),
                       "data_protection_status": group.get("dataProtectionStatus")},
              notes="Log groups and metadata only; events and streams are not read. "
                    "CloudWatch always encrypts logs at rest, including without a customer-managed KMS key.")


def collect_cloudfront(ctx, region):
    for distribution in ctx.safe_pages("cloudfront", "list_distributions", region,
                                       "DistributionList.Items"):
        identifier = distribution["Id"]
        arn = distribution.get("ARN")
        tags = (ctx.safe_call("cloudfront", "list_tags_for_resource", region,
                              Resource=arn) or {}).get("Tags", {}).get("Items", []) if arn else []
        # Origins can contain authentication headers: use an explicit field allowlist.
        origins = [_pick(origin, "Id", "DomainName", "OriginPath", "ConnectionAttempts",
                         "ConnectionTimeout", "OriginAccessControlId")
                   for origin in distribution.get("Origins", {}).get("Items", [])]
        _emit(ctx, "cloudfront", "distribution", identifier, "global",
              distribution.get("DomainName"), tags, resource_arn=arn,
              state=distribution.get("Status"),
              details={"domain_name": distribution.get("DomainName"),
                       "aliases": distribution.get("Aliases", {}).get("Items", []),
                       "enabled": distribution.get("Enabled"),
                       "price_class": distribution.get("PriceClass"),
                       "origins": origins,
                       "last_modified": distribution.get("LastModifiedTime"),
                       "http_version": distribution.get("HttpVersion"),
                       "ipv6_enabled": distribution.get("IsIPV6Enabled"),
                       "web_acl_id": distribution.get("WebACLId")})


def collect_route53(ctx, region):
    for zone in ctx.safe_pages("route53", "list_hosted_zones", region, "HostedZones"):
        identifier = zone["Id"].rsplit("/", 1)[-1]
        tags = (ctx.safe_call("route53", "list_tags_for_resource", region,
                              ResourceType="hostedzone", ResourceId=identifier) or {}).get(
                                  "ResourceTagSet", {}).get("Tags", [])
        response = ctx.safe_call("route53", "get_hosted_zone", region, Id=identifier) or {}
        _emit(ctx, "route53", "hosted_zone", identifier, "global", zone.get("Name"), tags,
              resource_arn=f"arn:{ctx.partition}:route53:::hostedzone/{identifier}",
              details={"private_zone": zone.get("Config", {}).get("PrivateZone"),
                       "record_set_count": zone.get("ResourceRecordSetCount"),
                       "associated_vpcs": response.get("VPCs", []),
                       "name_servers": response.get("DelegationSet", {}).get("NameServers", [])},
              notes="DNS records and health checks are not enumerated.")


REGIONAL_COLLECTORS = {
    "rds": collect_rds,
    "lambda": collect_lambda,
    "dynamodb": collect_dynamodb,
    "efs": collect_efs,
    "elasticache": collect_elasticache,
    "opensearch": collect_opensearch,
    "redshift": collect_redshift,
    "sqs": collect_sqs,
    "sns": collect_sns,
    "cloudwatch": collect_cloudwatch,
}

GLOBAL_COLLECTORS = {
    "s3": collect_s3,
    "cloudfront": collect_cloudfront,
    "route53": collect_route53,
}
