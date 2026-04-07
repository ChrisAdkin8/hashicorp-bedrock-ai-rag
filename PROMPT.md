# HashiCorp Bedrock AI RAG — Implementation Reference (Kendra Architecture)

This document is an AI agent / LLM prompt reference. It describes the **current** production implementation: a Kendra-based RAG pipeline that ingests HashiCorp documentation into **Amazon Kendra** for use as a grounding source in AI coding assistants powered by **Amazon Bedrock** (Claude). Kendra provides NLP-powered retrieval — no embedding model, vector database, or chunking configuration is required.

---

## 1. Overview

A production-grade weekly pipeline that:

1. Scrapes and processes HashiCorp documentation (markdown) from GitHub and API sources into S3.
2. Triggers Amazon Kendra to index the new/changed documents.
3. Validates retrieval quality with 10 sequential `kendra:query` calls.
4. Exposes the index to Claude Code (and other Bedrock-powered assistants) via an MCP server.

**What was removed** (old Bedrock Knowledge Bases architecture):
- Amazon Bedrock Knowledge Bases — REMOVED
- OpenSearch Serverless — REMOVED
- Titan Embeddings — REMOVED
- `StartIngestionJob` / `GetIngestionJob` API calls — REPLACED by `kendra:startDataSourceSyncJob`

---

## 2. Architecture

### Pipeline Flow

```
EventBridge Scheduler (weekly cron: "cron(0 2 ? * SUN *)")
    │
    ▼
Step Functions (8 states):
  Init → StartBuild → StartSync → WaitForSync → ListSyncJobs → CheckSyncStatus → ValidateRetrieval → PipelineComplete
              │              │
         CodeBuild      kendra:startDataSourceSyncJob
         (S3 upload)    (indexes new/changed docs)
```

### AWS Services

| Service | Role |
|---|---|
| **Amazon Kendra** (ENTERPRISE_EDITION, 100k docs/SCU) | Managed NLP retrieval index. No embedding model or vector DB. |
| **Amazon S3** | Staging area for processed markdown docs and `.metadata.json` sidecar files. |
| **AWS CodeBuild** (BUILD_GENERAL1_MEDIUM, amazonlinux2-x86_64-standard:5.0, 120 min timeout) | Runs data processing scripts. |
| **AWS Step Functions** | 8-state ASL pipeline orchestrator. |
| **Amazon EventBridge Scheduler** | Weekly cron trigger. |
| **Amazon Bedrock** | Claude models used at **query time only** (not during ingestion). |
| **MCP Server** (`mcp/server.py`) | Bridges Claude Code to Kendra via Model Context Protocol. |
| **CloudWatch + SNS** | Optional email alerts (controlled by `notification_email` variable). |
| **GitHub Actions OIDC** | CI/CD federation (controlled by `create_github_oidc_provider` variable). |

### IAM Roles

| Role name | Principal | Purpose |
|---|---|---|
| `hashicorp-rag-kendra` | kendra.amazonaws.com | Kendra index S3 access + CloudWatch metrics |
| `rag-pipeline-codebuild` | codebuild.amazonaws.com | S3 read/write + Secrets Manager |
| `rag-pipeline-step-functions` | states.amazonaws.com | CodeBuild + Kendra orchestration |
| `rag-pipeline-scheduler` | scheduler.amazonaws.com | Start Step Functions execution |
| `github-actions-terraform` | OIDC (GitHub Actions) | Terraform CI/CD |

---

## 3. Repository Layout

