# Cost of running the tool

Running the program locally requires no payment to this project. AWS can charge for API calls even when they only read data. The cost of generating a report is separate from the infrastructure spending displayed inside it.

For example, a report showing USD 5,000 of infrastructure spending does not cost USD 5,000 to generate. Its execution cost depends on the billable requests and any separately enabled AWS features.

This guide describes the default HTML report in `combined` mode. Pricing references were checked on October 12, 2026; consult the linked providers for current terms. Numerical scenarios below are calculations under stated assumptions, not measurements from a real account or spending caps.

## Two ways to build the same report

Both modes produce one HTML report, with inventory, relationships, monthly figures and downloadable Excel workbooks. Both use the primary billing view unless `--billing-view-arn` selects a different view. Adding resource detail does not create another billing view or another HTML report.

| Behavior | Without `--resource-costs` (default) | With `--resource-costs` |
| --- | --- | --- |
| Service and region spending | Cost Explorer totals | Same Cost Explorer totals |
| Individual EC2 compute cost | Estimate from supported catalog prices and runtime assumptions | Cost Explorer amounts attributed to the EC2 ID for returned intervals; estimates can fill uncovered intervals |
| Inventory and relationships | SDK metadata, names, disks and other supported associations | Same inventory and relationships |
| Attached resources | Supported components calculated separately and included in parent subtotals where applicable | Same; EC2 compute billing does not include EBS or every networking charge |
| Extra billing query | None for EC2 resource detail | `GetCostAndUsageWithResources`, including any additional pages |
| Date selection | Normal Cost Explorer availability limits | The script requires the entire selected period to be within the last 14 days |

### Default: billed service totals and estimated resource detail

The program retrieves service/region totals with `GetCostAndUsage`. It inventories resources through SDK metadata APIs, queries current catalog rates for supported configurations and calculates estimates locally. It does not divide the billed service total among instances to force the numbers to match.

In particular, the estimate describes current capacity modeled over the selected period, bounded by available creation dates. It does not estimate costs before known creation; creation partway through a month prorates the estimate. EC2 uses the primary network interface attachment for initial launch, with the last launch as a conservative fallback when creation is unknown. Missing dates are explicitly identified as an unverified historical scenario. It cannot reconstruct every past resize, deletion, attachment, runtime hour or usage-based charge. Discounts and commitment allocation can also make billing differ from list-price estimates. Unsupported or missing prices remain unavailable rather than becoming a zero charge; a resource known not to exist yet can have a genuine zero for that interval.

For a historical monthly report with resource detail estimated from current inventory, this is the intended mode. `--resource-costs` is off unless explicitly supplied.

Historical estimates use the configured runtime assumption even if an instance is stopped today. Associated costs are bounded by parent creation and known current disk attachment dates; they are not automatically copied as a full-month charge. If a returned resource billing interval crosses such a boundary, the tool keeps that billing on the resource but does not invent an allocation to the parent. These calculations use already collected metadata and do not add AWS API calls.

Example command, using a completed month of your choice:

```bash
python costcsv.py --regions eu-west-1 eu-central-1 --services all --from 2026-09-01 --to 2026-10-01
```

The start is inclusive and the end exclusive. Replace the dates as appropriate; input dates are always in UTC and ISO format.

### Optional: add AWS-attributed EC2 compute spending

`--resource-costs` adds one logical query grouped by EC2 resource ID and region. The implementation requests **daily** data and presents the results within the report's monthly windows. It does not query each EC2 separately, enable Cost Explorer settings or enable hourly data collection.

AWS requires resource-level access to be opted in and the requested data to be available. The API's date range is restricted to the last 14 days. The script also requires a service selection containing `ec2`, `eks`, `ecs` or `all`. See [GetCostAndUsageWithResources](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostAndUsageWithResources.html).

To compare modes, run the same command over a recent valid interval, adding `--resource-costs` for the second run. A three-month command cannot simply be reused with this flag: the script rejects it. If both runs produce the same timestamp-based filename, use `--overwrite` only if you intend to replace the first report.

Suppose `worker-01` has these fictional values for the **same selected period**:

| Component | Default report | Report with resource billing available |
| --- | ---: | ---: |
| EC2 compute | USD 30 estimated | USD 18 returned by Cost Explorer |
| Attached EBS volume | USD 4 estimated | USD 4 estimated |
| EC2 plus attached volume subtotal | USD 34 | USD 22 |

This is a difference in the infrastructure cost shown, not the price of executing the script. AWS-returned amounts follow `--metric` (default: `AmortizedCost`) and may themselves be provisional. If resource billing is incomplete, the report can mix returned amounts and supported estimates. Do not add service totals to resource subtotals: they are overlapping views of spending.

## How API execution charges are calculated

Cost Explorer charges USD 0.01 per request on the primary billing view. Custom views cost USD 0.01 per source per request: five sources make one request USD 0.05. Hourly data has separate usage-record charges. See [AWS Cost Explorer pricing](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/pricing/).

