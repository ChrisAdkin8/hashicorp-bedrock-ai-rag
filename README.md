# HashiCorp Kendra RAG Pipeline

A production-grade Terraform repository that provisions and operates a Retrieval-Augmented Generation (RAG) system on Amazon Web Services. The system ingests HashiCorp's public documentation from GitHub repositories, GitHub Issues, Discourse forums, and blog posts into **Amazon Kendra**, and surfaces that content through **Amazon Bedrock** (Claude) via an MCP server or programmatic API. The index is kept current via automated weekly refresh.

Clone it, set a few variables, and run `task up` — a single command provisions all infrastructure and ingests the documentation.

---

## Architecture

The pipeline has three layers:

**Ingestion** — AWS CodeBuild clones HashiCorp repos, scrapes APIs, splits and enriches markdown, and uploads to S3.

**Indexing** — Amazon Kendra indexes the S3 documents using its built-in NLP-powered retrieval (no embedding model or vector database required).

**Retrieval** — Claude (via Amazon Bedrock) calls the Kendra index through an MCP server, receiving semantically ranked passages as grounding context.

```
EventBridge Scheduler (weekly cron)
    │
    ▼
Step Functions: Init → StartBuild → StartSync → WaitForSync → ValidateRetrieval → PipelineComplete
                           │                │
                     CodeBuild          Kendra data source sync
                     (S3 upload)        (indexes new/changed docs)
```

### Ingestion Pipeline

The CodeBuild job runs two parallel tracks:

- **Git clone track** — shallow-clones `hashicorp/web-unified-docs` (the authoritative source for Vault, Consul, Nomad, Terraform Enterprise, and HCP Terraform docs) plus Terraform, Packer, Boundary, Waypoint, and all Terraform provider repos. Splits markdown at `##`/`###` heading boundaries, enriches with compact attribution prefixes, and writes cleaned files to `/codebuild/output/cleaned/`.
- **API fetch track** — queries the GitHub Issues API, HashiCorp Discuss, and HashiCorp/Engineering blogs in parallel; filters and formats results as markdown.

Both tracks converge at a single `aws s3 sync` upload, after which Step Functions triggers a Kendra data source sync job to index all new and changed documents.

The pipeline supports targeted runs via the `TARGET` parameter, allowing individual content sources to be refreshed without re-ingesting everything:

| Target | What runs |
|---|---|
| `all` (default) | Full pipeline — docs, registry modules, discuss, blogs, GitHub issues |
| `docs` | Product documentation from HashiCorp repos only |
| `registry` | Terraform public registry modules only |
| `discuss` | HashiCorp Discuss threads only |
| `blogs` | HashiCorp blog posts only |

```bash
task pipeline:run TARGET=blogs     # refresh blogs only
task pipeline:run TARGET=discuss   # refresh Discuss threads only
task pipeline:run                  # full run (default)
```

> **CDKTF excluded** — CDKTF (CDK for Terraform) documentation is intentionally excluded from the index. Path-based exclusion in `process_docs.py` drops any file under a `cdktf/` or `terraform-cdk/` directory, and title/keyword filters in the blog, discuss, and issues fetch scripts skip CDKTF-primary content.

---

## Data Sources

The pipeline ingests content from seven source types across three collection methods:

### Git-cloned sources

| Source | `source_type` | Repos | What's ingested |
|---|---|---|---|
| **HashiCorp core products** | `documentation` | web-unified-docs (vault, consul, nomad, terraform-enterprise, hcp-terraform), terraform, terraform-website, packer, boundary, waypoint | Official product docs — `content/{product}/` from web-unified-docs; `website/` from individual repos |
| **Terraform providers** | `provider` | AWS, Azure, GCP, Kubernetes, Helm, Docker, Vault, Consul, Nomad, and more | Resource and data source reference docs |
| **Terraform Registry modules** | `module` | Dynamically discovered via Registry API | Docs from HashiCorp-verified modules |
| **Sentinel policy libraries** | `sentinel` | 4 repos | Policy definitions and usage documentation |

### API-fetched sources