```
.
├── Taskfile.yml
├── AGENTS.md
├── PROMPT_BEDROCK_AI.md          # This file — implementation reference
├── README.md
├── .gitignore
├── .github/workflows/terraform.yml
├── docs/
│   ├── ARCHITECTURE.md
│   ├── MCP_SERVER.md
│   ├── RUNBOOK.md
│   └── diagrams/
│       ├── architecture.svg
│       └── ingestion_pipeline.svg
├── terraform/
│   ├── versions.tf
│   ├── variables.tf
│   ├── main.tf
│   ├── outputs.tf
│   └── terraform.tfvars.example
├── mcp/
│   ├── server.py
│   ├── test_server.py
│   └── requirements.txt
├── step-functions/
│   └── rag_pipeline.asl.json
├── codebuild/
│   ├── buildspec.yml
│   └── scripts/
│       ├── clone_repos.sh
│       ├── discover_modules.py
│       ├── clone_modules.sh
│       ├── process_docs.py
│       ├── fetch_github_issues.py
│       ├── fetch_discuss.py
│       ├── fetch_blogs.py
│       ├── deduplicate.py
│       ├── generate_metadata.py
│       ├── requirements.txt         # pyyaml, requests, pytest, beautifulsoup4, lxml
│       └── tests/
│           ├── test_process_docs.py
│           ├── test_fetch_github_issues.py
│           └── test_deduplicate.py
└── scripts/
    ├── deploy.sh
    ├── bootstrap_state.sh
    ├── run_pipeline.sh
    ├── setup_claude_bedrock.sh
    ├── setup_mcp.sh
    └── test_retrieval.py
```

---

## 4. Terraform Implementation

### Variables (`terraform/variables.tf`)

| Variable | Default | Description |
|---|---|---|
| `region` | `us-west-2` | AWS region |
| `repo_uri` | (required) | GitHub HTTPS URL for the repository |
| `kendra_edition` | `ENTERPRISE_EDITION` | `DEVELOPER_EDITION` (10k docs, ~$810/mo) or `ENTERPRISE_EDITION` (~$1,400/mo, 100k docs/SCU) |
| `refresh_schedule` | `cron(0 2 ? * SUN *)` | EventBridge cron expression (UTC) |
| `scheduler_timezone` | `"UTC"` | Timezone for the schedule |
| `notification_email` | `""` | Email for CloudWatch alarms (empty = disabled) |
| `create_github_oidc_provider` | `false` | Create OIDC provider resource for GitHub Actions |

### Key Resources (`terraform/main.tf`)

**S3 bucket** — bucket name uses a deterministic suffix:
```hcl
resource "aws_s3_bucket" "rag_docs" {
  bucket = "hashicorp-rag-docs-${var.region}-${substr(sha256(data.aws_caller_identity.current.account_id), 0, 8)}"
}
```

**Kendra index**:
```hcl
resource "aws_kendra_index" "main" {
  name    = "hashicorp-rag-index"
  edition = var.kendra_edition
  role_arn = aws_iam_role.kendra.arn

  document_metadata_configuration_updates {
    name = "product"
    type = "STRING_VALUE"
    search { facetable = true; searchable = true; displayable = true }
  }
  document_metadata_configuration_updates {
    name = "product_family"
    type = "STRING_VALUE"
    search { facetable = true; searchable = true; displayable = true }
  }
  document_metadata_configuration_updates {
    name = "source_type"
    type = "STRING_VALUE"
    search { facetable = true; searchable = true; displayable = true }
  }
}
```

**Kendra S3 data source** — uses `inclusion_patterns`, not `exclusion_patterns` (see Known Gotchas):
```hcl
resource "aws_kendra_data_source" "s3" {
  index_id = aws_kendra_index.main.id
  name     = "hashicorp-rag-s3"
  type     = "S3"
  role_arn = aws_iam_role.kendra.arn

  configuration {
    s3_configuration {
      bucket_name        = aws_s3_bucket.rag_docs.id
      inclusion_patterns = ["*.md"]  # NOT exclusion_patterns — see Known Gotchas
    }
  }
}
```

**`kendra_data_source_id` local** — the Terraform resource `.id` is `"<data_source_id>/<index_id>"` — extract just the data source ID:
```hcl
locals {
  kendra_data_source_id = split("/", aws_kendra_data_source.s3.id)[0]
}
```

**Step Functions state machine**:
```hcl
resource "aws_sfn_state_machine" "rag_pipeline" {
  name     = "rag-pipeline"
  role_arn = aws_iam_role.step_functions.arn
  definition = file("${path.module}/../step-functions/rag_pipeline.asl.json")
}
```

**EventBridge Scheduler** — passes runtime parameters as input JSON:
```hcl
resource "aws_scheduler_schedule" "rag_weekly_refresh" {
  name                         = "rag-weekly-refresh"
  schedule_expression          = var.refresh_schedule
  schedule_expression_timezone = var.scheduler_timezone

  flexible_time_window { mode = "OFF" }

  target {
    arn      = aws_sfn_state_machine.rag_pipeline.arn
    role_arn = aws_iam_role.scheduler.arn
    input = jsonencode({
      kendra_index_id       = aws_kendra_index.main.id
      kendra_data_source_id = local.kendra_data_source_id
      bucket_name           = aws_s3_bucket.rag_docs.id
      region                = var.region
      repo_url              = var.repo_uri
    })
  }
}
```

