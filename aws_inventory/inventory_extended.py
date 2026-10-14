"""Read-only collectors for the additional service families.

Only allowlisted metadata is exported. Configuration responses may include job
arguments or other sensitive fields in memory; those fields are not exported.
No APIs for secret values, model invocation, messages or object contents are used.
"""
from .inventory_services import _emit


def pick(value, *keys):
    return {key: value[key] for key in keys if key in value}


def emit(ctx, service, kind, identifier, region, value, *, name=None, arn=None,
         parent=None, links=(), fields=(), **kwargs):
    return _emit(ctx, service, kind, identifier, region, name or identifier,
                 resource_arn=arn, parent_id=parent,
                 related_resource_ids=list(filter(None, links)),
                 relationship_source=f"{service}:native SDK metadata",
                 details=pick(value, *fields), **kwargs)


def collect_fsx(ctx, region):
    for fs in ctx.safe_pages("fsx", "describe_file_systems", region, "FileSystems"):
        config = next((fs[k] for k in ("WindowsConfiguration", "LustreConfiguration", "OntapConfiguration", "OpenZFSConfiguration") if k in fs), {})
        emit(ctx, "fsx", "file_system", fs["FileSystemId"], region, fs, arn=fs.get("ResourceARN"),
             state=fs.get("Lifecycle"), storage_gib=fs.get("StorageCapacity"), storage_type=fs.get("StorageType"),
             engine=fs.get("FileSystemType"), vpc_id=fs.get("VpcId"), subnet_ids=fs.get("SubnetIds", []),
             links=fs.get("NetworkInterfaceIds", []), fields=("FileSystemType", "KmsKeyId"),
             throughput_mib_s=config.get("ThroughputCapacity"))
    for volume in ctx.safe_pages("fsx", "describe_volumes", region, "Volumes"):
        emit(ctx, "fsx", "volume", volume["VolumeId"], region, volume, name=volume.get("Name"),
             arn=volume.get("ResourceARN"), parent=volume.get("FileSystemId"),
             state=volume.get("Lifecycle"), fields=("VolumeType",))


def collect_backup(ctx, region):
    for vault in ctx.safe_pages("backup", "list_backup_vaults", region, "BackupVaultList"):
        name = vault["BackupVaultName"]
        emit(ctx, "backup", "vault", name, region, vault, arn=vault.get("BackupVaultArn"),
             fields=("NumberOfRecoveryPoints", "Locked", "EncryptionKeyArn"))
        for point in ctx.safe_pages("backup", "list_recovery_points_by_backup_vault", region, "RecoveryPoints", BackupVaultName=name):
            emit(ctx, "backup", "recovery_point", point["RecoveryPointArn"], region, point,
                 name=point.get("ResourceName"), arn=point["RecoveryPointArn"], parent=name,
                 links=[point.get("ResourceArn")], storage_bytes=point.get("BackupSizeInBytes"),
                 state=point.get("Status"), fields=("ResourceType", "StorageClass", "CreationDate", "IsEncrypted"))


def collect_apigateway(ctx, region):
    for api in ctx.safe_pages("apigateway", "get_rest_apis", region, "items"):
        emit(ctx, "apigateway", "rest_api", api["id"], region, api, name=api.get("name"),
             fields=("endpointConfiguration", "disableExecuteApiEndpoint"))
        for stage in (ctx.safe_call("apigateway", "get_stages", region, restApiId=api["id"]) or {}).get("item", []):
            emit(ctx, "apigateway", "stage", api["id"]+"/"+stage["stageName"], region, stage,
                 name=stage["stageName"], parent=api["id"], fields=("cacheClusterEnabled", "cacheClusterSize", "deploymentId"))
    for api in ctx.safe_pages("apigatewayv2", "get_apis", region, "Items"):
        emit(ctx, "apigateway", "http_or_websocket_api", api["ApiId"], region, api,
             name=api.get("Name"), fields=("ProtocolType", "DisableExecuteApiEndpoint"))
        for integration in ctx.safe_pages("apigatewayv2", "get_integrations", region, "Items", ApiId=api["ApiId"]):
            # An integration URI may contain a query secret: retain only ARN targets.
            target = integration.get("IntegrationUri", "")
            emit(ctx, "apigateway", "integration", api["ApiId"]+"/"+integration["IntegrationId"], region, integration,
                 parent=api["ApiId"], links=[target] if target.startswith("arn:") else [], fields=("IntegrationType", "ConnectionType"))


