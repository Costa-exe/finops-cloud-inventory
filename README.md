# finops-cloud-inventory

A Python command-line tool for inspecting AWS resources, their relationships and monthly costs. It combines AWS SDK inventory, Cost Explorer billing and current Price List rates in one HTML report, with downloadable Excel workbooks per service.

The tool works with the AWS credentials available in your terminal or a selected profile. It reads AWS resources without modifying them. Resource names and relationships use native API metadata where available, with resource IDs as a fallback when descriptive names are missing.

## Requirements

- Python 3.11 or later on Windows, Linux or macOS.
- Dependencies from `requirements.txt`.
- AWS credentials with access to the selected resources, billing and pricing APIs.
- Network access to AWS during collection; the HTML can then be opened offline.

AWS CLI is not required to run the tool. It can be used separately to authenticate an existing SSO profile.

Keep `costcsv.py`, `requirements.txt` and the complete `aws_inventory/` directory together. The package includes Python, JavaScript and CSS files required to build the report.

## Installation

Run the following commands from the project directory.

### Linux and macOS

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python costcsv.py --interactive
```

### Windows

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe costcsv.py --interactive
```

If `py` is unavailable, use `python` to create the environment. No Bash or PowerShell wrapper is needed.

### Using a virtual environment

A virtual environment is recommended, not mandatory. It isolates the project's dependencies from system Python and other applications. On systems that report `externally-managed-environment`, install dependencies in a virtual environment instead of changing system Python.

The commands above invoke the environment's interpreter directly, so activation is optional. If you prefer activation:

```bash
# Linux/macOS
source .venv/bin/activate
```

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

After activation, use `python costcsv.py`. Leave the environment with `deactivate`. If PowerShell blocks activation, use the direct interpreter commands above.

The virtual environment does not isolate AWS authentication: Python still inherits environment credentials and can read configured AWS profiles.

## AWS authentication

Without `--profile`, the tool uses boto3's standard credential chain, including environment credentials, configured profiles and workload roles.

To select an existing profile:

```bash
python costcsv.py --interactive --profile company
```

`--profile` selects credentials; it does not perform a login. For an existing AWS CLI SSO profile, authenticate first:

```bash
aws sso login --profile company
python costcsv.py --interactive --profile company
```

Each run uses the authenticated account. To inspect multiple accounts, run separately with the appropriate profiles or roles. The tool does not switch accounts automatically.

## Usage

The following examples use `python` from an activated environment. Alternatively, use the direct interpreter path shown in Installation.

### Guided configuration

```bash
python costcsv.py
```

The tool asks for regions, services, the billing period, output language, date display format, timezone and assumed instance runtime. Output languages are English (`en`), Italian (`it`), French (`fr`) and Spanish (`es`), with English as the default. The selection applies to the HTML interface, calculation explanations and embedded Excel workbooks; resource names, AWS identifiers and original API messages remain unchanged. CLI prompts remain in English. Press Enter to accept a proposed value. Billing prompts suggest the previous complete calendar month.

You can supply some choices and prompt for the rest:

```bash
python costcsv.py --interactive --profile company --region eu-west-1
```

### All supported services

Replace the dates with your required period. This example includes all of September:

```bash
python costcsv.py --from 2026-09-01 --to 2026-10-01 --region all --service all --language it --date-format dmy --timezone Europe/Rome
```

### Selected regions and services

```bash
python costcsv.py --from 2026-09-01 --to 2026-10-01 --region eu-west-1 eu-central-1 --service ec2,eks,rds
```

Region and service lists accept spaces or commas. Use `all` on its own. `--region all` includes global services; when selecting individual regions, add `global` if needed, for example `--region eu-west-1 global`. S3 buckets are filtered by their actual region.

### Inventory and estimates without billing queries

```bash
python costcsv.py --mode inventory --region all --service all
```

This mode uses the current inventory and catalog prices for a current-calendar-month scenario without querying Cost Explorer. Do not pass cost dates, `--filter-file` or `--resource-costs` in inventory mode.

### Help

