# PROMPT.md — HashiCorp RAG Pipeline Infrastructure (AWS)

> **Note:** This file documents what was actually built. It reflects the real
> implementation including all fixes applied during initial deployment. Use it
> as a reference for understanding the codebase or rebuilding from scratch.

---

## Project Overview

A production-grade repository that provisions and operates a RAG system on AWS.
Ingests HashiCorp documentation from GitHub repos and the Terraform Registry API
into an Amazon Kendra index, kept current via automated weekly refresh. Kendra
provides NLP-powered retrieval — no embedding model, vector database, or chunking
configuration required.

Clone, set variables, run `task up REPO_URI=<url>` — fully operational pipeline.

**What was removed** (old architecture):
- Amazon Bedrock Knowledge Bases, OpenSearch Serverless, Titan Embeddings —
  replaced by `kendra:startDataSourceSyncJob`.

---

## Architecture

See `docs/ARCHITECTURE.md` for full component tables, data flow diagrams, and
IAM design.

```
EventBridge Scheduler (weekly cron)
    │
    ▼
Step Functions (8 states):
  Init → StartBuild → StartSync → WaitForSync → ListSyncJobs → CheckSyncStatus → ValidateRetrieval → PipelineComplete
              │              │
         CodeBuild      kendra:startDataSourceSyncJob
         (S3 upload)    (indexes new/changed docs)
```

**Key design decisions:**
- Two parallel tracks inside CodeBuild: **git clone** (7 core + 14 providers +
  modules + sentinel) and **API fetch** (issues, discuss, blogs). Both converge
  at a single `aws s3 sync` upload step.
- `.sync` integration for CodeBuild (automatic polling via CloudWatch Events).
  Kendra sync uses a manual poll loop (no `.sync` integration available).
- Blog content extracted from RSS/Atom feed inline tags — NOT scraped URLs
  (Cloudflare blocks scraping).

### Chunking Strategy

Kendra manages its own NLP-powered chunking. Documents are semantically
pre-split by `process_docs.py` before upload:
1. Split at `##`/`###` headings. Sections < 200 chars merged; sections > ~4000
   chars split at code-fence boundaries.
2. Attribution prefix: `[source_type:product] Title — Section`.

### Cross-Source Deduplication

`deduplicate.py` removes near-duplicates by SHA-256 of normalised body content
before upload. Files < 100 chars excluded. Sorted path order for determinism.

---

## Terraform Implementation

### Provider & Backend

- `aws ~> 5.100`, `archive ~> 2.7`, `time ~> 0.12`. Required version `>= 1.10, < 1.15`.
- S3 backend — bucket and region supplied at init via `-backend-config` flags.

### Key Variables

| Variable | Default | Description |
|---|---|---|
| `region` | `"us-east-1"` | AWS region |
| `repo_uri` | (required) | GitHub HTTPS URL for the repository |
| `kendra_edition` | `"ENTERPRISE_EDITION"` | `DEVELOPER_EDITION` (~$810/mo) or `ENTERPRISE_EDITION` (~$1,400/mo) |
| `refresh_schedule` | `"cron(0 2 ? * SUN *)"` | EventBridge cron (UTC) |
| `create_neptune` | `false` | Deploy Neptune graph database module |
| `neptune_vpc_id` | `""` | VPC ID (required when `create_neptune = true`) |
| `neptune_create_proxy` | `false` | API Gateway + Lambda proxy for Neptune queries |
| `graph_repo_uris` | `[]` | GitHub HTTPS URLs of Terraform repos to ingest |

### Key Resources

- **S3 bucket** — deterministic name: `hashicorp-rag-docs-${region}-${sha8(account_id)}`.
- **Kendra index** with 3 custom string attributes: `product`, `product_family`,
  `source_type` (all facetable, searchable, displayable).
- **Kendra S3 data source** — `inclusion_patterns = ["*.md"]` (NOT
  `exclusion_patterns` — that blocks `.metadata.json` sidecars).
- **`kendra_data_source_id` local** — extracts just the data source ID from the
  composite `"<data_source_id>/<index_id>"` resource ID.
- **Step Functions** — loads ASL from `step-functions/rag_pipeline.asl.json`.
- **EventBridge Scheduler** — passes runtime params as input JSON to Step Functions.
- **IAM roles**: Kendra (S3 access), CodeBuild (S3 + Secrets Manager),
  Step Functions (CodeBuild + Kendra orchestration), Scheduler (start execution),
  optional GitHub Actions OIDC.

---

## Step Functions — step-functions/rag_pipeline.asl.json

Eight states:

| # | State | Type | Action |
|---|---|---|---|
| 1 | Init | Pass | Inject validation queries and parameters |
| 2 | StartBuild | Task | `codebuild:startBuild.sync` (auto-polling via CloudWatch Events) |
| 3 | StartSync | Task | `kendra:startDataSourceSyncJob` |
| 4 | WaitForSync | Wait | 60-second pause |
| 5 | ListSyncJobs | Task | `kendra:listDataSourceSyncJobs` |
| 6 | CheckSyncStatus | Choice | SUCCEEDED/INCOMPLETE → proceed; SYNCING → loop back |
| 7 | ValidateRetrieval | Map | 10 sequential `kendra:query` calls (MaxConcurrency: 1) |
| 8 | PipelineComplete | Succeed | Terminal state |

Error states: `BuildFailed`, `SyncFailed`, `ValidationFailed`.