---

## 5. Step Functions (8 States)

State machine definition lives in `step-functions/rag_pipeline.asl.json`.

| # | State | Type | Action |
|---|---|---|---|
| 1 | **Init** | Pass | Injects validation queries and parameters into execution context |
| 2 | **StartBuild** | Task | `codebuild:startBuild.sync` — automatic polling via CloudWatch Events, no sleep loop |
| 3 | **StartSync** | Task | `kendra:startDataSourceSyncJob` — triggers Kendra to index new/changed S3 documents |
| 4 | **WaitForSync** | Wait | 60-second pause before polling sync status |
| 5 | **ListSyncJobs** | Task | `kendra:listDataSourceSyncJobs` — retrieves sync job history |
| 6 | **CheckSyncStatus** | Choice | `History[0].Status == SUCCEEDED` or `INCOMPLETE` → proceed; `SYNCING` → back to WaitForSync; else → SyncFailed |
| 7 | **ValidateRetrieval** | Map (MaxConcurrency: 1) | 10 sequential `kendra:query` calls covering all product families. Sequential to avoid Kendra query throttling. |
| 8 | **PipelineComplete** | Pass/Succeed | Terminal success state |

**Error states**: `BuildFailed` (CodeBuild), `SyncFailed` (Kendra sync), `ValidationFailed` (retrieval validation).

### Step Functions IAM Policy

The `rag-pipeline-step-functions` role requires:
- `codebuild:StartBuild`, `codebuild:StopBuild`, `codebuild:BatchGetBuilds`
- `kendra:StartDataSourceSyncJob`, `kendra:ListDataSourceSyncJobs`, `kendra:StopDataSourceSyncJob`, `kendra:Query`, `kendra:Retrieve`
- `events:PutTargets`, `events:PutRule`, `events:DescribeRule`, `events:DeleteRule`, `events:RemoveTargets`

---

## 6. CodeBuild Pipeline

### `buildspec.yml` Phases

**install**
```yaml
install:
  runtime-versions:
    python: 3.12
  commands:
    - pip install -r codebuild/scripts/requirements.txt
```

**pre_build** — repository cloning:
```yaml
pre_build:
  commands:
    - bash codebuild/scripts/clone_repos.sh
    - python codebuild/scripts/discover_modules.py
    - bash codebuild/scripts/clone_modules.sh
```

**build** — document processing:
```yaml
build:
  commands:
    - python codebuild/scripts/process_docs.py
    - python codebuild/scripts/fetch_github_issues.py & python codebuild/scripts/fetch_discuss.py & python codebuild/scripts/fetch_blogs.py & wait
    - python codebuild/scripts/deduplicate.py
    - python codebuild/scripts/generate_metadata.py --bucket ${RAG_BUCKET}
```

**post_build** — S3 sync:
```yaml
post_build:
  commands:
    - aws s3 sync /codebuild/output/cleaned/ s3://${RAG_BUCKET}/ --delete
```

Build environment: `BUILD_GENERAL1_MEDIUM`, image `amazonlinux2-x86_64-standard:5.0`, 120-minute timeout.

---

## 7. Data Sources (7 Source Types)

### Git-Cloned Sources

| Source type | Repos / paths | Notes |
|---|---|---|
| `documentation` | `hashicorp/web-unified-docs` (Vault, Consul, Nomad, TFE, HCP Terraform), terraform, terraform-website, packer, boundary, waypoint, terraform-docs-agents | See web-unified-docs section below |
| `provider` | 14 Terraform provider repos: aws, azurerm, google, kubernetes, helm, docker, vault, consul, nomad, etc. | Cloned with `clone_repo_optional()` (soft-fail) |
| `module` | Dynamically discovered from Terraform Registry via `discover_modules.py` | Discovery runs in pre_build |
| `sentinel` | 4 sentinel policy library repos | Cloned with `clone_repo_optional()` |

### API-Fetched Sources