For the primary view:

```text
Cost Explorer execution cost = billable requests × USD 0.01
Additional resource-detail cost = additional billable requests × USD 0.01
```

One hundred primary-view requests cost USD 1. These amounts cover Cost Explorer requests only; other APIs and separately enabled features are not included.

### Example: two regions and about 40 EC2 instances

Assume one account, two regions, four or five clusters, two RDS databases, ten subnets, about 40 EC2 instances including cluster nodes, and associated volumes and networking. Use `--services all`, the primary billing view and explicit region codes.

The following scenarios assume successful requests, no additional discovery queries and no retries. Comparisons with the flag use the same valid recent period.

| Scenario | Aggregate response pages | EC2 detail response pages | Total requests | Cost Explorer cost per run |
| --- | ---: | ---: | ---: | ---: |
| Default, one page | 1 | 0 | 1 | USD 0.01 |
| Resource detail, one extra page | 1 | 1 | 2 | USD 0.02 |
| Resource detail, five extra pages | 1 | 5 | 6 | USD 0.06 |
| Default, larger aggregate response | 10 | 0 | 10 | USD 0.10 |

At 30 runs per month, the first two scenarios produce USD 0.30 and USD 0.60 respectively in Cost Explorer request charges, assuming unchanged request counts.

These are conditional examples, not a prediction of AWS pagination for 40 instances. Instance count alone is insufficient to determine pages: periods, dimensions and returned records also matter. A USD 0.50-per-run allowance can be a discretionary planning margin, but it is neither a measured price nor an enforced limit. The program currently has no spending cap or complete billable-request counter.

## Why one run can send several requests

The current HTML implementation submits an aggregate query across the selected services and regions. It can include multiple months in that query; it does not necessarily send one request per month, region, service or machine.

Additional requests can come from:

- **Pagination:** each next page is a separate SDK request.
- **Resource billing:** `--resource-costs` adds the EC2 detail query and its pages.
- **Filter discovery:** selecting `global` explicitly can require region-dimension discovery. Selecting EBS or VPC without EC2/all can require usage-type discovery.
- **Legacy workbook bundles:** `--format bundle` requests additional service breakdowns; the HTML examples above do not include them.
- **Retries or repeated runs:** SDK retries add attempts; AWS determines their billing treatment. Re-running collection issues fresh queries; there is no persistent billing-result cache between runs.

Changing a browser filter, navigating the HTML or downloading an embedded workbook performs local operations and sends no AWS requests. A single billing view therefore does not mean a single API request, while a detailed HTML report does not imply a paid billing query for every resource.

## Other possible costs

Inventory and Price List queries are separate from Cost Explorer calls. Do not multiply every SDK call by the Cost Explorer rate. Each service has its own pricing rules; read-only does not guarantee free access.

For example, AWS lists Secrets Manager API calls at USD 0.05 per 10,000 calls. At that rate, 100 calls would cost USD 0.0005 before any applicable allowances. Listing metadata does not create new secrets or new secret-storage charges. See [Secrets Manager pricing](https://aws.amazon.com/secrets-manager/pricing/).

Existing logging, network routing and the machine running the program can have their own costs. Running from an already available laptop does not provision AWS compute; running on a paid VM still incurs that VM's charges. Separately enabled Cost Explorer data features can have ongoing charges even when this program is not running. The script does not change those settings.

## Choosing a mode and controlling frequency

| Choice | Effect |
| --- | --- |
| Default HTML, no `--resource-costs` | Service/region billing with estimated resource detail; suitable for the usual monthly report |
| Add `--resource-costs` | Additional recent EC2 billing attribution and extra API requests |
| `--mode inventory` | No Cost Explorer queries; current-month inventory/pricing scenario, not historical billing; omit cost dates and resource-cost flags |
| `--no-prices` | Disables catalog queries, not Cost Explorer; some resource estimates become unavailable |
| Restrict services and regions | Reduces inventory scope; does not guarantee a proportional reduction in billing requests |
| Reuse an existing HTML report | No recollection or AWS requests when viewing/exporting locally |

Choose a collection schedule appropriate to how often the inventory or billing changes. Repeatedly opening the same report does not require running the program again. An AWS budget alert can help monitor spending, but is not a hard execution limit.

## GitHub Actions is separate from AWS collection

The repository workflow runs offline tests on standard `ubuntu-latest` runners with Python 3.11–3.14. Standard runner execution is free for public repositories. Private repositories and larger runners have different billing rules; artifact and cache storage have their own allowances. See [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

The workflow does not configure AWS credentials or collect from a real account. Its AWS fixtures block network access during simulations, so these tests do not incur AWS API charges. It caches dependencies and does not upload report artifacts. This is independent of the charges incurred when you run the tool against AWS yourself.
