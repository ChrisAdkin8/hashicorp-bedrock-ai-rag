# Architecture — HashiCorp RAG + Graph Pipeline

## Overview

A production-grade system on AWS providing two complementary data pipelines and a unified retrieval layer:

1. **HashiCorp Docs Pipeline** — ingests HashiCorp product documentation into Amazon Kendra for NLP-powered semantic retrieval. No embedding model, vector database, or chunking configuration required.
2. **Terraform Graph Store** — extracts Terraform resource dependency graphs from real workspaces via [rover](https://github.com/im2nguyen/rover) and loads them into Amazon Neptune (property graph database). This gives AI assistants a queryable model of real infrastructure topology.
3. **Unified MCP Server** — a single Model Context Protocol server that routes queries to Kendra or Neptune depending on intent, merges results, and surfaces them to Claude Code.

---

## Terraform Module Structure

```
terraform/
├── bootstrap/                    # Separate root module — creates remote state bucket (local backend)
│   └── main.tf                   # Calls modules/state-backend
├── modules/
│   ├── hashicorp-docs-pipeline/  # Kendra + ingestion pipeline
│   │   ├── iam.tf                # Five IAM roles (least-privilege)
│   │   ├── s3.tf                 # RAG docs S3 bucket
│   │   ├── kendra.tf             # Kendra index + data source
│   │   ├── locals.tf             # Computed names / IDs
│   │   ├── data.tf               # AWS data sources (aws_region, aws_caller_identity)
│   │   ├── variables.tf          # Module inputs (force_destroy, tags, …)
│   │   └── outputs.tf            # kendra_index_id, rag_bucket_name, state_machine_arn, …
│   ├── terraform-graph-store/    # Neptune + graph pipeline
│   │   ├── main.tf               # Neptune cluster, instances, subnet/param groups
│   │   ├── s3.tf                 # Graph staging S3 bucket
│   │   ├── codebuild.tf          # CodeBuild project (VPC-enabled), security groups
│   │   ├── sfn.tf                # Step Functions state machine, EventBridge scheduler, alarms
│   │   ├── lambda.tf             # Neptune proxy: Lambda + API Gateway + IAM + SGs (opt-in)
│   │   ├── nat.tf                # Optional NAT gateway for VPC-attached CodeBuild internet access
│   │   ├── iam.tf                # CodeBuild, Step Functions, Scheduler roles
│   │   ├── data.tf               # AWS data sources (aws_region, aws_caller_identity)
│   │   ├── locals.tf             # Computed names
│   │   ├── variables.tf          # Module inputs (vpc_id, subnet_ids, repo_uris, create_neptune_proxy, …)
│   │   └── outputs.tf            # Neptune endpoints, neptune_proxy_url, state_machine_arn, …
│   └── state-backend/            # KMS-encrypted S3 state bucket
│       ├── main.tf               # S3 bucket with encryption, versioning, public access block
│       ├── locals.tf             # Deterministic bucket name (account_id + sha256 suffix)
│       ├── data.tf               # aws_caller_identity
│       └── outputs.tf            # bucket_name, bucket_arn, backend_config
├── main.tf                       # Calls both pipeline modules
├── variables.tf                  # All root-level inputs (region for provider; module-specific vars)
├── outputs.tf                    # Proxies all module outputs (Neptune outputs null-safe via try())
└── versions.tf                   # required_version >= 1.10 < 1.15, aws ~> 5.100, archive ~> 2.7
```

---

## Components

### Orchestration

| Component | Role |
|---|---|
| **EventBridge Scheduler (docs)** | Cron trigger for the docs pipeline (default: Sundays 02:00 UTC) |
| **EventBridge Scheduler (graph)** | Cron trigger for the graph pipeline (default: Sundays 03:00 UTC) |
| **Step Functions (docs)** | Docs pipeline orchestrator — 8-state ASL machine |
| **Step Functions (graph)** | Graph pipeline orchestrator — Map state over repo list (MaxConcurrency: 3) |

### Data Ingestion — Docs Pipeline

| Component | Role |
|---|---|
| **AWS CodeBuild** | Runs data processing scripts (`BUILD_GENERAL1_MEDIUM`, 7 GB RAM, 4 vCPU) |
| **Amazon S3 (RAG docs)** | Staging area for processed markdown and Kendra `.metadata.json` sidecars |
| **Amazon Kendra** | Managed NLP retrieval index — keyword + semantic ranking, no embedding model needed |
| **Kendra S3 Data Source** | Watches the RAG S3 bucket; syncs new and changed documents after each CodeBuild run |

### Data Ingestion — Graph Pipeline

| Component | Role |
|---|---|
| **AWS CodeBuild (graph)** | VPC-enabled; runs Terraform + rover per repo, then `ingest_graph.py`; can reach Neptune privately |
| **Amazon S3 (graph staging)** | Stores rover JSON output before Neptune ingestion (30-day lifecycle) |
| **[rover](https://github.com/im2nguyen/rover)** | Terraform plan visualiser — outputs `nodes[]` and `edges[]` JSON representing the resource DAG |
| **Amazon Neptune** | Managed property graph database (openCypher). Stores resource nodes and dependency edges. |

### Retrieval

| Component | Role |
|---|---|
| **MCP Server** (`mcp/server.py`) | Bridges Claude Code to Kendra and Neptune via the Model Context Protocol; exposes `search_hashicorp_docs`, `get_resource_dependencies`, `find_resources_by_type`, and `get_index_info` tools |
| **Neptune Proxy** (API Gateway + Lambda) | Optional (`create_neptune_proxy = true`). HTTP API with IAM auth fronting a VPC Lambda that SigV4-signs and forwards openCypher queries to Neptune. Allows the MCP server to reach Neptune from outside the VPC without tunnels. |
| **Amazon Bedrock** | Hosts Claude models for AI inference — used at query time, not ingestion time |

### Supporting Infrastructure

| Component | Role |
|---|---|
| **IAM Roles** | Least-privilege execution roles for each service (separate per pipeline) |
| **CloudWatch Logs** | CodeBuild build logs; Step Functions execution history |
| **SNS + CloudWatch Alarms** | Optional email alerts on pipeline failure (`notification_email` variable) |
| **GitHub Actions (OIDC)** | CI/CD via OIDC federation — no long-lived IAM keys (`create_github_oidc_provider = true`) |
| **KMS** | Encrypts the Terraform state S3 bucket |

---

## Data Flow — Docs Pipeline

```
EventBridge Scheduler (cron(0 2 ? * SUN *))
    │
    ▼
Step Functions: Init
    │  inject params: index ID, data source ID, bucket, repo URL, pipeline_target
    ▼
Step Functions: StartBuild  [codebuild:startBuild.sync — waits for completion]
    │
    ▼
CodeBuild  [steps marked * are conditional on PIPELINE_TARGET]
    ├── pre_build:  clone_repos.sh        — [*all, docs]     shallow-clone HashiCorp + provider repos
    │               discover_modules.py   — [*all, registry] discover Terraform Registry modules
    │               clone_modules.sh      — [*all, registry] clone discovered module repos
    ├── build:      process_docs.py       — [*all, docs, registry] semantic splitting + attribution prefixes
    │               fetch_github_issues.py— [*all only]      (parallel)
    │               fetch_discuss.py      — [*all, discuss]  (parallel)
    │               fetch_blogs.py        — [*all, blogs]    (parallel)
    │               deduplicate.py        — cross-source deduplication
    │               generate_metadata.py  — write Kendra .metadata.json sidecars
    └── post_build: aws s3 sync → S3 RAG bucket
    ▼
Step Functions: StartSync
    │  kendra:startDataSourceSyncJob
    ▼
Step Functions: WaitForSync (60s) → ListSyncJobs → CheckSyncStatus (poll loop)
    ▼
Amazon Kendra  [NLP index — keyword + semantic ranking]
    └─ Custom metadata: product, product_family, source_type
    ▼
Step Functions: ValidateRetrieval
    │  Map state — 10 sequential kendra:query calls across all product families
    ▼
PipelineComplete
```

---

## Data Flow — Graph Pipeline

```
EventBridge Scheduler (cron(0 3 ? * SUN *))
    │
    ▼
Step Functions: GraphPipelineStart
    │
    ▼
Map state (MaxConcurrency: 3) — iterate over graph_repo_uris
    │
    ├── For each repo:
    │       codebuild:startBuild.sync  [VPC-enabled CodeBuild]
    │           ├── terraform init + plan -out=tfplan
    │           ├── rover --tfplan-json-file tfplan.json --standalone
    │           └── python ingest_graph.py  (openCypher MERGE into Neptune)
    │
    └── Retry on TaskFailed (2 retries, 30s interval, 2× backoff)
    ▼
GraphPipelineComplete
```

---

## IAM Design

### hashicorp-docs-pipeline module

| Role | Principals | Key permissions |
|---|---|---|
| `rag-pipeline-codebuild` | `codebuild.amazonaws.com` | `s3:PutObject/GetObject/ListBucket/DeleteObject`, `logs:*`, `secretsmanager:GetSecretValue`, `kms:Decrypt/GenerateDataKey` (via service condition) |
| `rag-pipeline-step-functions` | `states.amazonaws.com` | `codebuild:StartBuild/BatchGetBuilds`, `kendra:StartDataSourceSyncJob/ListDataSourceSyncJobs/Query` |
| `rag-pipeline-scheduler` | `scheduler.amazonaws.com` | `states:StartExecution` on the docs state machine ARN only |
| `rag-kendra-s3` | `kendra.amazonaws.com` | `s3:GetObject/ListBucket` on the RAG bucket |
| `github-actions-terraform` | GitHub Actions OIDC | Terraform state read/write, infra describe (scoped per-service, no wildcard) |

### terraform-graph-store module

| Role | Principals | Key permissions |
|---|---|---|
| `graph-codebuild` | `codebuild.amazonaws.com` | `ec2:CreateNetworkInterface*` (VPC), `neptune-db:connect`, `s3:PutObject/GetObject` on staging bucket, `logs:*` |
| `graph-step-functions` | `states.amazonaws.com` | `codebuild:StartBuild/BatchGetBuilds`, `logs:*` |
| `graph-scheduler` | `scheduler.amazonaws.com` | `states:StartExecution` on the graph state machine ARN only |
| `graph-neptune-proxy` | `lambda.amazonaws.com` | `neptune-db:connect`, `neptune-db:ReadDataViaQuery` (read-only), VPC networking, CloudWatch Logs. Created when `create_neptune_proxy = true`. |

---

## Document Processing (Docs Pipeline)

### Semantic pre-splitting

Markdown files are split at `##`/`###` heading boundaries before upload. Each section becomes a separate S3 object with its own `.metadata.json` sidecar:

- Sections under 200 characters are merged into the previous section
- Sections over ~4,000 characters are split at code-fence boundaries
- Each output file begins with a compact attribution prefix: `[source_type:product] Title — Section`

### Content exclusions

CDKTF documentation is intentionally excluded:

| Script | Mechanism |
|---|---|
| `process_docs.py` | `CDKTF_EXCLUDE_RE` drops files whose path contains `cdktf/`, `terraform-cdk/`, or `cdk-for-terraform/` |
| `fetch_blogs.py` | Posts with CDKTF in the title or ≥3 CDKTF body mentions are skipped |
| `fetch_discuss.py` | Threads with CDKTF in the title are skipped |
| `fetch_github_issues.py` | Issues with CDKTF in the title are skipped |

### Blog content fetching

`fetch_blogs.py` reads content from RSS/Atom feed inline tags — it does **not** scrape article URLs. hashicorp.com is Cloudflare-protected; article URLs return "We're verifying your browser" instead of content.

Both feeds include full article HTML inline:
- `https://www.hashicorp.com/blog/feed.xml` — Atom format, `<content>` tag
- `https://medium.com/feed/hashicorp-engineering` — RSS format, `<content:encoded>` tag

---

## Kendra Index Configuration

| Setting | Value |
|---|---|
| Edition | `ENTERPRISE_EDITION` (100,000 docs per SCU; 10,000 queries/day included) |
| Data source type | S3 (via `s3_configuration` with `inclusion_patterns = ["**/*.md"]` — includes `.metadata.json` sidecars automatically) |
| Sync schedule | On-demand (triggered by Step Functions after each CodeBuild run) |
| Custom attributes | `product` (STRING), `product_family` (STRING), `source_type` (STRING) |

> **Edition note:** `DEVELOPER_EDITION` is capped at 10,000 documents. This pipeline typically generates 10,000–30,000+ documents. The edition cannot be changed in-place — changing it destroys and recreates the index.

---

## Neptune Configuration

| Setting | Value / default |
|---|---|
| Engine | Neptune (openCypher + Gremlin) |
| Default instance class | `db.r6g.large` |
| IAM authentication | Enabled (`neptune_iam_auth_enabled = true`) |
| VPC | Required — cluster is in private subnets; CodeBuild uses VPC config to reach it |
| Port | 8182 |
| Backup retention | 7 days |

The CodeBuild security group has an egress rule permitting port 8182 to the Neptune security group. No public access is exposed.

### Neptune Proxy (Optional)

When `create_neptune_proxy = true`, an API Gateway HTTP API + Lambda function is deployed to expose Neptune queries from outside the VPC:

| Setting | Value |
|---|---|
| API Gateway | HTTP API, IAM authorization, `POST /query` route |
| Lambda | Python 3.12, 256 MB, 30s timeout, VPC-attached (same subnets as Neptune) |
| Security group | Egress to Neptune SG on port 8182; egress 443 for AWS APIs |
| IAM | `neptune-db:connect` + `neptune-db:ReadDataViaQuery` (read-only) |
| Authentication | Callers sign requests with SigV4 for `execute-api` service |

The MCP server routes through the proxy when `NEPTUNE_PROXY_URL` is set, falling back to direct Neptune access otherwise.

---

## State Machine Design

### Docs pipeline ASL (`step-functions/rag_pipeline.asl.json`)

- `.sync` resource integration for CodeBuild — automatic poll via CloudWatch Events
- Manual poll loop for Kendra sync (60-second wait between `ListDataSourceSyncJobs` calls)
- Sequential `Map` state (`MaxConcurrency: 1`) for 10 validation queries — avoids Kendra throttling
- Retry on `States.TaskFailed` for the CodeBuild step (2 retries, 30s interval, 2× backoff)

### Graph pipeline ASL (`step-functions/graph_pipeline.asl.json`)

- `Map` state with `MaxConcurrency: 3` — runs up to 3 repo ingestions concurrently
- `.sync` CodeBuild integration per repo
- Retry on `States.TaskFailed` (2 retries, 30s interval, 2× backoff)
- Catch: routes to `PipelineFailed` state on unrecoverable errors