| Source type | Source | Parameters |
|---|---|---|
| `issue` | GitHub Issues API | 8 priority repos, 365-day lookback, min body 50 chars, label denylist: `{stale, wontfix, duplicate, invalid, spam}` |
| `discuss` | HashiCorp Discuss forum | 9 product categories, 365-day lookback, min 1 reply |
| `blog` | **Inline content from RSS/Atom feeds** (NOT scraped from article URLs) | See below |

### Blog Fetching — Why Inline Feed Content

`hashicorp.com` is behind Cloudflare bot protection. A plain HTTP GET returns `"We're verifying your browser"` (~65 chars) instead of article content. Scraping article URLs does not work.

Both feeds already contain the full article HTML in their content tags:

| Feed | URL | Tag with full content |
|---|---|---|
| HashiCorp Blog (Atom) | `https://www.hashicorp.com/blog/feed.xml` | `<content>` (20 entries) |
| HashiCorp Engineering (RSS) | `https://medium.com/feed/hashicorp-engineering` | `<content:encoded>` (10 entries) |

`fetch_blogs.py` calls `_parse_feed()` to extract the inline content, then `process_feed()` HTML-strips it with BeautifulSoup before writing markdown.

---

## 8. Document Processing

### `clone_repos.sh` — CORE_REPOS vs Optional

`CORE_REPOS`: `web-unified-docs`, `terraform`, `packer`, `boundary`, `waypoint`, `terraform-docs-agents`, `terraform-website`.

Core repo clone failures call `exit 1`. Provider and sentinel repos use `clone_repo_optional()` (soft-fail with a warning).

### web-unified-docs (Key Design Decision)

`hashicorp/web-unified-docs` is the **authoritative unified documentation source** for Vault, Consul, Nomad, Terraform Enterprise, and HCP Terraform. Individual product repos (`hashicorp/vault`, `hashicorp/consul`, etc.) have deprecated their `website/` trees.

`process_docs.py` `REPO_CONFIG` uses a `repo_dir` override field to allow multiple product entries to share one cloned repo directory:

```python
"vault": {
    "repo_dir": "web-unified-docs",
    "docs_subdirs": ["content/vault"],
    "product_family": "vault",
    "source_type": "documentation",
},
"consul": {
    "repo_dir": "web-unified-docs",
    "docs_subdirs": ["content/consul"],
    ...
},
"nomad": {
    "repo_dir": "web-unified-docs",
    "docs_subdirs": ["content/nomad"],
    ...
},
"terraform-enterprise": {
    "repo_dir": "web-unified-docs",
    "docs_subdirs": ["content/terraform-enterprise"],
    "product_family": "terraform",
    ...
},
"hcp-terraform": {
    "repo_dir": "web-unified-docs",
    "docs_subdirs": ["content/terraform-docs-common/docs/cloud-docs"],
    "product_family": "terraform",
    ...
},
```

### Semantic Pre-splitting (`process_docs.py`)

- Split at `##` / `###` heading boundaries → each section becomes a separate file + sidecar.
- Sections < 200 chars: merged into the previous section.
- Sections > ~4000 chars: split at code-fence boundaries (never mid-block).
- Attribution prefix added: `[source_type:product] Title — Section`

### Document Attribution Format

```
[provider:aws] aws_instance — Argument Reference
[discuss:terraform] How do I manage multiple workspaces?
[issue:vault] #1234 (closed): Dynamic secrets not rotating
[blog:terraform] HCP Terraform adds IP allow lists
```

### CDKTF Exclusion (Layered Defence)

CDKTF is intentionally excluded from the index at every stage:

| Script | Mechanism |
|---|---|
| `process_docs.py` | `CDKTF_EXCLUDE_RE` regex drops files with `cdktf/`, `terraform-cdk/`, or `cdk-for-terraform/` in path |
| `fetch_blogs.py` | Title CDKTF match OR ≥3 CDKTF mentions in body → skip |
| `fetch_discuss.py` | Title CDKTF match → skip |
| `fetch_github_issues.py` | Title CDKTF match → skip |

---

## 9. Kendra Metadata Sidecar Format

`generate_metadata.py` writes a `.metadata.json` file alongside every `.md` file.