```bash
python costcsv.py --help
python costcsv.py --list-services
```

Both commands exit without contacting AWS. The optional positional command `export` is equivalent to omitting it. With no arguments, or just `export`, configuration is interactive. With other arguments, use `--interactive` explicitly if you want prompts.

## Report location

HTML reports always use this path inside the terminal's current working directory:

```text
output/<account_id>/YYYY-MM-DD_<service>_HHMM.html
```

The destination and filename cannot be customized. `--output` is rejected for HTML. Account directories are created automatically, and the absolute path is printed when the report is written.

- `<account_id>` comes from the authenticated AWS identity.
- The date and time are the generation timestamp in `--timezone`, default `UTC`.
- `<service>` is the selected service identifier, `all` for all services, or `multi` for a selection of multiple services.
- Filenames use ISO dates and a 24-hour clock without colons, independently of the display date format.

Two runs with the same account, service label and generation minute have the same destination. An existing report is preserved unless `--overwrite` is supplied. The `output/` directory is excluded from Git.

Excel downloads follow the browser's download directory or save dialog.

## Reading the report

Open the HTML in a browser. It needs no server or external assets. The header switches between four views:

| View | Contents |
| --- | --- |
| Overview | Monthly totals and charts by service and region. |
| Resources and associated costs | One selected resource, its own cost, associated costs and relationships. |
| AWS service spending | Monthly Cost Explorer billing, separate from resource estimates. |
| Exports | One Excel workbook per service. |

Select a service from the side menu and use the month and region filters to narrow the view. Resource search matches names, IDs, instance types and clusters. A compact switch at the top right changes the light/dark theme.

Relationships display resource names where available and identify the resource type. References such as security groups do not automatically add costs. Cost-bearing dependencies are explicitly marked as included in the subtotal.

The **Calculation paradigm** section explains the cost source, covered billing intervals, estimated components and calculation assumptions for the selected resource. An estimated component is already included in the resource's own cost; it is not an additional charge.

### Excel workbooks

Each service workbook contains four sheets:

| Sheet | Contents |
| --- | --- |
| Service spending | Monthly service billing from Cost Explorer. |
| By type | Own costs grouped by instance or resource type. |
| By resource | Names, regions, catalog rates and own monthly costs. |
| Associated costs | Resource-plus-dependency subtotals and a short note identifying included services. |

Sheets have monthly columns, a period total, a region field, frozen headings and no dropdown filters. Below the title, only the account ID is shown. Workbooks contain all collected months and regions, independently of the current HTML filters.

Blank costs mean unavailable, not zero. The associated-cost total counts shared components once. The sheets represent different views of costs and must not be added together.

## Cost sources and calculation

**AWS Cost Explorer** supplies billed amounts for the selected interval. **AWS Price List Query API** supplies current public On-Demand catalog rates for supported configurations. Rates are queried from AWS, not hard-coded.

The inventory is a snapshot of resources present when the command runs. Estimates apply that configuration and current catalog prices to the selected period. They do not reconstruct historical resource lifetimes, scaling or actual runtime.

| Component | Calculation |
| --- | --- |
| Supported instance compute | Matching hourly rate × selected calendar hours × assumed runtime hours/day ÷ 24. |
| EBS storage | Provisioned capacity × matching monthly rate, prorated for partial months; supported excess IOPS/throughput are separate components. |
| EKS control plane | Matching rate for the selected support-tier assumption × calendar hours. Nodes and disks have their own costs. |
| Associated resources | Own cost plus known costs of explicitly included dependencies. |
| Resource-level EC2 billing | Available resource billing replaces estimates for the same intervals; estimates cover only uncovered hours. |

`--estimate-hours-per-day` defaults to 24. Reducing instance runtime does not reduce continuously provisioned storage or every service's hourly charges. A resource currently stopped is assumed stopped throughout the scenario; attached storage remains chargeable.

Use `--resource-costs` to request EC2 billing attributed to resource IDs. The script requires a period within the last 14 days and a selection containing `ec2`, `eks`, `ecs` or `all`; resource-level data must be available in Cost Explorer. Historical service totals are never arbitrarily divided among instances.