The Step Functions role requires `codebuild:*Build*`, `kendra:*DataSource*`,
`kendra:Query/Retrieve`, and `events:*` (for `.sync` managed rules).

---

## CodeBuild Pipeline — codebuild/buildspec.yml

`PIPELINE_TARGET` env var gates phases: `all` (default), `docs`, `registry`,
`discuss`, `blogs`.

| Phase | Steps |
|---|---|
| **install** | Python 3.12, `pip install -r requirements.txt` |
| **pre_build** | Conditional: `clone_repos.sh` [all/docs], `discover_modules.py` + `clone_modules.sh` [all/registry] |
| **build** | Conditional: `process_docs.py` [all/docs/registry], parallel fetch (issues/discuss/blogs), `deduplicate.py`, `generate_metadata.py` |
| **post_build** | `aws s3 sync` to RAG bucket |

Note: use `export TARGET=` (not bare `TARGET=`) to avoid CodeBuild YAML parser
misinterpreting `${VAR:-default}` syntax.

Build environment: `BUILD_GENERAL1_MEDIUM`, `amazonlinux2-x86_64-standard:5.0`, 120-min timeout.

---

## Data Sources

### Git-Cloned

| Source type | Repos | Notes |
|---|---|---|
| `documentation` | `web-unified-docs` (Vault/Consul/Nomad/TFE/HCP TF) + standalone | `repo_dir` override for shared repos |
| `provider` | 14 Terraform provider repos | `clone_repo_optional()` (soft-fail) |
| `module` | Terraform Registry (dynamic discovery) | `discover_modules.py` |
| `sentinel` | 4 policy library repos | `clone_repo_optional()` |

### API-Fetched

| Source type | Source | Key parameters |
|---|---|---|
| `issue` | GitHub REST API | 8 priority repos, 365-day lookback, quality filters |
| `discuss` | Discourse JSON API | 9 categories, 365-day lookback, accepted-answer promotion |
| `blog` | Atom/RSS feeds | **Inline content** (NOT scraped — Cloudflare blocks) |

### Document Processing

- **Semantic splitting** at `##`/`###` headings.
- **CDKTF exclusion**: layered across all processing/fetch scripts.
- **web-unified-docs**: authoritative source for Vault/Consul/Nomad/TFE/HCP TF.
  Uses `repo_dir` override so multiple products share one clone.
- **Kendra metadata sidecars**: `.metadata.json` files alongside every `.md`.
  Naming: `doc.md.metadata.json`. `DocumentId` and `_source_uri` intentionally
  omitted (Kendra auto-assigns from S3 key; only S3 URIs available at ingest time).

---

## MCP Server

See `docs/MCP_SERVER.md` for full tool reference and configuration.

Five tools across two backends:

- `search_hashicorp_docs` — Kendra query with `AttributeFilter`, URI + content
  deduplication, chunk header stripping. Accepts `product`, `product_family`,
  and `source_type` filters. Returns formatted string.
- `get_index_info` — Kendra index configuration, available metadata filters,
  and default retrieval settings.
- `get_resource_dependencies` — Neptune graph walk (downstream/upstream/both),
  with optional `repository` filter. Returns formatted string.
- `find_resources_by_type` — Neptune resource lookup with optional `repository`
  filter and `limit` (1-500). Returns formatted string.
- `get_graph_info` — Neptune connectivity, node/edge counts, repo count.

All tools return formatted strings (not dicts/lists) for consistent output.
Kendra returns `product`, `product_family`, `source_type` directly from
`.metadata.json` sidecars. URI path inference is used as a fallback for
`product` filtering.

---

## Known Gotchas

| Issue | Fix |
|---|---|
| Kendra `"invalid metadata"` errors | Use `inclusion_patterns = ["*.md"]`, NOT `exclusion_patterns` |
| `DocumentId` / `_source_uri` validation failure | Omit both — Kendra auto-assigns from S3 key |
| Blog posts 0 files | Extract inline feed content, don't scrape URLs |
| `lxml` not installed | Add `lxml>=5.0` to `requirements.txt` |
| Vault/Consul/Nomad missing from S3 | Use `web-unified-docs` with `repo_dir` override |
| Kendra edition change requires destroy | `terraform destroy` + `task apply` + `task docs:run` |
| `kendra_data_source_id` wrong format | `split("/", ...)[0]` in locals |
| `DEVELOPER_EDITION` doc limit (10k) | Use `ENTERPRISE_EDITION` for production |
| GitHub API rate limit (60 req/hr) | Set `GITHUB_TOKEN` in Secrets Manager |
| YAML parse error in buildspec | Prefix `${VAR:-default}` with `export` |

---

## CI/CD

`.github/workflows/terraform.yml` — runs on push/PR: `terraform fmt -check`,
`terraform validate`, Trivy scan.

OIDC federation (opt-in via `create_github_oidc_provider = true`): creates
`aws_iam_openid_connect_provider.github` and `github-actions-terraform` role.

`task ci` runs locally via parallel deps: `fmt:check`, `validate`, `shellcheck`, `test`.

---

## Costs

| Service | Notes |
|---|---|
| Kendra ENTERPRISE_EDITION | ~$1,400/mo flat + $35/SCU/mo |
| Kendra DEVELOPER_EDITION | ~$810/mo, 10k doc limit |
| Amazon Bedrock (Claude) | Pay-per-token, query time only |
| S3 / CodeBuild / Step Functions / EventBridge | Negligible for weekly runs |

Kendra is the dominant cost driver. OpenSearch Serverless (~$350/mo) eliminated
vs old architecture.