Naming convention: `path.name + ".metadata.json"` — this **preserves the `.md` extension** in the sidecar name (e.g. `doc.md.metadata.json`).

```json
{
  "Title": "Vault — Auth Methods",
  "ContentType": "PLAIN_TEXT",
  "Attributes": {
    "product": "vault",
    "product_family": "vault",
    "source_type": "documentation"
  }
}
```

### What is Intentionally Omitted

| Field | Why omitted |
|---|---|
| `DocumentId` | Kendra auto-assigns from the S3 object key. Including a full `s3://` URI caused metadata validation failures. |
| `_source_uri` | Kendra requires an HTTP/HTTPS URI. Only an S3 URI is available at ingestion time. Including it caused validation failures. |

---

## 10. Kendra Index Configuration

### Edition

| Edition | Monthly cost | Document limit | Use case |
|---|---|---|---|
| `DEVELOPER_EDITION` | ~$810 | 10,000 | Evaluation only — pipeline generates 10k–30k+ docs |
| `ENTERPRISE_EDITION` | ~$1,400 + $35/SCU | 100,000/SCU | Production |

Changing edition requires `terraform destroy` + `terraform apply`, then re-run `task pipeline:run`. It cannot be changed in-place.

### S3 Data Source — `inclusion_patterns` vs `exclusion_patterns`

```hcl
configuration {
  s3_configuration {
    bucket_name        = aws_s3_bucket.rag_docs.id
    inclusion_patterns = ["*.md"]  # NOT exclusion_patterns!
  }
}
```

Using `exclusion_patterns = ["*.metadata.json"]` (the original approach) was **wrong** — it blocked Kendra from reading `.metadata.json` files in **all** sync participation, including as metadata sidecars, which caused `"invalid metadata"` errors.

`inclusion_patterns = ["*.md"]` achieves the same goal (sidecars not indexed as documents) while correctly allowing Kendra to read them as metadata.

### Custom Metadata Attributes

Three custom string attributes are defined on the index (facetable, searchable, displayable):
- `product` — e.g. `"vault"`, `"aws"` (provider name)
- `product_family` — e.g. `"vault"`, `"terraform"`, `"packer"`
- `source_type` — e.g. `"documentation"`, `"provider"`, `"issue"`, `"discuss"`, `"blog"`

Kendra reads these from the `.metadata.json` sidecar files during sync.

---

## 11. MCP Server (`mcp/server.py`)

The MCP server bridges Claude Code (and other MCP-compatible clients) to the Kendra index.

### Tools Exposed

**`search_hashicorp_docs`**
```python
def search_hashicorp_docs(
    query: str,
    top_k: int = 5,
    min_score: float = 0.0,
    product_family: str = "",
    source_type: str = "",
) -> list[dict]:
    ...
```
Issues `kendra:query` with optional attribute filters. Returns documents with their attribution prefix, score, and custom attributes.

**`get_index_info`**
```python
def get_index_info() -> dict:
    ...
```
Returns: region, index ID, edition, status, caller identity (from STS).

### AttributeFilter Usage

```python
# Single filter
{"EqualsTo": {"Key": "product_family", "Value": {"StringValue": "vault"}}}

# Combined filter
{
  "AndAllFilters": [
    {"EqualsTo": {"Key": "product_family", "Value": {"StringValue": "vault"}}},
    {"EqualsTo": {"Key": "source_type", "Value": {"StringValue": "documentation"}}},
  ]
}
```

Kendra returns `product`, `product_family`, and `source_type` custom attributes directly from the `.metadata.json` sidecars — no path inference is required (unlike the old Bedrock Knowledge Bases architecture).

---

## 12. Known Gotchas