Missing rates, ambiguous configurations and unsupported usage quantities remain unavailable. Some services have inventory and service-level billing without a resource-level estimate. RDS estimates cover compute only; storage, backups and I/O are excluded. Spot instances may have an On-Demand comparison, not a Spot price. Discounts, Savings Plans and Reserved Instances are not inferred from catalog prices.

Resource subtotals may overlap: an EKS cluster can include EC2 workers and their disks. Overview resource totals count each own cost once. **Do not add Cost Explorer service totals to the resource estimates**, or sum overlapping dependency subtotals.

Amounts displayed in HTML are USD only. Other source currencies are excluded with a notice; there is no currency conversion option.

## Command-line parameters

Flags without a value are disabled unless supplied, except catalog pricing, which is enabled by default.

### Main options

| Parameter | Accepted values | Default / behavior |
| --- | --- | --- |
| `export` | Optional positional command | Can be omitted. |
| `-h`, `--help` | Flag, no value | Print help and exit. |
| `--interactive` | Flag, no value | Ask configuration questions one at a time. |
| `--profile` | Existing boto3 profile name, e.g. `company` | Standard boto3 credential chain when omitted. |
| `--region`, `--regions` | Region codes separated by spaces or commas; `global`; or `all` | `all`. Use `all` alone. Explicit regional selections can include `global`. |
| `--service`, `--services` | Service identifiers below, separated by spaces or commas; or `all` | `all`. Use `all` alone. |
| `--from` | `YYYY-MM-DD` | Inclusive start in UTC; required for `combined` and `costs` unless prompted. |
| `--to` | `YYYY-MM-DD` | Exclusive end in UTC; required for `combined` and `costs` unless prompted. |
| `--overwrite` | Flag, no value | Allow replacement of an existing generated report. Off by default. |
| `--language` | `en`, `it`, `fr`, `es` | `en`; HTML and embedded Excel language. |
| `--date-format` | `iso`, `dmy`, `mdy` | `iso`: `YYYY-MM-DD`; `dmy`: `DD/MM/YYYY`; `mdy`: `MM/DD/YYYY`. Input dates remain ISO. |
| `--timezone` | Valid IANA name, e.g. `UTC`, `Europe/Rome`, `America/New_York` | `UTC`; changes displayed timestamps, not UTC billing boundaries. |
| `--estimate-hours-per-day` | Number from `0` to `24`, including decimals such as `8.5` | `24`; instance runtime assumption, not a blanket reduction of storage or other continuously provisioned charges. |
| `--eks-support` | `standard`, `extended` | `standard`; explicit support-tier assumption for EKS estimates. |
| `--prices`, `--no-prices` | Flags, no value | Price queries enabled by default; `--no-prices` disables catalog queries. |
| `--workers` | Integer from `1` to `32` | `4`; concurrent inventory regions. `1` runs sequentially. |
| `--strict` | Flag, no value | Do not publish a report if any read fails. Off by default. |
| `--list-services` | Flag, no value | List supported service identifiers and exit without AWS access. |

### Advanced and legacy options

