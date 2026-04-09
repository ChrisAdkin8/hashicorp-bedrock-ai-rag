# HashiCorp RAG + Graph Pipeline

A production-grade Terraform repository that provisions and operates two complementary data pipelines on AWS:

1. **HashiCorp Docs Pipeline** — ingests HashiCorp product documentation, forum threads, blog posts, and GitHub issues into **Amazon Kendra** for NLP-powered retrieval.
2. **Terraform Graph Store** — runs `terraform plan` over your Terraform workspaces, extracts the resource dependency graph via [rover](https://github.com/im2nguyen/rover), and loads it into **Amazon Neptune** (optional).

Both pipelines are surfaced to AI coding assistants through a unified **MCP server** layer.

Clone it, set a few variables, and run `task up` — a single command provisions all infrastructure and ingests the documentation.

---

## Architecture

```
EventBridge Schedulers (weekly cron)
    │                          │
    ▼                          ▼
Docs Pipeline              Graph Pipeline
(Step Functions)           (Step Functions)
    │                          │
    ▼                          ▼
CodeBuild                  CodeBuild (per repo)
(ingest scripts)           terraform plan → rover → ingest_graph.py
    │                          │
    ▼                          ▼
S3 (RAG docs)              Amazon Neptune
    │                      (property graph)
    ▼                          │
Amazon Kendra              API Gateway + Lambda
(NLP index)                (Neptune proxy, opt-in)
    │                          │
    └──────────┬───────────────┘
               ▼
         MCP Server
    (search_hashicorp_docs,
     get_resource_dependencies,
     find_resources_by_type,
     get_index_info)
               │
               ▼
         Claude Code
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for detailed component tables, data flow, IAM design, and state machine internals.

See [docs/diagrams/unified-data-layer.svg](docs/diagrams/unified-data-layer.svg) for the unified data layer diagram.

---

## Terraform Modules

| Module | Path | Purpose |
|---|---|---|
| **hashicorp-docs-pipeline** | `terraform/modules/hashicorp-docs-pipeline` | S3 bucket, Kendra index + data source, CodeBuild project, Step Functions state machine, EventBridge scheduler, IAM roles |
| **terraform-graph-store** | `terraform/modules/terraform-graph-store` | Neptune cluster, graph staging S3 bucket, CodeBuild (with VPC config), Step Functions state machine, EventBridge scheduler, IAM roles |
| **state-backend** | `terraform/modules/state-backend` | KMS-encrypted S3 bucket for Terraform remote state |

The `terraform-graph-store` module is opt-in — set `create_neptune = true` and supply VPC/subnet IDs to enable it.

Bootstrap (`terraform/bootstrap/`) is a separate root module with a local backend that creates the remote state bucket before the main module runs.

---

## Data Sources

### Docs Pipeline (Kendra)

| Source | Collection method | What's ingested |
|---|---|---|
| **HashiCorp core products** | Git clone | `hashicorp/web-unified-docs` (Vault, Consul, Nomad, TFE, HCP Terraform), Terraform, Packer, Boundary, Waypoint |
| **Terraform providers** | Git clone | AWS, Azure, GCP, Kubernetes, Helm, Docker, Vault, Consul, Nomad, and more |
| **Terraform Registry modules** | Registry API + git clone | HashiCorp-verified module docs |
| **Sentinel policy libraries** | Git clone | Policy definitions and usage docs |
| **GitHub Issues** | GitHub API | Last 365 days, 8 priority repos |
| **HashiCorp Discuss** | Discourse API | Last 365 days, 9 product categories |
| **HashiCorp Blog** | RSS/Atom feed (inline content) | Last 365 days |

### Graph Pipeline (Neptune)

The graph pipeline runs `terraform plan -out=tfplan` in each workspace repo, extracts the resource dependency graph using rover, and loads nodes and edges into Neptune via openCypher queries. This gives AI assistants a queryable model of real infrastructure topology.

---

## Prerequisites

- **AWS account** with billing enabled
- **AWS CLI** installed and credentials configured — environment variables, `aws configure`, or AWS SSO
- **Terraform** >= 1.10
- **Python** 3.11+
- **Task** ([taskfile.dev](https://taskfile.dev)) — `brew install go-task`
- **Python packages** — `pip install boto3 pyyaml requests pytest beautifulsoup4 mcp`
- **shellcheck** — `brew install shellcheck`
- **jq** — `brew install jq`
- **Amazon Bedrock model access** — enable Claude (e.g. `claude-sonnet-4-20250514`) in Bedrock console → Model access for AI inference via the MCP server
- *(Neptune only)* An existing VPC with private subnets where CodeBuild can reach Neptune on port 8182
- *(Neptune proxy only)* Set `neptune_create_proxy = true` to expose Neptune via API Gateway + Lambda for access from outside the VPC

---

## Quick Start

1. **Clone the repository**

   ```bash
   git clone https://github.com/ChrisAdkin8/aws-hashi-knowledge-base
   cd aws-hashi-knowledge-base
   ```

2. **Configure AWS credentials**

   ```bash
   export AWS_ACCESS_KEY_ID=AKIA...
   export AWS_SECRET_ACCESS_KEY=...
   export AWS_SESSION_TOKEN=...   # required for temporary credentials

   # or use a named profile / AWS SSO
   aws sso login --profile my-profile
   ```

   Verify with `task login`.

3. **Create a virtual environment and install Python dependencies**

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install boto3 pyyaml requests pytest beautifulsoup4 mcp
   ```

4. **Deploy**

   ```bash
   task up REPO_URI=https://github.com/ChrisAdkin8/aws-hashi-knowledge-base
   ```

   `task up` runs these steps automatically:

   | Step | What happens |
   |---|---|
   | 0 | Preflight checks — tools, auth, Python packages, repo files, Terraform formatting |
   | 1 | Bootstrap: create remote state S3 bucket (`terraform/bootstrap`) |
   | 2 | All AWS infrastructure provisioned via `terraform apply` — IAM, S3, Kendra, CodeBuild, Step Functions, EventBridge, Neptune (if `create_neptune=true`) |
   | 3 | First docs pipeline run triggered — CodeBuild ingests docs to S3, Kendra syncs and indexes |

   Optional overrides:

   ```bash
   task up REPO_URI=https://github.com/... REGION=eu-west-1
   task up REPO_URI=https://github.com/... SKIP_PIPELINE=true
   ```

   > **Note:** Kendra index creation takes 10–30 minutes on first deploy.

5. **Validate retrieval quality**

   ```bash
   task docs:test
   ```

6. *(Optional)* **Populate the graph database**

   Enable Neptune first by adding `create_neptune = true` and VPC variables to `terraform/terraform.tfvars`, then re-run `task apply`. Once the cluster is up:

   ```bash
   task graph:populate GRAPH_REPO_URIS="https://github.com/org/infra-repo"
   # Multiple repos (space-separated):
   task graph:populate GRAPH_REPO_URIS="https://github.com/org/infra-repo https://github.com/org/app-repo"
   # Or set graph_repo_uris in terraform.tfvars and omit the override:
   task graph:populate
   ```

---

## Configuration

### Docs Pipeline

| Variable | Default | Description |
|---|---|---|
| `region` | `us-east-1` | AWS region for all resources |
| `repo_uri` | (required) | GitHub HTTPS URL of this repo — CodeBuild clones it to run pipeline scripts |
| `kendra_edition` | `ENTERPRISE_EDITION` | `DEVELOPER_EDITION` (~$810/mo, 10k docs) or `ENTERPRISE_EDITION` (~$1,400/mo, 100k docs/SCU). Cannot change in-place. |
| `refresh_schedule` | `cron(0 2 ? * SUN *)` | EventBridge cron expression (UTC) for the docs pipeline |
| `scheduler_timezone` | `Europe/London` | Timezone for the EventBridge Scheduler |
| `notification_email` | `""` | Email for CloudWatch alarms (empty = disabled) |
| `create_github_oidc_provider` | `false` | Create GitHub Actions OIDC provider + IAM role for CI/CD |
| `force_destroy` | `false` | Allow S3 bucket destruction even if non-empty (non-prod only) |
| `tags` | `{}` | Additional tags applied to all resources |

### Graph Pipeline (Neptune)

| Variable | Default | Description |
|---|---|---|
| `create_neptune` | `false` | Set `true` to deploy the Neptune module |
| `neptune_vpc_id` | `""` | VPC ID for the Neptune cluster |
| `neptune_subnet_ids` | `[]` | Subnet IDs for the Neptune subnet group |
| `neptune_allowed_cidr_blocks` | `[]` | CIDR blocks permitted to reach Neptune on port 8182 |
| `neptune_cluster_identifier` | `hashicorp-rag-graph` | Neptune cluster identifier |
| `neptune_instance_class` | `db.r6g.large` | Neptune instance class |
| `neptune_instance_count` | `1` | Number of Neptune instances (1 = writer only) |
| `neptune_iam_auth_enabled` | `true` | Enable IAM authentication for Neptune |
| `neptune_deletion_protection` | `true` | Prevent cluster deletion via Terraform |
| `neptune_backup_retention_days` | `7` | Automated backup retention (days) |
| `graph_repo_uris` | `[]` | GitHub HTTPS URLs of Terraform workspace repos to ingest into Neptune |
| `graph_refresh_schedule` | `cron(0 3 ? * SUN *)` | EventBridge cron for the graph pipeline (UTC) |
| `graph_codebuild_compute_type` | `BUILD_GENERAL1_MEDIUM` | CodeBuild compute type for graph pipeline |
| `neptune_create_nat_gateway` | `false` | Create a NAT gateway so VPC-attached CodeBuild can reach the internet |
| `neptune_codebuild_subnet_cidr` | `172.31.64.0/24` | CIDR for the private CodeBuild subnet created when `neptune_create_nat_gateway = true` |
| `neptune_create_proxy` | `false` | Create an API Gateway + Lambda proxy for Neptune queries from outside the VPC |

---

## Using the RAG Index with AI Coding Assistants

### Claude Code via MCP Server

The MCP server in `mcp/server.py` exposes the Kendra index as tools that Claude Code calls automatically.

```bash
task mcp:install    # install mcp + boto3 + requests into .venv
task mcp:setup      # register with Claude Code (auto-detects IDs from Terraform), then restart
task mcp:test       # smoke-test retrieval (Kendra + Neptune if deployed)
```

Available tools:

- **`search_hashicorp_docs`** — keyword + semantic search with optional `product_family` and `source_type` filters
- **`get_resource_dependencies`** — traverse Terraform resource dependency graph (downstream, upstream, or both)
- **`find_resources_by_type`** — list all resources of a given type, optionally filtered by repository
- **`get_index_info`** — inspect region, Kendra index, Neptune connectivity, and status

### Claude Code via Amazon Bedrock

Route Claude Code through your AWS account's Bedrock endpoint:

```bash
task claude:setup                              # default (us-east-1, claude-sonnet-4-20250514)
task claude:setup CLAUDE_REGION=eu-west-2
task claude:setup PERSIST=true                 # persist to ~/.bashrc
```

### Targeted pipeline runs

```bash
task docs:run TARGET=blogs     # refresh blogs only
task docs:run TARGET=discuss   # refresh Discuss threads only
task docs:run TARGET=docs      # product repo documentation only
task docs:run                  # full run (default)
```

Valid `TARGET` values: `all` (default), `docs`, `registry`, `discuss`, `blogs`.

### Programmatic access

```python
import boto3
import json
import urllib.parse
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

region = "us-east-1"

# 1. Retrieve documentation context from Kendra
kendra = boto3.client("kendra", region_name=region)
response = kendra.query(
    IndexId="<KENDRA_INDEX_ID>",
    QueryText="How do I use Vault dynamic secrets with Terraform?",
    PageSize=5,
)
docs_context = "\n\n---\n\n".join(
    item["DocumentExcerpt"]["Text"]
    for item in response.get("ResultItems", [])
)

# 2. Query the Terraform dependency graph from Neptune
endpoint, port = "<NEPTUNE_ENDPOINT>", 8182
url = f"https://{endpoint}:{port}/openCypher"
query = "MATCH (a:Resource)-[:DEPENDS_ON]->(b:Resource) WHERE a.type = 'aws_iam_role' RETURN b.id, b.type LIMIT 10"
body = urllib.parse.urlencode({"query": query, "parameters": json.dumps({})})
headers = {"Content-Type": "application/x-www-form-urlencoded"}
creds = boto3.Session().get_credentials().get_frozen_credentials()
aws_req = AWSRequest(method="POST", url=url, data=body, headers=headers)
SigV4Auth(creds, "neptune-db", region).add_auth(aws_req)
graph_context = json.dumps(
    requests.post(url, data=body, headers=dict(aws_req.headers), timeout=30).json().get("results", [])
)

# 3. Pass combined context to Claude via Bedrock
bedrock = boto3.client("bedrock-runtime", region_name=region)
response = bedrock.converse(
    modelId="anthropic.claude-sonnet-4-20250514-v1:0",
    messages=[{
        "role": "user",
        "content": [{"text": f"Docs:\n{docs_context}\n\nGraph:\n{graph_context}\n\nHow do I use Vault dynamic secrets with Terraform?"}],
    }],
)
print(response["output"]["message"]["content"][0]["text"])
```

---

## Task Reference

| Task | Description |
|---|---|
| `task up` | Full deploy: preflight → bootstrap → terraform apply → populate Kendra + Neptune |
| `task down` | Alias for `task destroy` |
| `task destroy` | Destroy all Terraform-managed infrastructure |
| `task login` | Verify AWS credentials |
| `task bootstrap` | Create/verify remote state S3 bucket |
| `task init` | Initialise Terraform (auto-detects state bucket) |
| `task init:upgrade` | Re-initialise Terraform with `-upgrade` (refresh providers/modules) |
| `task fmt` | Format all Terraform files |
| `task fmt:check` | Check Terraform formatting (CI-friendly, no writes) |
| `task validate` | Validate Terraform configuration |
| `task plan` | `terraform plan` (saves plan to `tfplan`) |
| `task apply` | Interactive `terraform apply` (plan + confirm) |
| `task output` | Print all Terraform outputs |
| `task oidc:import` | Import an existing GitHub OIDC provider into Terraform state |
| `task preflight` | Run all preflight checks (tools, auth, packages, files, Terraform) |
| `task docs:run` | Trigger a docs pipeline run and wait for completion |
| `task docs:test` | Run retrieval validation queries against Kendra |
| `task docs:status` | List last 5 docs pipeline executions |
| `task test:token-efficiency` | Compare token cost across backends (`MODE=kendra\|graph\|combined\|all`) |
| `task graph:populate` | Trigger a graph pipeline run and wait for completion |
| `task graph:status` | List last 5 graph pipeline executions |
| `task graph:test` | Validate Neptune graph has nodes and edges |
| `task mcp:install` | Install MCP server dependencies |
| `task mcp:setup` | Register MCP server with Claude Code |
| `task mcp:test` | Smoke-test MCP server connectivity |
| `task claude:setup` | Configure Claude Code to use Amazon Bedrock |
| `task ci` | Run all CI checks (fmt + validate + shellcheck + tests) |
| `task shellcheck` | Lint all shell scripts |
| `task test` | Run Python unit tests |

---

## Costs

| Component | Notes |
|---|---|
| **Amazon Kendra Enterprise Edition** | ~$1,400/month flat + $35/SCU/month for additional storage. Includes 10,000 queries/day. |
| **Amazon Kendra Developer Edition** | ~$810/month, 10,000 document limit. Suitable for evaluation only. |
| **Amazon Neptune** | ~$0.20–$0.35/hour per instance (`db.r6g.large`). Optional — only deployed when `create_neptune = true`. |
| **Neptune Proxy (API Gateway + Lambda)** | API Gateway: per-request pricing; Lambda: per-invocation + compute. Optional — deployed when `neptune_create_proxy = true`. Negligible for MCP query volumes. |
| **Amazon Bedrock** | Pay-per-token for Claude inference; negligible for query-time use |
| **S3** | Storage for processed markdown and graph staging; negligible cost |
| **CodeBuild** | Per-build-minute (`BUILD_GENERAL1_MEDIUM`); ~weekly runs |
| **Step Functions** | Per-state-transition; negligible for weekly runs |
| **EventBridge Scheduler** | Negligible |

**Kendra is the dominant cost driver.** Use `DEVELOPER_EDITION` for evaluation and switch to `ENTERPRISE_EDITION` when document volume exceeds 10,000 or query volume exceeds 4,000/day.

---

## How to monitor and troubleshoot

See [docs/RUNBOOK.md](docs/RUNBOOK.md) for the full operational runbook.

Quick links (replace `REGION` with your deployment region — auto-detected from `terraform/terraform.tfvars`):

- Step Functions: `https://console.aws.amazon.com/states/home?region=REGION`
- CodeBuild: `https://console.aws.amazon.com/codesuite/codebuild/projects?region=REGION`
- Kendra: `https://console.aws.amazon.com/kendra/home?region=REGION`
- Neptune: `https://console.aws.amazon.com/neptune/home?region=REGION`
- CloudWatch Logs (CodeBuild): `https://console.aws.amazon.com/cloudwatch/home?region=REGION#logsV2:log-groups/log-group/$252Faws$252Fcodebuild$252Frag-hashicorp-pipeline`

---

## Licence

Apache 2.0 — see [LICENSE](LICENSE).
