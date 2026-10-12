"""Explicit SDK operation allowlist and corresponding IAM permissions.

API names are not assumed to match IAM actions (notably S3 and API Gateway).
Keep this registry synchronized with collectors and the IAM example.
"""
from types import MappingProxyType


OPERATIONS = MappingProxyType({
    'apigateway': MappingProxyType({
        'get_rest_apis': 'apigateway:GET',
        'get_stages': 'apigateway:GET',
    }),
    'apigatewayv2': MappingProxyType({
        'get_apis': 'apigateway:GET',
        'get_integrations': 'apigateway:GET',
    }),
    'athena': MappingProxyType({
        'list_work_groups': 'athena:ListWorkGroups',
    }),
    'autoscaling': MappingProxyType({
        'describe_auto_scaling_groups': 'autoscaling:DescribeAutoScalingGroups',
    }),
    'backup': MappingProxyType({
        'list_backup_vaults': 'backup:ListBackupVaults',
        'list_recovery_points_by_backup_vault': 'backup:ListRecoveryPointsByBackupVault',
    }),
    'bedrock': MappingProxyType({
        'list_inference_profiles': 'bedrock:ListInferenceProfiles',
        'list_provisioned_model_throughputs': 'bedrock:ListProvisionedModelThroughputs',
    }),
    'ce': MappingProxyType({
        'get_cost_and_usage': 'ce:GetCostAndUsage',
        'get_cost_and_usage_with_resources': 'ce:GetCostAndUsageWithResources',
        'get_dimension_values': 'ce:GetDimensionValues',
    }),
    'cloudfront': MappingProxyType({
        'list_distributions': 'cloudfront:ListDistributions',
        'list_tags_for_resource': 'cloudfront:ListTagsForResource',
    }),
    'dynamodb': MappingProxyType({
        'describe_table': 'dynamodb:DescribeTable',
        'list_tables': 'dynamodb:ListTables',
        'list_tags_of_resource': 'dynamodb:ListTagsOfResource',
    }),
    'ec2': MappingProxyType({
        'describe_addresses': 'ec2:DescribeAddresses',
        'describe_instance_types': 'ec2:DescribeInstanceTypes',
        'describe_instances': 'ec2:DescribeInstances',
        'describe_internet_gateways': 'ec2:DescribeInternetGateways',
        'describe_nat_gateways': 'ec2:DescribeNatGateways',
        'describe_network_acls': 'ec2:DescribeNetworkAcls',
        'describe_network_interfaces': 'ec2:DescribeNetworkInterfaces',
        'describe_regions': 'ec2:DescribeRegions',
        'describe_route_tables': 'ec2:DescribeRouteTables',
        'describe_security_groups': 'ec2:DescribeSecurityGroups',
        'describe_snapshots': 'ec2:DescribeSnapshots',
        'describe_subnets': 'ec2:DescribeSubnets',
        'describe_transit_gateway_attachments': 'ec2:DescribeTransitGatewayAttachments',
        'describe_transit_gateways': 'ec2:DescribeTransitGateways',
        'describe_volumes': 'ec2:DescribeVolumes',
        'describe_vpc_endpoints': 'ec2:DescribeVpcEndpoints',
        'describe_vpc_peering_connections': 'ec2:DescribeVpcPeeringConnections',
        'describe_vpcs': 'ec2:DescribeVpcs',
        'describe_vpn_connections': 'ec2:DescribeVpnConnections',
    }),
    'ecr': MappingProxyType({
        'describe_images': 'ecr:DescribeImages',
        'describe_repositories': 'ecr:DescribeRepositories',
    }),
    'ecs': MappingProxyType({
        'describe_clusters': 'ecs:DescribeClusters',
        'describe_container_instances': 'ecs:DescribeContainerInstances',
        'describe_services': 'ecs:DescribeServices',
        'describe_task_definition': 'ecs:DescribeTaskDefinition',
        'describe_tasks': 'ecs:DescribeTasks',
        'list_clusters': 'ecs:ListClusters',
        'list_container_instances': 'ecs:ListContainerInstances',
        'list_services': 'ecs:ListServices',
        'list_tasks': 'ecs:ListTasks',
    }),
    'efs': MappingProxyType({
        'describe_file_systems': 'elasticfilesystem:DescribeFileSystems',
        'describe_mount_target_security_groups': 'elasticfilesystem:DescribeMountTargetSecurityGroups',
        'describe_mount_targets': 'elasticfilesystem:DescribeMountTargets',
    }),
    'eks': MappingProxyType({
        'describe_addon': 'eks:DescribeAddon',
        'describe_cluster': 'eks:DescribeCluster',
        'describe_fargate_profile': 'eks:DescribeFargateProfile',
        'describe_nodegroup': 'eks:DescribeNodegroup',
        'list_addons': 'eks:ListAddons',
        'list_clusters': 'eks:ListClusters',
        'list_fargate_profiles': 'eks:ListFargateProfiles',
        'list_nodegroups': 'eks:ListNodegroups',
    }),
    'elasticache': MappingProxyType({
        'describe_cache_clusters': 'elasticache:DescribeCacheClusters',
        'describe_cache_subnet_groups': 'elasticache:DescribeCacheSubnetGroups',
        'describe_replication_groups': 'elasticache:DescribeReplicationGroups',
        'describe_serverless_caches': 'elasticache:DescribeServerlessCaches',
        'list_tags_for_resource': 'elasticache:ListTagsForResource',
    }),
    'elb': MappingProxyType({
        'describe_load_balancers': 'elasticloadbalancing:DescribeLoadBalancers',
        'describe_tags': 'elasticloadbalancing:DescribeTags',
    }),
    'elbv2': MappingProxyType({
        'describe_listeners': 'elasticloadbalancing:DescribeListeners',
        'describe_load_balancers': 'elasticloadbalancing:DescribeLoadBalancers',
        'describe_tags': 'elasticloadbalancing:DescribeTags',
        'describe_target_groups': 'elasticloadbalancing:DescribeTargetGroups',
        'describe_target_health': 'elasticloadbalancing:DescribeTargetHealth',
    }),
    'emr': MappingProxyType({
        'describe_cluster': 'elasticmapreduce:DescribeCluster',
        'list_clusters': 'elasticmapreduce:ListClusters',
        'list_instances': 'elasticmapreduce:ListInstances',
    }),
    'events': MappingProxyType({
        'list_event_buses': 'events:ListEventBuses',
        'list_rules': 'events:ListRules',
        'list_targets_by_rule': 'events:ListTargetsByRule',
    }),
    'fsx': MappingProxyType({
        'describe_file_systems': 'fsx:DescribeFileSystems',
        'describe_volumes': 'fsx:DescribeVolumes',
    }),
    'glue': MappingProxyType({
        'get_jobs': 'glue:GetJobs',
    }),
    'kafka': MappingProxyType({
        'list_clusters_v2': 'kafka:ListClustersV2',
    }),
    'kinesis': MappingProxyType({
        'describe_stream_summary': 'kinesis:DescribeStreamSummary',
        'list_streams': 'kinesis:ListStreams',
    }),
    'kms': MappingProxyType({
        'describe_key': 'kms:DescribeKey',
        'list_keys': 'kms:ListKeys',
    }),
    'lambda': MappingProxyType({
        'get_function_configuration': 'lambda:GetFunctionConfiguration',
        'list_functions': 'lambda:ListFunctions',
        'list_tags': 'lambda:ListTags',
    }),
    'logs': MappingProxyType({
        'describe_log_groups': 'logs:DescribeLogGroups',
        'list_tags_for_resource': 'logs:ListTagsForResource',
    }),
    'opensearch': MappingProxyType({
        'describe_domains': 'es:DescribeDomains',
        'list_domain_names': 'es:ListDomainNames',
        'list_tags': 'es:ListTags',
    }),
    'pricing': MappingProxyType({
        'get_products': 'pricing:GetProducts',
    }),
    'rds': MappingProxyType({
        'describe_db_cluster_snapshots': 'rds:DescribeDBClusterSnapshots',
        'describe_db_clusters': 'rds:DescribeDBClusters',
        'describe_db_instances': 'rds:DescribeDBInstances',
        'describe_db_snapshots': 'rds:DescribeDBSnapshots',
        'list_tags_for_resource': 'rds:ListTagsForResource',
    }),
    'redshift': MappingProxyType({
        'describe_clusters': 'redshift:DescribeClusters',
    }),
    'route53': MappingProxyType({
        'get_hosted_zone': 'route53:GetHostedZone',
        'list_hosted_zones': 'route53:ListHostedZones',
        'list_tags_for_resource': 'route53:ListTagsForResource',
    }),
    's3': MappingProxyType({
        'get_bucket_encryption': 's3:GetEncryptionConfiguration',
        'get_bucket_location': 's3:GetBucketLocation',
        'get_bucket_tagging': 's3:GetBucketTagging',
        'get_bucket_versioning': 's3:GetBucketVersioning',
        'get_public_access_block': 's3:GetBucketPublicAccessBlock',
        'list_buckets': 's3:ListAllMyBuckets',
    }),
    'sagemaker': MappingProxyType({
        'describe_endpoint': 'sagemaker:DescribeEndpoint',
        'describe_endpoint_config': 'sagemaker:DescribeEndpointConfig',
        'list_endpoints': 'sagemaker:ListEndpoints',
    }),
    'secretsmanager': MappingProxyType({
        'list_secrets': 'secretsmanager:ListSecrets',
    }),
    'sns': MappingProxyType({
        'get_topic_attributes': 'sns:GetTopicAttributes',
        'list_tags_for_resource': 'sns:ListTagsForResource',
        'list_topics': 'sns:ListTopics',
    }),
    'sqs': MappingProxyType({
        'get_queue_attributes': 'sqs:GetQueueAttributes',
        'list_queue_tags': 'sqs:ListQueueTags',
        'list_queues': 'sqs:ListQueues',
    }),
    'stepfunctions': MappingProxyType({
        'list_state_machines': 'states:ListStateMachines',
    }),
    'sts': MappingProxyType({
        'get_caller_identity': 'sts:GetCallerIdentity',
    }),
    'wafv2': MappingProxyType({
        'list_resources_for_web_acl': 'wafv2:ListResourcesForWebACL',
        'list_web_acls': 'wafv2:ListWebACLs',
    }),
})