| Parameter | Accepted values | Default / behavior |
| --- | --- | --- |
| `--mode` | `combined`, `inventory`, `costs` | `combined`: inventory and billing; `inventory`: inventory without Cost Explorer; `costs`: billing without resource inventory. |
| `--metric` | `AmortizedCost`, `UnblendedCost`, `BlendedCost`, `NetAmortizedCost`, `NetUnblendedCost` | `AmortizedCost`; spelling is case-sensitive. |
| `--resource-costs` | Flag, no value | Request EC2 resource-level billing. Off by default; select `ec2`, `eks`, `ecs` or `all`. The script restricts the period to the last 14 days; AWS resource-level data must be available. |
| `--filter-file` | Path to a JSON Cost Explorer expression | Optional; applies to billing only, including in `combined` mode. |
| `--billing-view-arn` | Billing view ARN | Optional; billing remains restricted to the authenticated account. |
| `--ce-region` | Cost Explorer endpoint region | `us-east-1`; does not select inventory regions. |
| `--pricing-region` | Price List Query API endpoint region | `us-east-1`; does not select inventory regions. |
| `--output` | Legacy formats only: directory for `bundle`, file for `raw-csv` | Not accepted for HTML. Legacy defaults: `aws_reports` and `aws_inventory.csv`. |
| `--format` | `html`, `bundle`, `raw-csv` | `html`. Legacy `bundle` produces workbooks and companion CSVs; `raw-csv` produces one technical CSV. |
| `--granularity` | `MONTHLY`, `DAILY`, `HOURLY` | `MONTHLY`; case-insensitive. HTML and bundle require `MONTHLY`. The script restricts `HOURLY` to the last 14 days; AWS hourly data must be available. |
| `--group-by` | One or two distinct dimensions listed below, separated by spaces or commas | `SERVICE REGION`; HTML and bundle require exactly this pair in this order. Custom grouping requires `raw-csv`. |
| `--excel` | Flag, no value | Legacy CSV formatting: UTF-8 BOM, semicolons and decimal commas. It does not enable HTML workbook downloads, which are already included. |

Supported `--group-by` dimensions (case-insensitive):

```text
SERVICE REGION AZ INSTANCE_TYPE LINKED_ACCOUNT USAGE_TYPE OPERATION
PURCHASE_TYPE RECORD_TYPE PLATFORM TENANCY LEGAL_ENTITY_NAME INVOICING_ENTITY
```

Dates must satisfy `--from < --to`. The start is inclusive and the end exclusive, both in UTC. The period cannot extend beyond the current UTC day; tomorrow's date is permitted as the exclusive end. The selected timezone changes timestamp display and the filename timestamp, not billing boundaries.

Non-default language, date-display and timezone options require HTML. HTML and workbook bundles require monthly granularity and `SERVICE REGION` grouping. Endpoint overrides select an API endpoint, not the regions whose resources are collected.

`--filter-file` accepts a non-empty Cost Explorer JSON expression, for example:

```json
{"Dimensions":{"Key":"RECORD_TYPE","Values":["Usage"]}}
```

Filters apply only to billing and combine with account, region and service filters. `--billing-view-arn` retains the authenticated-account restriction.

## Supported services

`--service all` selects all implemented inventory collectors and all services returned by Cost Explorer. It does not imply inventory coverage of every AWS product or feature.

| Selector | Main information |
| --- | --- |
| `ec2` | Instances, type, state, CPU/RAM, networking, IPs, security groups, and disk relationships. |
| `ebs` | Volumes, provisioned GiB, type, IOPS, throughput, encryption, attachments, and owned snapshots. |
| `eks` | Clusters, node groups, Fargate profiles, add-ons, and Auto Scaling/instance relationships exposed by AWS APIs. |
| `ecs` | Clusters, services, tasks, and container instances with available relationships. |
| `elb` | Classic load balancers, ALB/NLB, target groups, and targets. |
| `vpc` | VPCs, subnets, NAT gateways, endpoints, Elastic IPs, and implemented network resources. |
| `rds` | DB instances, clusters, snapshots, engine, storage, and networking. |
| `s3` | General-purpose buckets, region, and configuration; no sizes or object scans. |
| `lambda` | Functions, runtime, memory, temporary storage, and network configuration. |
| `dynamodb` | Tables, indexes, capacity, and service-reported size. |
| `efs` | File systems, mount targets, reported storage, and networking. |
| `elasticache` | Clusters, replication groups, nodes, and serverless caches. |
| `opensearch` | Provisioned domains, node configuration, EBS, and networking. |
| `redshift` | Provisioned clusters, nodes, storage, and networking. |
| `sqs`, `sns` | Queues and topics with descriptive attributes. |
| `cloudwatch` | Log groups and reported storage. |
| `cloudfront`, `route53` | Global distributions and hosted zones. |
| `fsx`, `backup` | File systems/volumes, backup vaults/recovery points and native links. |
| `apigateway` | REST APIs/stages and HTTP/WebSocket APIs/integrations; ARN targets only. |
| `msk`, `kinesis` | Kafka clusters and broker configuration; Data Streams and shard counts. |
| `emr`, `glue`, `athena` | Active EMR clusters/EC2 IDs; Glue job configuration; Athena workgroups. |
| `sagemaker`, `bedrock` | SageMaker endpoints/production variants; Bedrock provisioned throughput and application inference profiles. |
| `eventbridge`, `stepfunctions` | Event buses/rules/target ARNs; state machine metadata. |
| `ecr` | Repositories and image metadata; image sizes are not summed as billed storage. |
| `kms`, `secretsmanager` | Key and secret metadata; secret values and cryptographic material are never read. |
| `waf` | Regional/CloudFront Web ACLs; regional ALB associations. |