| Issue | Root Cause | Fix |
|---|---|---|
| Kendra metadata `"invalid metadata"` errors | `exclusion_patterns = ["*.metadata.json"]` blocks sidecars from ALL sync participation | Use `inclusion_patterns = ["*.md"]` instead |
| `DocumentId` validation failure | Full `s3://` URI in `DocumentId` field — Kendra expects S3 key only | Omit `DocumentId` entirely; Kendra auto-assigns from S3 key |
| `_source_uri` validation failure | Kendra requires HTTP/HTTPS URI; only `s3://` is available at ingestion time | Omit `_source_uri` from `Attributes` |
| Blog posts not fetched (0 files) | `hashicorp.com` is Cloudflare-protected — scraping returns `"We're verifying your browser"` | Extract inline content from `<content>` (Atom) / `<content:encoded>` (RSS) tags |
| `lxml` not installed | `BeautifulSoup(..., "xml")` requires lxml | `lxml>=5.0` in `requirements.txt`; installed in CodeBuild via pip |
| Vault/Consul/Nomad missing from S3 | Individual product repos deprecated their `website/` trees | Use `hashicorp/web-unified-docs` with `repo_dir` override in `REPO_CONFIG` |
| Clone failures silently swallowed | `\|\| { echo "WARN"; }` pattern in old `clone_repos.sh` | Hard-fail (`exit 1`) on `CORE_REPOS`; `clone_repo_optional()` for providers/sentinel |
| Kendra edition change requires destroy | Edition (`DEVELOPER`/`ENTERPRISE`) cannot be changed in-place via Terraform | `terraform destroy` + `terraform apply`; re-run `task pipeline:run` |
| `kendra_data_source_id` wrong format | `aws_kendra_data_source.s3.id` = `"<data_source_id>/<index_id>"` | Use `split("/", aws_kendra_data_source.s3.id)[0]` in locals |
| `DEVELOPER_EDITION` document limit | Capped at 10,000 docs; pipeline generates 10k–30k+ | Use `ENTERPRISE_EDITION` for production |
| GitHub Issues API rate limit | 60 req/hr unauthenticated | Set `GITHUB_TOKEN` in Secrets Manager |

---

## 13. Adding a GitHub Token

GitHub Issues fetching (`fetch_github_issues.py`) is rate-limited to 60 requests/hour unauthenticated. To raise this to 5,000 req/hr:

1. Create a GitHub Personal Access Token (read-only, `public_repo` scope).
2. Store it in AWS Secrets Manager with a tag that matches the CodeBuild IAM condition (e.g. `{"Project": "hashicorp-rag"}`).
3. The `rag-pipeline-codebuild` IAM role allows `secretsmanager:GetSecretValue` scoped to resources with that tag.
4. Reference the secret ARN in the buildspec environment variables or retrieve it at runtime in the script.

---

## 14. CI/CD (GitHub Actions OIDC)

Workflow file: `.github/workflows/terraform.yml`

Steps:
1. `terraform fmt -check` — enforces formatting
2. `terraform validate` — validates configuration
3. Trivy scan — IaC security scan

OIDC federation resources (created when `create_github_oidc_provider = true`):
- `aws_iam_openid_connect_provider.github`
- `aws_iam_role.github_actions` (name: `github-actions-terraform`)

Trust condition:
```hcl
condition {
  test     = "StringLike"
  variable = "token.actions.githubusercontent.com:sub"
  values   = ["repo:*/${local.repo_name}:*"]
}
```

---

## 15. Costs

| Service | Cost | Notes |
|---|---|---|
| Kendra ENTERPRISE_EDITION | ~$1,400/month flat + $35/SCU/month additional storage | 10,000 queries/day included |
| Kendra DEVELOPER_EDITION | ~$810/month | 10,000 document limit — evaluation only |
| Amazon Bedrock (Claude) | Pay-per-token | Query time only; not used during ingestion |
| S3 | Negligible | GB range of markdown |
| CodeBuild | Per-build-minute | Weekly runs only |
| Step Functions + EventBridge | Negligible | — |

**Key saving vs old architecture**: OpenSearch Serverless minimum cost (~$350/month) is eliminated. Kendra is the dominant cost driver.

---

## 16. Code Quality Requirements

- **Python**: type hints required; `ruff check` must pass; use `logging` module (not `print`); docstrings on all functions.
- **Bash**: `shellcheck` must pass on all shell scripts.
- **Terraform**: `terraform fmt` + `terraform validate` must pass.
- **No hardcoded values**: no account IDs, bucket names, or index IDs in committed files.
- **No secrets**: no credentials or tokens in code or logs.
- **IAM**: least-privilege with resource conditions where applicable (e.g. Secrets Manager tag condition on CodeBuild role).
- **`requirements.txt`** (`codebuild/scripts/requirements.txt`): `pyyaml`, `requests`, `pytest`, `beautifulsoup4`, `lxml>=5.0`.