def require_read_operation(service: str, operation: str) -> None:
    """Fail closed before creating a client or sending an unknown operation."""
    if operation not in OPERATIONS.get(service, {}):
        raise ValueError(f"Operation is not in the inventory read allowlist: {service}.{operation}")


def example_policy() -> dict:
    """Full-coverage, action-scoped IAM example for the commercial partition.

    This is an identity policy, not a permissions boundary. Restrict resources
    and remove unused operations for the actual deployment.
    """
    all_actions = {action for operations in OPERATIONS.values() for action in operations.values()}
    special_prefixes = ('s3:', 'apigateway:', 'ce:', 'pricing:', 'sts:')
    metadata = sorted(action for action in all_actions if not action.startswith(special_prefixes))
    statements = [
        {'Sid': 'InventoryMetadataRead', 'Effect': 'Allow', 'Action': metadata, 'Resource': '*'},
        {'Sid': 'CallerIdentity', 'Effect': 'Allow', 'Action': ['sts:GetCallerIdentity'], 'Resource': '*'},
        {'Sid': 'BucketDiscovery', 'Effect': 'Allow', 'Action': ['s3:ListAllMyBuckets'], 'Resource': '*'},
        {'Sid': 'BucketConfiguration', 'Effect': 'Allow',
         'Action': sorted(set(OPERATIONS['s3'].values()) - {'s3:ListAllMyBuckets'}),
         'Resource': 'arn:aws:s3:::*'},
        {'Sid': 'ApiGatewayInventory', 'Effect': 'Allow', 'Action': ['apigateway:GET'],
         'Resource': ['arn:aws:apigateway:*::/restapis', 'arn:aws:apigateway:*::/restapis/*/stages',
                      'arn:aws:apigateway:*::/apis', 'arn:aws:apigateway:*::/apis/*/integrations']},
        {'Sid': 'ServiceBilling', 'Effect': 'Allow',
         'Action': ['ce:GetCostAndUsage', 'ce:GetDimensionValues'], 'Resource': '*'},
        {'Sid': 'OptionalEC2ResourceBilling', 'Effect': 'Allow',
         'Action': ['ce:GetCostAndUsageWithResources'], 'Resource': '*'},
        {'Sid': 'CurrentCatalogPrices', 'Effect': 'Allow', 'Action': ['pricing:GetProducts'], 'Resource': '*'},
    ]
    return {'Version': '2012-10-17', 'Statement': statements}


if __name__ == '__main__':
    import json
    print(json.dumps(example_policy(), indent=2))