Selecting EC2 includes EBS inventory. EKS, ECS and EMR also include EC2 and EBS as regional dependencies; this can collect instances outside the selected cluster. These dependencies do not change the service label in the HTML filename. Billing queries retain the requested service selection.

EC2 billing includes Compute and EC2 Other. EBS selects identifiable EBS usage within EC2 Other; VPC includes identifiable supported network usage. EC2 Other is a shared billing category, so its total is not an exact allocation to EBS or VPC.

Native names are used when available, followed by the `Name` tag or resource ID as appropriate. An EC2 instance without a descriptive name cannot have one reconstructed. Relationships use IDs, ARNs and metadata returned by AWS.

EBS capacity is provisioned capacity, not guest filesystem usage. The tool does not inspect VM files, S3 object contents, database records, queue messages, Lambda environment variables or secret values. Kubernetes workload internals and resource variants outside the implemented collectors are not included.

## Required AWS access

Permissions depend on the selected services and options:

| Purpose | Access |
| --- | --- |
| Account identity and region discovery | STS caller identity; `ec2:DescribeRegions` for region discovery. |
| Service billing | `ce:GetCostAndUsage`; `ce:GetDimensionValues` when filter resolution requires it. |
| Optional EC2 resource billing | `ce:GetCostAndUsageWithResources`. |
| Current catalog prices | `pricing:GetProducts`; omitted with `--no-prices`. |
| Resource inventory | Service-specific `Describe*`, `List*` and selected `Get*` operations used by the collectors. |

Resource-read access alone does not guarantee billing access. Cost Explorer must be enabled and its data available. Cost Explorer API requests may incur charges. Selecting every service and region increases the number of requests.

The default billing and pricing endpoints target the commercial AWS partition. GovCloud and China deployments require compatible endpoint and pricing availability; cross-partition price equivalence is not assumed.

## Errors and troubleshooting

| Situation | Action |
| --- | --- |
| `externally-managed-environment` during installation | Install dependencies in a virtual environment. |
| Missing or expired credentials | Authenticate the selected profile or refresh terminal credentials. |
| Access denied | Check the reported AWS operation and the identity's permissions. |
| Unknown timezone | Use an IANA name such as `Europe/Rome` and install dependencies, including `tzdata`. |
| Existing HTML destination | Use `--overwrite` if replacement is intended. |
| `--output` rejected | Omit it for HTML; the account directory and filename are fixed. |
| Unavailable resource cost | Inspect the calculation details; a supported rate or usage quantity may be missing. |
| Request throttling | Reduce `--workers`; `1` processes regions sequentially. |

The report is marked partial when reads fail. If Cost Explorer is unavailable, HTML can still contain inventory and catalog estimates. `--strict` prevents publication on any read error. Incomplete costs are not treated as zero.

| Exit code | Meaning |
| --- | --- |
| `0` | Collection completed. |
| `1` | Failure; no new report published. |
| `2` | Partial report published. |
| `130` | Execution interrupted. |

Reports contain infrastructure names, identifiers and relationships. Handle them as account information and keep them out of public repositories. Use a local filesystem supporting hard links for publication of a new HTML report.
