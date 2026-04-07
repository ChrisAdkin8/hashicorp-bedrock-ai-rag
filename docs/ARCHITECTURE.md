# Architecture — HashiCorp Kendra RAG Pipeline

## Overview

A production-grade pipeline that ingests HashiCorp documentation into **Amazon Kendra** for use as a grounding source in AI coding assistants powered by **Amazon Bedrock** (Claude). The pipeline runs weekly via EventBridge Scheduler and self-heals from transient failures via Step Functions retry logic.

Kendra provides NLP-powered retrieval — no embedding model, vector database, or chunking configuration is required. Documents are pre-split semantically by the ingestion pipeline to improve passage quality.

## Components

### Orchestration Layer

| Component | Role |
|---|---|
| **EventBridge Scheduler** | Cron trigger — fires weekly (default: Sundays 02:00 UTC), passes input JSON to Step Functions |
| **Step Functions** | Pipeline orchestrator — 8-state ASL machine: Init → StartBuild → StartSync → WaitForSync → ListSyncJobs → CheckSyncStatus → ValidateRetrieval → PipelineComplete |

### Data Ingestion

| Component | Role |
|---|---|
| **AWS CodeBuild** | Runs data processing scripts inside an isolated Linux container (`BUILD_GENERAL1_MEDIUM`, 7 GB RAM, 4 vCPU) |
| **Amazon S3** | Staging area for processed markdown documents and Kendra metadata sidecar files (`.metadata.json`) |
| **`hashicorp/web-unified-docs`** | Single GitHub repo that is the authoritative documentation source for Vault, Consul, Nomad, Terraform Enterprise, and HCP Terraform. Docs live under `content/{product}/`. Individual product repos (`hashicorp/vault` etc.) have deprecated their `website/` trees. |

### Retrieval Index

| Component | Role |
|---|---|
| **Amazon Kendra** | Managed retrieval service — indexes S3 documents using built-in NLP, serves keyword + semantic queries |
| **Kendra S3 Data Source** | Watches the RAG S3 bucket; syncs new and changed documents on each pipeline run |

### AI Inference

| Component | Role |
|---|---|
| **Amazon Bedrock** | Hosts Claude models for AI inference — used at query time, not ingestion time |
| **MCP Server** (`mcp/server.py`) | Bridges Claude Code to Kendra via the Model Context Protocol; exposes `search_hashicorp_docs` and `get_index_info` tools |

### Supporting Infrastructure

| Component | Role |
|---|---|
| **IAM Roles** | Least-privilege execution roles for each service |
| **CloudWatch Logs** | Build logs from CodeBuild; Step Functions execution history |
| **SNS + CloudWatch Alarms** | Optional email alerts on pipeline failure (set `notification_email` variable) |
| **GitHub Actions (OIDC)** | CI/CD via OIDC federation — no long-lived IAM keys (set `create_github_oidc_provider = true`) |

## Data Flow

```
EventBridge Scheduler
    │  weekly cron (cron(0 2 ? * SUN *))
    ▼
Step Functions: Init
    │  inject params: index ID, data source ID, bucket, repo URL, pipeline_target
    ▼
Step Functions: StartBuild
    │  codebuild:startBuild.sync — waits for CodeBuild to complete
    │  passes PIPELINE_TARGET env var (all | docs | registry | discuss | blogs)
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
    └── post_build: aws s3 sync → S3 RAG bucket (--delete keeps bucket current)
    ▼
Step Functions: StartSync
    │  kendra:startDataSourceSyncJob — triggers Kendra to index new/changed S3 documents
    ▼
Step Functions: WaitForSync (60s wait) → ListSyncJobs → CheckSyncStatus (poll loop)
    │  kendra:listDataSourceSyncJobs until History[0].Status ∈ {SUCCEEDED, INCOMPLETE}
    ▼
Amazon Kendra
    │  NLP-powered index — keyword extraction, entity recognition, semantic ranking
    └─ Custom metadata attributes: product, product_family, source_type
    ▼
Step Functions: ValidateRetrieval
    │  Map state — 10 sequential kendra:query calls covering all product families
    └─ Confirms index is queryable before marking pipeline complete
    ▼
PipelineComplete
```

## IAM Design

Each service has a dedicated least-privilege execution role:

| Role | Principals | Key permissions |
|---|---|---|
| `rag-pipeline-codebuild` | `codebuild.amazonaws.com` | `s3:PutObject/GetObject/ListBucket/DeleteObject`, `logs:CreateLogGroup/PutLogEvents`, `secretsmanager:GetSecretValue` |
| `rag-pipeline-step-functions` | `states.amazonaws.com` | `codebuild:StartBuild/BatchGetBuilds`, `kendra:StartDataSourceSyncJob/ListDataSourceSyncJobs/Query` |
| `rag-pipeline-scheduler` | `scheduler.amazonaws.com` | `states:StartExecution` |
| `rag-kendra-s3` | `kendra.amazonaws.com` | `s3:GetObject/ListBucket` on the RAG bucket |
| `github-actions-terraform` | GitHub Actions OIDC | Terraform state read/write, infra describe |

## Document Processing

### Blog content fetching

`fetch_blogs.py` reads content directly from the RSS/Atom feed rather than scraping individual article URLs. hashicorp.com is behind Cloudflare bot protection — a plain HTTP GET to an article URL returns "We're verifying your browser" (~65 bytes) instead of article content.

Both feeds contain full article HTML inline:
- `https://www.hashicorp.com/blog/feed.xml` — Atom format, `<content>` tag per entry
- `https://medium.com/feed/hashicorp-engineering` — RSS format, `<content:encoded>` tag per item

`_parse_feed()` extracts the inline content tag and stores it in the entry dict. `process_feed()` HTML-strips it with BeautifulSoup before writing the output file. URL scraping (`fetch_article_content()`) is retained as a fallback for feeds that do not include inline content.

### Content exclusions

CDKTF (CDK for Terraform) documentation is intentionally excluded from the index:

| Script | Mechanism |
|---|---|
| `process_docs.py` | `CDKTF_EXCLUDE_RE` regex drops any file whose repo-relative path contains `cdktf/`, `terraform-cdk/`, or `cdk-for-terraform/` |
| `fetch_blogs.py` | Posts with a CDKTF keyword in the title, or ≥3 CDKTF mentions in the body, are skipped |
| `fetch_discuss.py` | Threads with a CDKTF keyword in the title are skipped |
| `fetch_github_issues.py` | Issues with a CDKTF keyword in the title are skipped |

### Semantic pre-splitting (`process_docs.py`)

Markdown files are split at `##`/`###` heading boundaries before upload. Each section becomes a separate file with its own Kendra metadata sidecar:

- Sections under 200 characters are merged into the previous section
- Sections over ~4,000 characters are split at code-fence boundaries to avoid cutting inside code blocks
- Each output file begins with a compact attribution prefix: `[source_type:product] Title — Section`

This pre-splitting ensures Kendra receives well-bounded passages rather than whole large documents, improving retrieval precision.

### Kendra metadata attributes

Every document has a `.metadata.json` sidecar file written by `generate_metadata.py`. Kendra reads these automatically when syncing:

```json
{
  "Title": "Vault — Auth Methods",
  "ContentType": "PLAIN_TEXT",
  "Attributes": {
    "product":        "vault",
    "product_family": "vault",
    "source_type":    "documentation"
  }
}
```

`DocumentId` and `_source_uri` are intentionally omitted:
- `DocumentId` — Kendra auto-assigns from the S3 object key. Providing a full `s3://` URI causes metadata validation failures.
- `_source_uri` — Kendra requires an HTTP/HTTPS URL; only S3 URIs are available at ingestion time.

These attributes are indexed by Kendra and available as filters in the `search_hashicorp_docs` MCP tool (`product_family`, `source_type`).

## Kendra Index Configuration

| Setting | Value |
|---|---|
| Edition | `ENTERPRISE_EDITION` (100,000 docs per SCU; 10,000 queries/day included) |
| Data source type | S3 |
| Sync schedule | On-demand (triggered by Step Functions after each CodeBuild run) |
| Custom attributes | `product` (STRING), `product_family` (STRING), `source_type` (STRING) |

> **Edition note:** `DEVELOPER_EDITION` is capped at 10,000 documents. This pipeline typically generates 10,000–30,000+ documents across all source types; `ENTERPRISE_EDITION` is required for production use. The edition cannot be changed in-place — changing it destroys and recreates the index.

## State Machine Design

The Step Functions ASL (`step-functions/rag_pipeline.asl.json`) uses:

- `.sync` resource integration for CodeBuild — automatic poll via CloudWatch Events, no sleep loop
- Manual poll loop for Kendra sync (60-second wait between `ListDataSourceSyncJobs` calls)
- Sequential `Map` state (`MaxConcurrency: 1`) for the 10 validation queries — avoids Kendra query throttling
- Retry on `States.TaskFailed` for the CodeBuild step (2 retries, 30-second interval, 2× backoff)
- Catch-all error states for CodeBuild (`BuildFailed`), Kendra sync (`SyncFailed`), and validation (`ValidationFailed`)