def collect_msk(ctx, region):
    for cluster in ctx.safe_pages("kafka", "list_clusters_v2", region, "ClusterInfoList"):
        provisioned = cluster.get("Provisioned", {})
        broker = provisioned.get("BrokerNodeGroupInfo", {})
        emit(ctx, "msk", "cluster", cluster["ClusterArn"], region, cluster,
             name=cluster.get("ClusterName"), arn=cluster["ClusterArn"], state=cluster.get("State"),
             instance_type=broker.get("InstanceType"), subnet_ids=broker.get("ClientSubnets", []),
             security_group_ids=broker.get("SecurityGroups", []), fields=("ClusterType",),
             notes=f"Provisioned broker count: {provisioned.get('NumberOfBrokerNodes', 'unknown')}. Serverless usage is not inferred.")


def collect_kinesis(ctx, region):
    for stream in ctx.safe_pages("kinesis", "list_streams", region, "StreamNames"):
        response = ctx.safe_call("kinesis", "describe_stream_summary", region, StreamName=stream)
        data = (response or {}).get("StreamDescriptionSummary", {})
        emit(ctx, "kinesis", "stream", stream, region, data, arn=data.get("StreamARN"),
             state=data.get("StreamStatus"), fields=("OpenShardCount", "RetentionPeriodHours", "StreamModeDetails", "EncryptionType"))


def collect_emr(ctx, region):
    for summary in ctx.safe_pages("emr", "list_clusters", region, "Clusters", ClusterStates=["STARTING", "BOOTSTRAPPING", "RUNNING", "WAITING"]):
        cluster = (ctx.safe_call("emr", "describe_cluster", region, ClusterId=summary["Id"]) or {}).get("Cluster", summary)
        instances = ctx.safe_pages("emr", "list_instances", region, "Instances", ClusterId=summary["Id"])
        created = cluster.get('Status', {}).get('Timeline', {}).get('CreationDateTime')
        if created is None:
            created = summary.get('Status', {}).get('Timeline', {}).get('CreationDateTime')
        emit(ctx, "emr", "cluster", summary["Id"], region, cluster, name=cluster.get("Name"),
             arn=cluster.get("ClusterArn"), links=[i.get("Ec2InstanceId") for i in instances],
             created_at=created,
             state=cluster.get("Status", {}).get("State"), fields=("ReleaseLabel", "InstanceCollectionType"))


def collect_glue(ctx, region):
    for job in ctx.safe_pages("glue", "get_jobs", region, "Jobs"):
        emit(ctx, "glue", "job", job["Name"], region, job,
             fields=("WorkerType", "NumberOfWorkers", "MaxCapacity", "GlueVersion", "ExecutionClass", "Timeout"))


def collect_athena(ctx, region):
    for group in ctx.safe_pages("athena", "list_work_groups", region, "WorkGroups"):
        emit(ctx, "athena", "workgroup", group["Name"], region, group,
             state=group.get("State"), fields=("EngineVersion", "CreationTime"))


def collect_sagemaker(ctx, region):
    for endpoint in ctx.safe_pages("sagemaker", "list_endpoints", region, "Endpoints"):
        name = endpoint["EndpointName"]
        data = ctx.safe_call("sagemaker", "describe_endpoint", region, EndpointName=name) or endpoint
        config_name = data.get("EndpointConfigName")
        config = (ctx.safe_call("sagemaker", "describe_endpoint_config", region, EndpointConfigName=config_name) or {}) if config_name else {}
        emit(ctx, "sagemaker", "endpoint", name, region, data, arn=data.get("EndpointArn"),
             state=data.get("EndpointStatus"), links=[v.get("ModelName") for v in config.get("ProductionVariants", [])],
             fields=("EndpointConfigName",))
        for variant in config.get("ProductionVariants", []):
            emit(ctx, "sagemaker", "variant", name+"/"+variant["VariantName"], region, variant,
                 parent=name, instance_type=variant.get("InstanceType"),
                 fields=("InitialInstanceCount", "ServerlessConfig", "VolumeSizeInGB", "ModelName"))


def collect_bedrock(ctx, region):
    for model in ctx.safe_pages("bedrock", "list_provisioned_model_throughputs", region, "provisionedModelSummaries"):
        emit(ctx, "bedrock", "provisioned_throughput", model["provisionedModelArn"], region, model,
             name=model.get("provisionedModelName"), arn=model["provisionedModelArn"], state=model.get("status"),
             links=[model.get("modelArn")], fields=("modelUnits", "commitmentDuration", "commitmentExpirationTime"))
    for profile in ctx.safe_pages("bedrock", "list_inference_profiles", region, "inferenceProfileSummaries", typeEquals="APPLICATION"):
        emit(ctx, "bedrock", "inference_profile", profile["inferenceProfileArn"], region, profile,
             name=profile.get("inferenceProfileName"), arn=profile["inferenceProfileArn"], state=profile.get("status"),
             links=[m.get("modelArn") for m in profile.get("models", [])], fields=("type",))