| Source | `source_type` | What's ingested |
|---|---|---|
| **GitHub Issues** | `issue` | Issues from the last 365 days across 8 priority repos. Filtered: PRs excluded, minimum body length, minimum comment count, label denylist. |
| **HashiCorp Discuss** | `discuss` | Forum threads with at least 1 reply from the last 365 days across 9 product categories. Accepted answers reordered to front. |
| **HashiCorp Blog** | `blog` | Posts from the last 365 days from hashicorp.com/blog and medium.com/hashicorp-engineering. Content read from inline feed tags (`<content>` / `<content:encoded>`) — article URLs are Cloudflare-protected and cannot be scraped. |

### Document metadata

Every document begins with a compact attribution prefix (~15 tokens vs ~100 tokens for YAML front matter):

```
[provider:aws] aws_instance — Argument Reference

[discuss:terraform] How do I manage multiple workspaces?

[issue:vault] #1234 (closed): Dynamic secrets not rotating
```

Kendra custom metadata attributes (`product`, `product_family`, `source_type`) are written in `.metadata.json` sidecar files alongside each document. These attributes are used at query time to filter results by product or source type.

---

## Prerequisites

- **AWS account** with billing enabled
- **AWS CLI** installed and credentials configured — environment variables, `aws configure`, or AWS SSO
- **Terraform** >= 1.5
- **Python** 3.11+
- **Task** ([taskfile.dev](https://taskfile.dev)) — `brew install go-task`
- **Python packages** — `pip install boto3 pyyaml requests pytest beautifulsoup4`
- **shellcheck** — `brew install shellcheck`
- **jq** — `brew install jq`
- **Amazon Bedrock model access** — enable Claude (e.g. `claude-sonnet-4-20250514`) in Bedrock console → Model access for AI inference via the MCP server

---

## Quick Start

1. **Clone the repository**

   ```bash
   git clone https://github.com/ChrisAdkin8/hashicorp-bedrock-ai-rag
   cd hashicorp-bedrock-ai-rag
   ```

2. **Configure AWS credentials**

   ```bash
   # Environment variables (preferred for temporary/CI sessions)
   export AWS_ACCESS_KEY_ID=AKIA...
   export AWS_SECRET_ACCESS_KEY=...
   export AWS_SESSION_TOKEN=...   # required for temporary credentials

   # Named profile
   aws configure

   # AWS SSO
   aws sso login --profile my-profile
   ```

   Verify with `task login`.

3. **Create a virtual environment and install Python dependencies**

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install boto3 pyyaml requests pytest beautifulsoup4 mcp
   ```

4. **Deploy everything with one command**

   ```bash
   task up REPO_URI=https://github.com/ChrisAdkin8/hashicorp-bedrock-ai-rag
   ```

   Optional overrides:

   ```bash
   task up REPO_URI=https://github.com/ChrisAdkin8/hashicorp-bedrock-ai-rag REGION=eu-west-1
   task up REPO_URI=https://github.com/ChrisAdkin8/hashicorp-bedrock-ai-rag SKIP_PIPELINE=true
   ```

   `task up` runs these steps automatically:

   | Step | What happens |
   |---|---|
   | 0 | Preflight checks — tools, auth, Python packages, repo files, Terraform formatting |
   | 1 | S3 state bucket + DynamoDB lock table created (idempotent) |
   | 2 | All AWS infrastructure provisioned via `terraform apply` — IAM, S3, Kendra, CodeBuild, Step Functions, EventBridge |
   | 3 | First pipeline run triggered — CodeBuild ingests docs to S3, Kendra syncs and indexes |

   > **Note:** Kendra index creation takes 10–30 minutes on first deploy. The pipeline run starts automatically once Terraform completes.

5. **Validate retrieval quality**

   ```bash
   task pipeline:test KENDRA_INDEX_ID=<INDEX_ID>
   ```

6. **Measure token efficiency** (optional)

   Compares RAG retrieval token cost against pasting full documentation pages.
   `KENDRA_INDEX_ID` and `REGION` are auto-detected from Terraform output when not provided.

   ```bash
   task pipeline:token-efficiency
   # or explicitly:
   task pipeline:token-efficiency KENDRA_INDEX_ID=<INDEX_ID> REGION=us-east-1
   ```

   Install `tiktoken` for exact token counts: `pip install tiktoken`

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `region` | `us-west-2` | AWS region for all resources |
| `repo_uri` | (required) | GitHub HTTPS URL of this repo — CodeBuild clones it to run pipeline scripts |
| `kendra_edition` | `ENTERPRISE_EDITION` | `DEVELOPER_EDITION` (10 k doc limit, ~$810/mo) or `ENTERPRISE_EDITION` (~$1,400/mo, 100 k docs per SCU). Cannot be changed in-place; requires destroy + recreate. |
| `refresh_schedule` | `cron(0 2 ? * SUN *)` | EventBridge cron expression (UTC) |
| `notification_email` | `""` | Email for CloudWatch alarms (empty = disabled) |

---

## Using the RAG Index with AI Coding Assistants

### Claude Code via MCP Server

The MCP server in `mcp/server.py` exposes the Kendra index as tools that Claude Code calls automatically when answering questions about HashiCorp products.

```bash
task mcp:install                                    # install mcp + boto3 into .venv
task mcp:setup KENDRA_INDEX_ID=<INDEX_ID>           # register with Claude Code, then restart
task mcp:test  KENDRA_INDEX_ID=<INDEX_ID>           # smoke-test retrieval
```

Available tools:

- **`search_hashicorp_docs`** — keyword + semantic search with optional `product_family` and `source_type` filters
- **`get_index_info`** — inspect region, index ID, edition, and status

### Claude Code via Amazon Bedrock

Route Claude Code through your AWS account's Bedrock endpoint:

```bash
task claude:setup                              # default (us-west-2, claude-sonnet-4-20250514)
task claude:setup CLAUDE_REGION=eu-west-2
task claude:setup PERSIST=true                 # persist to ~/.bashrc
```

### Programmatic access (retrieve-then-prompt)

```python
import boto3
import anthropic

# 1. Retrieve context from Kendra
kendra = boto3.client("kendra", region_name="us-east-1")
response = kendra.query(
    IndexId="<KENDRA_INDEX_ID>",
    QueryText="How do I use Vault dynamic secrets with Terraform?",
    PageSize=5,
)
context = "\n\n---\n\n".join(
    item["DocumentExcerpt"]["Text"]
    for item in response.get("ResultItems", [])
)

# 2. Pass context to Claude via Bedrock
bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")
response = bedrock.converse(
    modelId="anthropic.claude-sonnet-4-20250514-v1:0",
    messages=[{
        "role": "user",
        "content": [{"text": f"Context:\n{context}\n\nHow do I use Vault dynamic secrets with Terraform?"}],
    }],
)
print(response["output"]["message"]["content"][0]["text"])
```

---

## Costs

| Component | Notes |
|---|---|
| **Amazon Kendra Enterprise Edition** | ~$1,400/month flat + $35/SCU/month for additional storage. Includes 10,000 queries/day. |
| **Amazon Kendra Developer Edition** | ~$810/month, 10,000 document limit. Suitable for evaluation only. |
| **Amazon Bedrock** | Pay-per-token for Claude inference; negligible for query-time use |
| **S3** | Storage for processed markdown (~GB range); negligible cost |
| **CodeBuild** | Per-build-minute (`BUILD_GENERAL1_MEDIUM`); ~weekly runs |
| **Step Functions** | Per-state-transition; negligible for weekly runs |
| **EventBridge Scheduler** | Negligible |

**Kendra is the dominant cost driver.** Use `DEVELOPER_EDITION` for evaluation and switch to `ENTERPRISE_EDITION` when document volume exceeds 10,000 or query volume exceeds 4,000/day.

---

## How to monitor and troubleshoot

See [docs/RUNBOOK.md](docs/RUNBOOK.md) for the full operational runbook.

Quick links (replace `REGION`):

- Step Functions: `https://console.aws.amazon.com/states/home?region=REGION`
- CodeBuild: `https://console.aws.amazon.com/codesuite/codebuild/projects?region=REGION`
- Kendra: `https://console.aws.amazon.com/kendra/home?region=REGION`
- CloudWatch Logs (CodeBuild): `https://console.aws.amazon.com/cloudwatch/home?region=REGION#logsV2:log-groups/log-group/$252Faws$252Fcodebuild$252Frag-hashicorp-pipeline`

---

## Licence

Apache 2.0 — see [LICENSE](LICENSE).
