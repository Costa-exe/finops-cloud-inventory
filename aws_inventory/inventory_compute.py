"""Read-only compute/network inventory without inferred cost attribution.

Relationships retain their source. EKS inventory describes the AWS control
plane without querying Kubernetes or reading secrets, EC2 user data,
task overrides, or container environment variables.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .common import chunks, resource_name, tags_dict


def _fields(value, *keys):
    return {key: value[key] for key in keys if key in value}


def _ids(values):
    return sorted({value for value in values if value})


def _emit(ctx, service, kind, identifier, region, native_name=None, tags=None, **fields):
    normalized_tags = tags_dict(tags)
    name, source = resource_name(native_name, normalized_tags, identifier)
    return ctx.emit(service, kind, identifier, region, resource_name=name,
                    name_source=source, tags=normalized_tags, **fields)


def _ec2_arn(ctx, region, kind, identifier, owner_id=None):
    return f"arn:{ctx.partition}:ec2:{region}:{owner_id or ctx.account_id}:{kind}/{identifier}"


def _dependency_note(ctx, service):
    requested = getattr(ctx, "requested_services", ctx.selected_services)
    if service not in requested and "all" not in requested:
        return (f"{service.upper()} included as a dependency: complete regional inventory; "
                "cluster relationships are shown only when documented.")
    return ""


def _volumes(ctx, region):
    key = ("ebs_volumes", region)
    if key not in ctx.cache:
        previous_issues = len(ctx.issues)
        values = ctx.safe_pages("ec2", "describe_volumes", region, "Volumes")
        ctx.cache[key] = {value["VolumeId"]: value for value in values}
        ctx.cache[("ebs_volumes_complete", region)] = len(ctx.issues) == previous_issues
    return ctx.cache[key]


def _cluster_links(ctx, region):
    return ctx.cache.setdefault(("instance_cluster_links", region), {})


def _auto_scaling_groups(ctx, region, consumer):
    """Share one regional listing across EKS and EC2, retaining partial-read errors."""
    key = ("auto_scaling_groups", region)
    if key not in ctx.cache:
        previous_issues = len(ctx.issues)
        groups = ctx.safe_pages("autoscaling", "describe_auto_scaling_groups", region,
                                "AutoScalingGroups")
        ctx.cache[key] = {group["AutoScalingGroupName"]: group for group in groups}
        ctx.cache[("auto_scaling_groups_complete", region)] = len(ctx.issues) == previous_issues
        ctx.cache[("auto_scaling_groups_consumers", region)] = {consumer}
    else:
        consumers = ctx.cache[("auto_scaling_groups_consumers", region)]
        if consumer not in consumers and not ctx.cache[("auto_scaling_groups_complete", region)]:
            ctx.issue(consumer, region, "describe_auto_scaling_groups",
                      "Reused Auto Scaling inventory is incomplete because an earlier "
                      "DescribeAutoScalingGroups call failed. Available rows do not prove full coverage.",
                      code="IncompleteCachedInventory")
        consumers.add(consumer)
    return ctx.cache[key]


def _link_instance(ctx, region, instance_id, cluster, source, parent=""):
    if not instance_id:
        return
    links = _cluster_links(ctx, region).setdefault(instance_id, [])
    link = {"cluster_name": cluster, "source": source, "parent_id": parent}
    if link not in links:
        links.append(link)


def _tag_cluster_links(tags):
    links = []
    for key, value in tags.items():
        if key in ("eks:cluster-name", "aws:eks:cluster-name") and value:
            links.append({"cluster_name": value, "source": f"tag:{key}"})
        elif key.startswith("kubernetes.io/cluster/"):
            links.append({"cluster_name": key.split("/", 2)[-1], "source": f"tag:{key}"})
    return links


def collect_ec2(ctx, region):
    """Collect instances, machine capacity, EBS attachments, and Auto Scaling groups."""
    reservations = ctx.safe_pages("ec2", "describe_instances", region, "Reservations")
    instances = [item for reservation in reservations for item in reservation.get("Instances", [])]
    volumes = _volumes(ctx, region)
    types = {}
    for batch in chunks(_ids(item.get("InstanceType") for item in instances), 100):
        for item in ctx.safe_pages("ec2", "describe_instance_types", region,
                                   "InstanceTypes", InstanceTypes=batch):
            types[item["InstanceType"]] = item

    groups = _auto_scaling_groups(ctx, region, "ec2").values()
    instance_groups = {}
    for group in groups:
        name = group["AutoScalingGroupName"]
        members = group.get("Instances", [])
        tags = tags_dict(group.get("Tags"))
        inferred = _tag_cluster_links(tags)
        native_cluster_names = ctx.cache.get(("asg_cluster_names", region), {}).get(name, [])
        for member in members:
            instance_id = member.get("InstanceId")
            if instance_id:
                instance_groups[instance_id] = name
                for link in inferred:
                    _link_instance(ctx, region, instance_id, link["cluster_name"],
                                   f"autoscaling_membership+{link['source']}", name)
        _emit(ctx, "ec2", "auto_scaling_group", name, region, native_name=name,
              tags=tags, resource_arn=group.get("AutoScalingGroupARN"),
              availability_zone=group.get("AvailabilityZones"),
              subnet_ids=[part for part in group.get("VPCZoneIdentifier", "").split(",") if part],
              related_resource_ids=_ids(member.get("InstanceId") for member in members),
              relationship_source="autoscaling:DescribeAutoScalingGroups.Instances",
              cluster_name=_ids(native_cluster_names + [link["cluster_name"] for link in inferred]),
              created_at=group.get("CreatedTime"),
              details=_fields(group, "MinSize", "MaxSize", "DesiredCapacity", "DefaultCooldown",
                              "HealthCheckType", "HealthCheckGracePeriod", "Instances",
                              "LaunchTemplate", "LaunchConfigurationName", "MixedInstancesPolicy",
                              "TargetGroupARNs", "LoadBalancerNames", "CapacityRebalance",
                              "DesiredCapacityType", "SuspendedProcesses"),
              notes=_dependency_note(ctx, "ec2"))

    for instance in instances:
        identifier = instance["InstanceId"]
        tags = tags_dict(instance.get("Tags"))
        for link in _tag_cluster_links(tags):
            _link_instance(ctx, region, identifier, link["cluster_name"], link["source"])
        links = list(_cluster_links(ctx, region).get(identifier, []))
        instance_type = instance.get("InstanceType", "")
        specifications = types.get(instance_type, {})
        mappings = instance.get("BlockDeviceMappings", [])
        volume_ids = _ids(mapping.get("Ebs", {}).get("VolumeId") for mapping in mappings)
        disk_details = []
        for mapping in mappings:
            block = mapping.get("Ebs", {})
            volume = volumes.get(block.get("VolumeId"), {})
            disk_details.append({"device_name": mapping.get("DeviceName"),
                                 "volume_id": block.get("VolumeId"),
                                 "delete_on_termination": block.get("DeleteOnTermination"),
                                 "attachment_status": block.get("Status"),
                                 "attached_at": block.get("AttachTime"),
                                 "size_gib": volume.get("Size"), "type": volume.get("VolumeType"),
                                 "iops": volume.get("Iops"),
                                 "throughput_mib_s": volume.get("Throughput"),
                                 "encrypted": volume.get("Encrypted")})
        size_complete = all(volume_id in volumes for volume_id in volume_ids)
        interfaces = instance.get("NetworkInterfaces", [])
        private = [instance.get("PrivateIpAddress")]
        public = [instance.get("PublicIpAddress")]
        for interface in interfaces:
            for address in interface.get("PrivateIpAddresses", []):
                private.append(address.get("PrivateIpAddress"))
                public.append(address.get("Association", {}).get("PublicIp"))
            private.extend(address.get("Ipv6Address") for address in interface.get("Ipv6Addresses", []))
        cpu = instance.get("CpuOptions", {})
        configured_vcpu = (cpu["CoreCount"] * cpu["ThreadsPerCore"]
                           if "CoreCount" in cpu and "ThreadsPerCore" in cpu
                           else specifications.get("VCpuInfo", {}).get("DefaultVCpus"))
        _emit(ctx, "ec2", "instance", identifier, region, tags=tags,
              resource_arn=_ec2_arn(ctx, region, "instance", identifier),
              state=instance.get("State", {}).get("Name"),
              availability_zone=instance.get("Placement", {}).get("AvailabilityZone"),
              instance_type=instance_type, vpc_id=instance.get("VpcId"),
              subnet_ids=_ids([instance.get("SubnetId")]), private_ips=_ids(private),
              public_ips=_ids(public),
              security_group_ids=_ids(group.get("GroupId") for group in instance.get("SecurityGroups", [])),
              cluster_name=_ids(link["cluster_name"] for link in links),
              parent_id=instance_groups.get(identifier), related_resource_ids=volume_ids,
              relationship_source=_ids(["ec2:DescribeInstances.BlockDeviceMappings"] +
                                       [link["source"] for link in links]),
              storage_gib=sum(volumes[item].get("Size", 0) for item in volume_ids)
              if size_complete else None,
              vcpu=configured_vcpu, memory_mib=specifications.get("MemoryInfo", {}).get("SizeInMiB"),
              # LaunchTime changes after stop/start; device 0 attachment records initial launch.
              created_at=next((interface.get("Attachment", {}).get("AttachTime")
                               for interface in interfaces
                               if interface.get("Attachment", {}).get("DeviceIndex") == 0
                               and interface.get("NetworkCardIndex", 0) == 0), None),
              created_at_source="primary_network_interface_attachment",
              last_launched_at=instance.get("LaunchTime"),
              details={"image_id": instance.get("ImageId"),
                       "outpost_arn": instance.get("OutpostArn"),
                       "architecture": instance.get("Architecture"),
                       "platform_details": instance.get("PlatformDetails"),
                       "usage_operation": instance.get("UsageOperation"),
                       "tenancy": instance.get("Placement", {}).get("Tenancy", "default"),
                       "instance_lifecycle": instance.get("InstanceLifecycle", "on-demand"),
                       "product_codes": instance.get("ProductCodes", []),
                       "private_dns_name": instance.get("PrivateDnsName"),
                       "public_dns_name": instance.get("PublicDnsName"),
                       "root_device_name": instance.get("RootDeviceName"),
                       "root_device_type": instance.get("RootDeviceType"),
                       "ebs_optimized": instance.get("EbsOptimized"),
                       "iam_instance_profile": instance.get("IamInstanceProfile", {}).get("Arn"),
                       "network_interface_ids": _ids(item.get("NetworkInterfaceId") for item in interfaces),
                       "block_devices": disk_details, "ebs_size_complete": size_complete,
                       "instance_store": specifications.get("InstanceStorageInfo"),
                       "cpu_options": cpu, "cluster_links": links,
                       "auto_scaling_group": instance_groups.get(identifier)},
              notes=_dependency_note(ctx, "ec2"))


def collect_ebs(ctx, region):
    cached = ("ebs_volumes", region) in ctx.cache
    volumes = _volumes(ctx, region)
    if cached and not ctx.cache.get(("ebs_volumes_complete", region), False):
        ctx.issue("ebs", region, "describe_volumes",
                  "Reused EC2 volume inventory is incomplete because an earlier "
                  "DescribeVolumes call failed. Available rows do not prove full coverage.",
                  code="IncompleteCachedInventory")
    for identifier, volume in volumes.items():
        attachments = volume.get("Attachments", [])
        instance_ids = _ids(item.get("InstanceId") for item in attachments)
        links = [link for item in instance_ids for link in _cluster_links(ctx, region).get(item, [])]
        _emit(ctx, "ebs", "volume", identifier, region, tags=volume.get("Tags"),
              resource_arn=_ec2_arn(ctx, region, "volume", identifier),
              state=volume.get("State"), availability_zone=volume.get("AvailabilityZone"),
              storage_gib=volume.get("Size"), storage_type=volume.get("VolumeType"),
              iops=volume.get("Iops"), throughput_mib_s=volume.get("Throughput"),
              encrypted=volume.get("Encrypted"), attached_to=instance_ids,
              device_names=_ids(item.get("Device") for item in attachments),
              related_resource_ids=_ids(instance_ids + [volume.get("SnapshotId")]),
              relationship_source="ec2:DescribeVolumes.Attachments",
              cluster_name=_ids(link["cluster_name"] for link in links),
              created_at=volume.get("CreateTime"),
              details={"attachments": attachments, "snapshot_id": volume.get("SnapshotId"),
                       "kms_key_id": volume.get("KmsKeyId"),
                       "multi_attach_enabled": volume.get("MultiAttachEnabled"),
                       "fast_restored": volume.get("FastRestored"),
                       "outpost_arn": volume.get("OutpostArn"), "cluster_links": links},
              notes=_dependency_note(ctx, "ebs"))
    for snapshot in ctx.safe_pages("ec2", "describe_snapshots", region, "Snapshots", OwnerIds=["self"]):
        identifier = snapshot["SnapshotId"]
        _emit(ctx, "ebs", "snapshot", identifier, region, tags=snapshot.get("Tags"),
              resource_arn=_ec2_arn(ctx, region, "snapshot", identifier),
              state=snapshot.get("State"), storage_gib=snapshot.get("VolumeSize"),
              storage_type=snapshot.get("StorageTier"), encrypted=snapshot.get("Encrypted"),
              parent_id=snapshot.get("VolumeId"), created_at=snapshot.get("StartTime"),
              relationship_source="ec2:DescribeSnapshots.VolumeId",
              details=_fields(snapshot, "Description", "Progress", "KmsKeyId", "OwnerId",
                              "RestoreExpiryTime", "FullSnapshotSizeInBytes", "CompletionTime"),
              notes="storage_gib is the source volume size, not the incremental bytes billed. "
                    + _dependency_note(ctx, "ebs"))


def collect_eks(ctx, region):
    for name in ctx.safe_pages("eks", "list_clusters", region, "clusters"):
        response = ctx.safe_call("eks", "describe_cluster", region, name=name)
        if not response:
            continue
        cluster = response.get("cluster", {})
        networking = cluster.get("resourcesVpcConfig", {})
        _emit(ctx, "eks", "cluster", name, region, native_name=cluster.get("name"),
              tags=cluster.get("tags"), resource_arn=cluster.get("arn"),
              state=cluster.get("status"), engine="kubernetes", engine_version=cluster.get("version"),
              vpc_id=networking.get("vpcId"), subnet_ids=networking.get("subnetIds", []),
              security_group_ids=_ids(networking.get("securityGroupIds", []) +
                                     [networking.get("clusterSecurityGroupId")]),
              cluster_name=name, created_at=cluster.get("createdAt"),
              details={"version": cluster.get("version"),
                       "platform_version": cluster.get("platformVersion"),
                       "upgrade_policy": cluster.get("upgradePolicy"),
                       "compute_config": cluster.get("computeConfig"),
                       "storage_config": cluster.get("storageConfig"),
                       "kubernetes_network_config": cluster.get("kubernetesNetworkConfig"),
                       "resources_vpc_config": networking,
                       "endpoint": cluster.get("endpoint"), "role_arn": cluster.get("roleArn"),
                       "logging": cluster.get("logging"), "health": cluster.get("health"),
                       "encryption_config": cluster.get("encryptionConfig")},
              notes="AWS APIs exclude Kubernetes pods, deployments, PVCs/PVs, and actual capacity. "
                    "Self-managed/Auto Mode nodes without API or tag relationships remain unattributed.")
        for node_name in ctx.safe_pages("eks", "list_nodegroups", region, "nodegroups", clusterName=name):
            node_response = ctx.safe_call("eks", "describe_nodegroup", region,
                                          clusterName=name, nodegroupName=node_name)
            if not node_response:
                continue
            node = node_response.get("nodegroup", {})
            group_names = _ids(item.get("name") for item in node.get("resources", {}).get("autoScalingGroups", []))
            for group_name in group_names:
                names = ctx.cache.setdefault(("asg_cluster_names", region), {}).setdefault(group_name, [])
                if name not in names:
                    names.append(name)
            members = []
            if group_names:
                groups = _auto_scaling_groups(ctx, region, "eks")
                for group_name in group_names:
                    group = groups.get(group_name, {})
                    for instance in group.get("Instances", []):
                        instance_id = instance.get("InstanceId")
                        if instance_id:
                            members.append(instance_id)
                            _link_instance(ctx, region, instance_id, name,
                                           "eks:DescribeNodegroup.resources.autoScalingGroups+"
                                           "autoscaling:DescribeAutoScalingGroups.Instances", node_name)
            _emit(ctx, "eks", "nodegroup", f"{name}/{node_name}", region,
                  native_name=node.get("nodegroupName", node_name), tags=node.get("tags"),
                  resource_arn=node.get("nodegroupArn"), state=node.get("status"),
                  engine_version=node.get("version"), instance_type=node.get("instanceTypes", []),
                  subnet_ids=node.get("subnets", []), cluster_name=name, parent_id=name,
                  storage_gib=node.get("diskSize"), related_resource_ids=_ids(group_names + members),
                  relationship_source="eks:DescribeNodegroup.resources.autoScalingGroups",
                  created_at=node.get("createdAt"),
                  details={"auto_scaling_groups": group_names, "instance_ids": _ids(members),
                           "disk_size_semantics": "configured size per node; not total capacity",
                           "ami_type": node.get("amiType"), "capacity_type": node.get("capacityType"),
                           "scaling_config": node.get("scalingConfig"),
                           "launch_template": node.get("launchTemplate"),
                           "node_role": node.get("nodeRole"), "release_version": node.get("releaseVersion"),
                           "labels": node.get("labels"), "taints": node.get("taints"),
                           "health": node.get("health"), "update_config": node.get("updateConfig")})
        for profile_name in ctx.safe_pages("eks", "list_fargate_profiles", region,
                                          "fargateProfileNames", clusterName=name):
            profile_response = ctx.safe_call("eks", "describe_fargate_profile", region,
                                             clusterName=name, fargateProfileName=profile_name)
            if not profile_response:
                continue
            profile = profile_response.get("fargateProfile", {})
            _emit(ctx, "eks", "fargate_profile", f"{name}/{profile_name}", region,
                  native_name=profile.get("fargateProfileName", profile_name), tags=profile.get("tags"),
                  resource_arn=profile.get("fargateProfileArn"), state=profile.get("status"),
                  cluster_name=name, parent_id=name, subnet_ids=profile.get("subnets", []),
                  created_at=profile.get("createdAt"),
                  details=_fields(profile, "selectors", "podExecutionRoleArn", "health"))
        for addon_name in ctx.safe_pages("eks", "list_addons", region, "addons", clusterName=name):
            addon_response = ctx.safe_call("eks", "describe_addon", region,
                                           clusterName=name, addonName=addon_name)
            if not addon_response:
                continue
            addon = addon_response.get("addon", {})
            _emit(ctx, "eks", "addon", f"{name}/{addon_name}", region,
                  native_name=addon.get("addonName", addon_name), tags=addon.get("tags"),
                  resource_arn=addon.get("addonArn"), state=addon.get("status"),
                  engine_version=addon.get("addonVersion"), cluster_name=name, parent_id=name,
                  created_at=addon.get("createdAt"),
                  details=_fields(addon, "serviceAccountRoleArn", "health", "owner", "publisher",
                                  "marketplaceInformation", "podIdentityAssociations"))


def _ecs_failures(ctx, region, operation, response):
    for failure in response.get("failures", []):
        ctx.issue("ecs", region, operation, failure.get("detail", failure.get("reason", "")),
                  resource_id=failure.get("arn", ""), code=failure.get("reason", "Failure"))


def _cpu_units(value):
    try:
        return Decimal(str(value)) / Decimal(1024) if value is not None else None
    except (InvalidOperation, ValueError):
        return None


def collect_ecs(ctx, region):
    clusters = ctx.safe_pages("ecs", "list_clusters", region, "clusterArns")
    for batch in chunks(clusters, 100):
        response = ctx.safe_call("ecs", "describe_clusters", region, clusters=batch,
                                 include=["TAGS", "SETTINGS", "STATISTICS", "CONFIGURATIONS"])
        if not response:
            continue
        _ecs_failures(ctx, region, "describe_clusters", response)
        for cluster in response.get("clusters", []):
            arn = cluster["clusterArn"]
            name = cluster.get("clusterName", arn.rsplit("/", 1)[-1])
            _emit(ctx, "ecs", "cluster", arn, region, native_name=name, tags=cluster.get("tags"),
                  resource_arn=arn, cluster_name=name, state=cluster.get("status"),
                  details=_fields(cluster, "registeredContainerInstancesCount", "runningTasksCount",
                                  "pendingTasksCount", "activeServicesCount", "capacityProviders",
                                  "defaultCapacityProviderStrategy", "settings", "statistics",
                                  "configuration"))
            container_instances = []
            # Explicit statuses include instances that a default ACTIVE-only list hides.
            for status in ("ACTIVE", "DRAINING", "REGISTERING", "DEREGISTERING", "REGISTRATION_FAILED"):
                container_instances.extend(ctx.safe_pages("ecs", "list_container_instances", region,
                                                           "containerInstanceArns", cluster=arn,
                                                           status=status))
            instance_by_container = {}
            for members in chunks(_ids(container_instances), 100):
                described = ctx.safe_call("ecs", "describe_container_instances", region,
                                         cluster=arn, containerInstances=members, include=["TAGS"])
                if not described:
                    continue
                _ecs_failures(ctx, region, "describe_container_instances", described)
                for member in described.get("containerInstances", []):
                    member_arn = member["containerInstanceArn"]
                    instance_id = member.get("ec2InstanceId")
                    instance_by_container[member_arn] = instance_id
                    _link_instance(ctx, region, instance_id, name,
                                   "ecs:DescribeContainerInstances.ec2InstanceId", arn)
                    _emit(ctx, "ecs", "container_instance", member_arn, region,
                          tags=member.get("tags"), resource_arn=member_arn,
                          state=member.get("status"), cluster_name=name, parent_id=arn,
                          related_resource_ids=_ids([instance_id]),
                          relationship_source="ecs:DescribeContainerInstances.ec2InstanceId",
                          created_at=member.get("registeredAt"),
                          details=_fields(member, "ec2InstanceId", "agentConnected", "versionInfo",
                                          "registeredResources", "remainingResources", "runningTasksCount",
                                          "pendingTasksCount", "capacityProviderName", "healthStatus"))
            service_arns = ctx.safe_pages("ecs", "list_services", region, "serviceArns", cluster=arn)
            service_by_name = {}
            for services in chunks(service_arns, 10):
                described = ctx.safe_call("ecs", "describe_services", region, cluster=arn,
                                          services=services, include=["TAGS"])
                if not described:
                    continue
                _ecs_failures(ctx, region, "describe_services", described)
                for service in described.get("services", []):
                    service_arn = service["serviceArn"]
                    service_by_name[service.get("serviceName")] = service_arn
                    network = service.get("networkConfiguration", {}).get("awsvpcConfiguration", {})
                    _emit(ctx, "ecs", "service", service_arn, region,
                          native_name=service.get("serviceName"), tags=service.get("tags"),
                          resource_arn=service_arn, state=service.get("status"),
                          cluster_name=name, parent_id=arn, subnet_ids=network.get("subnets", []),
                          security_group_ids=network.get("securityGroups", []),
                          related_resource_ids=_ids(item.get("targetGroupArn") for item in service.get("loadBalancers", [])),
                          relationship_source="ecs:DescribeServices.loadBalancers",
                          created_at=service.get("createdAt"),
                          details=_fields(service, "desiredCount", "runningCount", "pendingCount",
                                          "launchType", "capacityProviderStrategy", "taskDefinition",
                                          "platformVersion", "schedulingStrategy", "loadBalancers",
                                          "deploymentController", "networkConfiguration",
                                          "serviceRegistries", "healthCheckGracePeriodSeconds"))
            task_arns = ctx.safe_pages("ecs", "list_tasks", region, "taskArns", cluster=arn,
                                       desiredStatus="RUNNING")
            # ECS RUNNING desired status includes tasks still transitioning to RUNNING.
            for tasks in chunks(task_arns, 100):
                described = ctx.safe_call("ecs", "describe_tasks", region, cluster=arn,
                                          tasks=tasks, include=["TAGS"])
                if not described:
                    continue
                _ecs_failures(ctx, region, "describe_tasks", described)
                for task in described.get("tasks", []):
                    task_arn = task["taskArn"]
                    containers = task.get("containers", [])
                    attachment_details = [{item.get("name"): item.get("value")
                                           for item in attachment.get("details", [])}
                                          for attachment in task.get("attachments", [])]
                    group = task.get("group", "")
                    parent = service_by_name.get(group.removeprefix("service:")) if group.startswith("service:") else None
                    network_interfaces = [interface for container in containers
                                          for interface in container.get("networkInterfaces", [])]
                    instance_id = instance_by_container.get(task.get("containerInstanceArn"))
                    runtime_platform = {}
                    if task.get("launchType") == "FARGATE" and task.get("taskDefinitionArn"):
                        definition = task["taskDefinitionArn"]
                        cache_key = ("ecs_task_platform", region, definition)
                        if cache_key not in ctx.cache:
                            response = ctx.safe_call("ecs", "describe_task_definition", region, taskDefinition=definition)
                            ctx.cache[cache_key] = (response or {}).get("taskDefinition", {}).get("runtimePlatform")
                        runtime_platform = ctx.cache[cache_key] or {}
                    _emit(ctx, "ecs", "task", task_arn, region, tags=task.get("tags"),
                          resource_arn=task_arn, state=task.get("lastStatus"),
                          cluster_name=name, parent_id=parent or arn,
                          availability_zone=task.get("availabilityZone"),
                          subnet_ids=_ids(item.get("subnetId") for item in attachment_details),
                          private_ips=_ids([item.get("privateIPv4Address") for item in attachment_details] +
                                           [item.get("privateIpv4Address") for item in network_interfaces] +
                                           [item.get("ipv6Address") for item in network_interfaces]),
                          related_resource_ids=_ids([instance_id, task.get("containerInstanceArn"),
                                                    task.get("taskDefinitionArn")] +
                                                   [item.get("networkInterfaceId") for item in attachment_details] +
                                                   [item.get("volumeId") for item in attachment_details]),
                          relationship_source="ecs:DescribeTasks",
                          vcpu=_cpu_units(task.get("cpu")), memory_mib=task.get("memory"),
                          storage_gib=task.get("ephemeralStorage", {}).get("sizeInGiB"),
                          created_at=task.get("createdAt"),
                          details={"launch_type": task.get("launchType"),
                                   "operating_system_family": runtime_platform.get("operatingSystemFamily"),
                                   "cpu_architecture": runtime_platform.get("cpuArchitecture"),
                                   "capacity_provider": task.get("capacityProviderName"),
                                   "platform_version": task.get("platformVersion"),
                                   "task_definition_arn": task.get("taskDefinitionArn"),
                                   "desired_status": task.get("desiredStatus"),
                                   "group": group, "health_status": task.get("healthStatus"),
                                   "ec2_instance_id": instance_id, "attachments": attachment_details,
                                   "containers": [_fields(item, "name", "containerArn", "image",
                                                           "imageDigest", "lastStatus", "healthStatus",
                                                           "cpu", "memory", "memoryReservation",
                                                           "networkInterfaces", "networkBindings")
                                                  for item in containers]},
                          notes="Tasks with desiredStatus RUNNING; storage_gib is configured ephemeral storage.")


def _elb_tags(ctx, region, sdk, identifiers, classic=False):
    """Fetch tags in supported batches and map responses by their resource ID."""
    parameter = "LoadBalancerNames" if classic else "ResourceArns"
    identifier_key = "LoadBalancerName" if classic else "ResourceArn"
    tags = {}
    for batch in chunks(identifiers, 20):
        response = ctx.safe_call(sdk, "describe_tags", region, **{parameter: batch})
        for description in (response or {}).get("TagDescriptions", []):
            identifier = description.get(identifier_key)
            if identifier:
                tags[identifier] = description.get("Tags", [])
    return tags


def collect_elb(ctx, region):
    balancers = ctx.safe_pages("elbv2", "describe_load_balancers", region, "LoadBalancers")
    tags = _elb_tags(ctx, region, "elbv2", [item["LoadBalancerArn"] for item in balancers])
    for balancer in balancers:
        arn = balancer["LoadBalancerArn"]
        listeners = ctx.safe_pages("elbv2", "describe_listeners", region, "Listeners", LoadBalancerArn=arn)
        # Listener action configuration may include authentication client secrets.
        listener_metadata = [_fields(item, "ListenerArn", "Port", "Protocol", "SslPolicy", "Certificates")
                             for item in listeners]
        zones = balancer.get("AvailabilityZones", [])
        _emit(ctx, "elb", "load_balancer", arn, region, native_name=balancer.get("LoadBalancerName"),
              tags=tags.get(arn, []), resource_arn=arn,
              state=balancer.get("State", {}).get("Code"), vpc_id=balancer.get("VpcId"),
              availability_zone=_ids(item.get("ZoneName") for item in zones),
              subnet_ids=_ids(item.get("SubnetId") for item in zones),
              security_group_ids=balancer.get("SecurityGroups", []),
              created_at=balancer.get("CreatedTime"),
              details={"type": balancer.get("Type"), "scheme": balancer.get("Scheme"),
                       "dns_name": balancer.get("DNSName"), "ip_address_type": balancer.get("IpAddressType"),
                       "availability_zones": zones, "listeners": listener_metadata})
    groups = ctx.safe_pages("elbv2", "describe_target_groups", region, "TargetGroups")
    tags = _elb_tags(ctx, region, "elbv2", [item["TargetGroupArn"] for item in groups])
    for group in groups:
        arn = group["TargetGroupArn"]
        health = ctx.safe_call("elbv2", "describe_target_health", region, TargetGroupArn=arn)
        targets = (health or {}).get("TargetHealthDescriptions", [])
        _emit(ctx, "elb", "target_group", arn, region, native_name=group.get("TargetGroupName"),
              tags=tags.get(arn, []), resource_arn=arn,
              vpc_id=group.get("VpcId"), attached_to=group.get("LoadBalancerArns", []),
              related_resource_ids=_ids(item.get("Target", {}).get("Id") for item in targets),
              relationship_source="elasticloadbalancing:DescribeTargetHealth.Target",
              details={"target_type": group.get("TargetType"), "protocol": group.get("Protocol"),
                       "port": group.get("Port"), "protocol_version": group.get("ProtocolVersion"),
                       "health_check_protocol": group.get("HealthCheckProtocol"),
                       "health_check_port": group.get("HealthCheckPort"),
                       "health_check_path": group.get("HealthCheckPath"),
                       "target_health": targets, "target_health_available": health is not None})
    balancers = ctx.safe_pages("elb", "describe_load_balancers", region, "LoadBalancerDescriptions")
    tags = _elb_tags(ctx, region, "elb", [item["LoadBalancerName"] for item in balancers], classic=True)
    for balancer in balancers:
        name = balancer["LoadBalancerName"]
        _emit(ctx, "elb", "classic_load_balancer", name, region, native_name=name,
              tags=tags.get(name, []),
              resource_arn=f"arn:{ctx.partition}:elasticloadbalancing:{region}:{ctx.account_id}:loadbalancer/{name}",
              vpc_id=balancer.get("VPCId"), availability_zone=balancer.get("AvailabilityZones", []),
              subnet_ids=balancer.get("Subnets", []), security_group_ids=balancer.get("SecurityGroups", []),
              related_resource_ids=_ids(item.get("InstanceId") for item in balancer.get("Instances", [])),
              relationship_source="elasticloadbalancing:DescribeLoadBalancers.Instances",
              created_at=balancer.get("CreatedTime"),
              details=_fields(balancer, "DNSName", "Scheme", "ListenerDescriptions", "HealthCheck"))


def collect_vpc(ctx, region):
    specifications = (
        ("describe_vpcs", "Vpcs", "vpc", "VpcId", ("CidrBlock", "IsDefault", "InstanceTenancy", "CidrBlockAssociationSet", "Ipv6CidrBlockAssociationSet")),
        ("describe_subnets", "Subnets", "subnet", "SubnetId", ("CidrBlock", "AvailableIpAddressCount", "MapPublicIpOnLaunch", "DefaultForAz", "Ipv6CidrBlockAssociationSet")),
        ("describe_security_groups", "SecurityGroups", "security_group", "GroupId", ("GroupName", "Description", "IpPermissions", "IpPermissionsEgress", "OwnerId")),
        ("describe_network_interfaces", "NetworkInterfaces", "network_interface", "NetworkInterfaceId", ("Description", "InterfaceType", "RequesterManaged", "RequesterId", "Attachment", "Association", "PrivateIpAddresses", "Ipv6Addresses")),
        ("describe_nat_gateways", "NatGateways", "nat_gateway", "NatGatewayId", ("ConnectivityType", "AvailabilityMode", "AutoProvisionZones", "NatGatewayAddresses", "DeleteTime", "FailureCode", "FailureMessage")),
        ("describe_route_tables", "RouteTables", "route_table", "RouteTableId", ("Routes", "Associations", "PropagatingVgws")),
        ("describe_internet_gateways", "InternetGateways", "internet_gateway", "InternetGatewayId", ("Attachments",)),
        ("describe_vpc_endpoints", "VpcEndpoints", "vpc_endpoint", "VpcEndpointId", ("VpcEndpointType", "ServiceName", "NetworkInterfaceIds", "RouteTableIds", "PrivateDnsEnabled", "DnsEntries", "IpAddressType")),
        ("describe_addresses", "Addresses", "elastic_ip", "AllocationId", ("Domain", "AssociationId", "NetworkInterfaceId", "InstanceId", "PublicIpv4Pool", "NetworkBorderGroup")),
        ("describe_network_acls", "NetworkAcls", "network_acl", "NetworkAclId", ("IsDefault", "Entries", "Associations")),
        ("describe_vpc_peering_connections", "VpcPeeringConnections", "vpc_peering_connection", "VpcPeeringConnectionId", ("RequesterVpcInfo", "AccepterVpcInfo", "Status", "ExpirationTime")),
        ("describe_transit_gateways", "TransitGateways", "transit_gateway", "TransitGatewayId", ("Description", "OwnerId", "Options")),
        ("describe_transit_gateway_attachments", "TransitGatewayAttachments", "transit_gateway_attachment", "TransitGatewayAttachmentId", ("TransitGatewayId", "ResourceType", "ResourceId", "ResourceOwnerId", "Association")),
        ("describe_vpn_connections", "VpnConnections", "vpn_connection", "VpnConnectionId", ("Type", "CustomerGatewayId", "VpnGatewayId", "TransitGatewayId", "Category")),
    )
    for operation, result_key, kind, id_key, detail_keys in specifications:
        for value in ctx.safe_pages("ec2", operation, region, result_key):
            identifier = value.get(id_key) or value.get("PublicIp")
            if not identifier:
                continue
            attachment = value.get("Attachment", {})
            related = [attachment.get("InstanceId"), value.get("InstanceId"),
                       value.get("NetworkInterfaceId") if kind != "network_interface" else None,
                       value.get("ResourceId"), value.get("TransitGatewayId"),
                       value.get("CustomerGatewayId"), value.get("VpnGatewayId")]
            related += [item.get("VpcId") for item in value.get("Attachments", [])]
            related += value.get("NetworkInterfaceIds", [])
            related += value.get("RouteTableIds", [])
            private = [value.get("PrivateIpAddress")]
            private += [item.get("PrivateIpAddress") for item in value.get("PrivateIpAddresses", [])]
            private += [item.get("Ipv6Address") for item in value.get("Ipv6Addresses", [])]
            public = [value.get("PublicIp"), value.get("Association", {}).get("PublicIp")]
            public += [item.get("PublicIp") for item in value.get("NatGatewayAddresses", [])]
            private += [item.get("PrivateIp") for item in value.get("NatGatewayAddresses", [])]
            tags = value.get("Tags", value.get("TagSet"))
            _emit(ctx, "vpc", kind, identifier, region,
                  native_name=value.get("GroupName") if kind == "security_group" else None,
                  tags=tags, resource_arn=value.get("TransitGatewayArn") or
                  _ec2_arn(ctx, region, "natgateway" if kind == "nat_gateway"
                           else kind.replace("_", "-"), identifier, value.get("OwnerId")),
                  state=value.get("State", value.get("Status", "")),
                  availability_zone=value.get("AvailabilityZone"),
                  vpc_id=value.get("VpcId"), subnet_ids=_ids(value.get("SubnetIds", []) + [value.get("SubnetId")]),
                  private_ips=_ids(private), public_ips=_ids(public),
                  security_group_ids=_ids(item.get("GroupId") for item in value.get("Groups", [])),
                  parent_id=value.get("VpcId") if kind != "vpc" else None,
                  attached_to=_ids([attachment.get("InstanceId"), value.get("InstanceId")]),
                  related_resource_ids=_ids(related), relationship_source=f"ec2:{operation}",
                  created_at=value.get("CreateTime", value.get("CreationTimestamp")),
                  details=_fields(value, *detail_keys))


COLLECTORS = {
    "eks": collect_eks,
    "ecs": collect_ecs,
    "ec2": collect_ec2,
    "ebs": collect_ebs,
    "elb": collect_elb,
    "vpc": collect_vpc,
}