def collect_eventbridge(ctx, region):
    for bus in ctx.safe_pages("events", "list_event_buses", region, "EventBuses"):
        name = bus["Name"]
        emit(ctx, "eventbridge", "event_bus", name, region, bus, arn=bus.get("Arn"))
        for rule in ctx.safe_pages("events", "list_rules", region, "Rules", EventBusName=name):
            targets = ctx.safe_pages("events", "list_targets_by_rule", region, "Targets", EventBusName=name, Rule=rule["Name"])
            emit(ctx, "eventbridge", "rule", rule["Arn"], region, rule, name=rule["Name"], arn=rule["Arn"],
                 parent=name, links=[t.get("Arn") for t in targets], state=rule.get("State"), fields=("ScheduleExpression",))


def collect_stepfunctions(ctx, region):
    for machine in ctx.safe_pages("stepfunctions", "list_state_machines", region, "stateMachines"):
        emit(ctx, "stepfunctions", "state_machine", machine["stateMachineArn"], region, machine,
             name=machine.get("name"), arn=machine["stateMachineArn"], fields=("type", "creationDate"))


def collect_ecr(ctx, region):
    for repository in ctx.safe_pages("ecr", "describe_repositories", region, "repositories"):
        name = repository["repositoryName"]
        images = ctx.safe_pages("ecr", "describe_images", region, "imageDetails", repositoryName=name)
        emit(ctx, "ecr", "repository", name, region, repository, arn=repository.get("repositoryArn"),
             fields=("imageTagMutability", "encryptionConfiguration"),
             notes="Image sizes are not summed: shared layers and manifest lists would overcount billed storage.")
        for image in images:
            emit(ctx, "ecr", "image", name+"@"+image["imageDigest"], region, image,
                 parent=name, storage_bytes=image.get("imageSizeInBytes"), fields=("imageTags", "imagePushedAt"))


def collect_kms(ctx, region):
    for item in ctx.safe_pages("kms", "list_keys", region, "Keys"):
        key = (ctx.safe_call("kms", "describe_key", region, KeyId=item["KeyId"]) or {}).get("KeyMetadata", {})
        emit(ctx, "kms", "key", item["KeyId"], region, key, arn=key.get("Arn", item.get("KeyArn")),
             state=key.get("KeyState"), fields=("KeyManager", "KeySpec", "KeyUsage", "MultiRegion", "Origin"))


def collect_secretsmanager(ctx, region):
    for secret in ctx.safe_pages("secretsmanager", "list_secrets", region, "SecretList"):
        emit(ctx, "secretsmanager", "secret", secret["ARN"], region, secret, name=secret.get("Name"),
             arn=secret["ARN"], links=[secret.get("KmsKeyId")], fields=("RotationEnabled", "PrimaryRegion", "DeletedDate"))


def waf_scope(ctx, region, scope):
    for acl in ctx.safe_pages("wafv2", "list_web_acls", region, "WebACLs", Scope=scope):
        links = []
        if scope == "REGIONAL":
            # Other WAF-supported resource types remain documented as unresolved.
            links = (ctx.safe_call("wafv2", "list_resources_for_web_acl", region,
                                  WebACLArn=acl["ARN"], ResourceType="APPLICATION_LOAD_BALANCER") or {}).get("ResourceArns", [])
        emit(ctx, "waf", "web_acl", acl["ARN"], "global" if scope == "CLOUDFRONT" else region,
             acl, name=acl.get("Name"), arn=acl["ARN"], links=links,
             notes="Regional associations cover ALBs; CloudFront associations are resolved from distribution metadata. Rule bodies are not collected.")


def collect_waf(ctx, region):
    waf_scope(ctx, region, "REGIONAL")


def collect_global_waf(ctx, region):
    waf_scope(ctx, "us-east-1", "CLOUDFRONT")


EXTENDED_COLLECTORS = {name: globals()["collect_"+name] for name in (
    "fsx", "backup", "apigateway", "msk", "kinesis", "emr", "glue", "athena",
    "sagemaker", "bedrock", "eventbridge", "stepfunctions", "ecr", "kms", "secretsmanager", "waf")}
