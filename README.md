# aws-hashi-knowledge-base

A production-grade Terraform repository that provisions and operates two
complementary knowledge stores on AWS, both surfaced through a single
MCP server:

1. **HashiCorp Docs Pipeline** — ingests HashiCorp product documentation,
   forum threads, blog posts, and GitHub issues into an **Amazon Kendra**
   index for NLP-powered retrieval-augmented generation. Always on.
2. **Terraform Graph Store** — runs `terraform plan` over your Terraform
   workspaces, extracts the resource dependency graph via
   [rover](https://github.com/im2nguyen/rover), and loads it into an
   **Amazon Neptune** property graph. Opt-in via `create_neptune = true`.

Five MCP tools are exposed through `mcp/server.py`:

| Tool | Backend | Purpose |
|---|---|---|
| `search_hashicorp_docs` | Amazon Kendra | Keyword + semantic search over docs/issues/discuss/blog with deduplication |
| `get_index_info` | Amazon Kendra | Inspect region, Kendra index status, and available metadata filters |
| `get_resource_dependencies` | Amazon Neptune | Walk Terraform dependency graph (upstream/downstream), optionally per repo |
| `find_resources_by_type` | Amazon Neptune | List every resource of a given type, optionally per repo, with limit control |
| `get_graph_info` | Amazon Neptune | Inspect Neptune connectivity, node/edge counts, and status |

Clone it, set a few variables, and run `task up` — a single command
provisions all docs-side infrastructure and ingests the documentation. The
graph store is opt-in: set `create_neptune = true`, supply VPC/subnet IDs,
re-apply, and run `task graph:populate`. CodeBuild clones this repository
directly via its public HTTPS URL — no GitHub App installation, no PATs,
no manual steps.

---

## Architecture

### System Architecture

End-to-end view of both pipelines, from EventBridge triggers through CodeBuild to the Kendra and Neptune backends, with the MCP serving layer that connects them to Claude Code.

![System Architecture](docs/diagrams/architecture.svg)

### Ingestion Pipeline

Detailed flow of the docs ingestion pipeline: trigger and orchestration via Step Functions, parallel git-clone and API-fetch tracks, and post-processing into S3 and Kendra.

![Ingestion Pipeline](docs/diagrams/ingestion_pipeline.svg)

EventBridge Scheduler triggers Step Functions weekly. Step Functions orchestrates CodeBuild (data ingestion), Amazon Kendra (sync and index), and a multi-query retrieval validation step that tests all 7 product families and 3 source types. All infrastructure is provisioned by Terraform. CodeBuild clones this repository directly via its public HTTPS URL — no GitHub connection, trigger, or personal access token is required.

The pipeline runs two parallel tracks inside CodeBuild: a **git clone track** that shallow-clones HashiCorp repos and processes markdown through semantic section splitting and metadata enrichment, and an **API fetch track** that queries the GitHub Issues API, Discourse API, and blog feeds in parallel. Both tracks converge at a single S3 upload step, after which Step Functions calls the Kendra data source sync API to chunk, embed, and index all documents.

### Unified Data Layer

How the MCP server unifies both backends — Kendra for semantic document search and Neptune for graph queries — behind a single tool interface consumed by Claude Code, Bedrock agents, or direct SDK calls.

![Unified Data Layer](docs/diagrams/unified-data-layer.svg)

---

## Data Sources

The pipeline ingests content from seven source types across three collection methods, all fetched automatically during each build:

### Git-cloned sources

These are shallow-cloned from GitHub, then processed into cleaned markdown with enriched metadata headers.

| Source | `source_type` | Repos | What's ingested |
|---|---|---|---|
| **HashiCorp core products** | `documentation` | `hashicorp/web-unified-docs` (Vault, Consul, Nomad, TFE, HCP Terraform), Terraform, Packer, Boundary, Waypoint | Official product documentation from `website/docs/` or `website/content/` directories |
| **Terraform providers** | `provider` | AWS, Azure, GCP, Kubernetes, Helm, Docker, Vault, Consul, Nomad, and more | Resource and data source reference docs with argument/attribute tables and HCL examples |
| **Terraform Registry modules** | `module` | Dynamically discovered via the Registry API | Docs from HashiCorp-verified modules |
| **Sentinel policy libraries** | `sentinel` | Policy definitions and usage documentation |

### API-fetched sources

These run in parallel with the Git clone pipeline, fetched via REST APIs and written directly to the cleaned output directory.

| Source | `source_type` | API | What's ingested |
|---|---|---|---|
| **GitHub Issues** | `issue` | GitHub REST API | Issues updated in the last 365 days from 8 priority repos. Filtered for quality: PRs excluded, minimum body length, minimum comment count. |
| **HashiCorp Discuss** | `discuss` | Discourse JSON API | Forum threads with at least 1 reply from the last 365 days across 9 categories (terraform-core, terraform-providers, vault, consul, nomad, packer, boundary, waypoint, sentinel). |
| **HashiCorp Blog** | `blog` | Atom/RSS feeds (inline content) | Posts from the last 365 days. Full article content extracted via BeautifulSoup. |

### Metadata enrichment

Every document body begins with a compact single-line attribution prefix. This replaces a verbose multi-line YAML header, reducing metadata overhead from ~100 tokens to ~15 tokens per retrieved chunk:

```
[provider:aws] aws_instance — Argument Reference

[discuss:terraform] How do I manage multiple workspaces?

[issue:vault] #1234 (closed): Dynamic secrets not rotating

[blog:terraform] Running Terraform in CI — Setting Up Remote State
```

The full metadata (`product`, `product_family`, `source_type`, `file_name`) is stored separately in `metadata.jsonl` by `generate_metadata.py` and registered with Kendra at sync time — available for filtered retrieval via Kendra attribute filters without consuming body tokens.

| Field | Present in | Description |
|---|---|---|
| `source_type` | all | `documentation`, `provider`, `module`, `sentinel`, `issue`, `discuss`, `blog` |
| `product` | all | Specific product name (e.g. `aws`, `vault`, `terraform`) |
| `product_family` | all | Top-level grouping — `terraform` for all providers/sentinel, product name for core products |
| `repo` | all | Source repository or site name |
| `title` | all | Document or issue title |
| `description` | all | Brief description or issue summary |
| `url` | all | Canonical source URL (GitHub blob, issue, Discuss thread, or blog post) |
| `doc_category` | docs | `resource-reference`, `data-source-reference`, `guide`, `cli-reference`, `api-reference`, `getting-started`, `internals`, `upgrade-guide`, `configuration`, `documentation` |
| `resource_type` | providers | Terraform resource/data source name (e.g. `aws_instance`, `google_compute_network`) |
| `section_title` | docs | Heading text for semantically split document sections |
| `has_accepted_answer` | discuss | `true` if the thread has a community-accepted answer |

---

## Prerequisites

- **AWS account** with billing enabled
- **AWS CLI** installed and credentials configured — environment variables, `aws configure`, or AWS SSO
- **Terraform** >= 1.10
- **Python** 3.11+
- **Task** ([taskfile.dev](https://taskfile.dev)) — `brew install go-task` or `go install github.com/go-task/task/v3/cmd/task@latest`
- **Python packages** — `pip install boto3 pyyaml requests pytest beautifulsoup4 mcp`
- **shellcheck** — `brew install shellcheck` (used by preflight checks)
- **jq** — `brew install jq` (used by preflight checks)
- **Amazon Bedrock model access** — enable Claude (e.g. `claude-sonnet-4-20250514`) in Bedrock console -> Model access for AI inference via the MCP server
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

4. **Run preflight checks** (optional — `task up` runs these automatically)

   ```bash
   task preflight
   ```

   Validates CLI tools (terraform, aws, python3, jq, shellcheck), version requirements (Terraform >= 1.10, Python >= 3.11), AWS authentication and account access, Python packages, repository file integrity, and Terraform formatting/validation.

5. **Deploy everything with one command**

   ```bash
   task up REPO_URI=https://github.com/ChrisAdkin8/aws-hashi-knowledge-base
   ```

   You can override the auto-detected region if needed:

   ```bash
   task up REPO_URI=https://github.com/ChrisAdkin8/aws-hashi-knowledge-base REGION=eu-west-1
   ```

   `task up` runs four steps automatically:

   | Step | What happens |
   |------|-------------|
   | 0 | Preflight checks — tools, auth, Python packages, repo files, Terraform validation |
   | 1 | Bootstrap: create remote state S3 bucket (`terraform/bootstrap`) |
   | 2 | All AWS infrastructure provisioned (`terraform apply`) — IAM, S3, Kendra, CodeBuild, Step Functions, EventBridge, Neptune (if `create_neptune=true`) |
   | 3 | First pipeline run triggered — CodeBuild ingests docs to S3, Kendra syncs and indexes |

   Optional overrides:

   ```bash
   task up REPO_URI=https://github.com/... SKIP_PIPELINE=true
   ```

   > **Note:** Kendra index creation takes 10-30 minutes on first deploy.

   Each step is idempotent — re-running `task up` safely skips completed steps.

6. **Validate retrieval quality**

   ```bash
   task docs:test
   ```

7. **(Optional) Enable the graph store**

   The graph store is opt-in and provisions a Neptune cluster (~$0.20-$0.35/hr
   per `db.r6g.large` instance). To enable:

   ```hcl
   # terraform/terraform.tfvars
   create_neptune = true
   neptune_vpc_id = "vpc-xxx"
   neptune_subnet_ids = ["subnet-aaa", "subnet-bbb"]
   graph_repo_uris = [
     "https://github.com/my-org/my-tf-workspace",
   ]
   ```

   Then:

   ```bash
   task apply                        # provisions Neptune + workflow + scheduler
   task graph:populate               # one-off run; or wait for the weekly cron
   task graph:test                   # smoke-test that nodes/edges are loaded
   task mcp:test                     # exercises the new graph tools alongside docs
   ```

### Optional: customise before deploying

If you want to change notification email, refresh schedule, or other settings before the first apply, copy the example vars file and edit it:

```bash
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
# Edit terraform/terraform.tfvars, then run task up as above.
# task up will detect the existing file and not overwrite it.
```

---

## Configuration

### Always-on (docs pipeline)

| Variable | Type | Default | Description |
|---|---|---|---|
| `region` | string | `"us-east-1"` | AWS region for all resources |
| `repo_uri` | string | (required) | GitHub HTTPS URL of this repo — CodeBuild clones it to run pipeline scripts |
| `kendra_edition` | string | `"ENTERPRISE_EDITION"` | `DEVELOPER_EDITION` (~$810/mo, 10k docs) or `ENTERPRISE_EDITION` (~$1,400/mo, 100k docs/SCU). Cannot change in-place. |
| `refresh_schedule` | string | `"cron(0 2 ? * SUN *)"` | EventBridge cron expression (UTC) for the docs pipeline |
| `scheduler_timezone` | string | `"Europe/London"` | Timezone for the EventBridge Scheduler |
| `notification_email` | string | `""` | Email for CloudWatch alarms (empty = disabled) |
| `create_github_oidc_provider` | bool | `false` | Create GitHub Actions OIDC provider + IAM role for CI/CD |
| `force_destroy` | bool | `false` | Allow S3 bucket destruction even if non-empty (non-prod only) |
| `tags` | map(string) | `{}` | Additional tags applied to all resources |

### Optional (graph pipeline — `create_neptune = true`)

| Variable | Type | Default | Description |
|---|---|---|---|
| `create_neptune` | bool | `false` | Master switch for the graph pipeline |
| `neptune_vpc_id` | string | `""` | VPC ID for the Neptune cluster |
| `neptune_subnet_ids` | list(string) | `[]` | Subnet IDs for the Neptune subnet group |
| `neptune_allowed_cidr_blocks` | list(string) | `[]` | CIDR blocks permitted to reach Neptune on port 8182 |
| `neptune_cluster_identifier` | string | `"hashicorp-rag-graph"` | Neptune cluster identifier |
| `neptune_instance_class` | string | `"db.r6g.large"` | Neptune instance class |
| `neptune_instance_count` | number | `1` | Number of Neptune instances (1 = writer only) |
| `neptune_iam_auth_enabled` | bool | `true` | Enable IAM authentication for Neptune |
| `neptune_deletion_protection` | bool | `true` | Prevent cluster deletion via Terraform |
| `neptune_backup_retention_days` | number | `7` | Automated backup retention (days) |
| `graph_repo_uris` | list(string) | `[]` | GitHub HTTPS URLs of Terraform workspace repos to ingest into Neptune |
| `graph_refresh_schedule` | string | `"cron(0 3 ? * SUN *)"` | EventBridge cron for the graph pipeline (UTC) |
| `graph_codebuild_compute_type` | string | `"BUILD_GENERAL1_MEDIUM"` | CodeBuild compute type for graph pipeline |
| `neptune_create_nat_gateway` | bool | `false` | Create a NAT gateway so VPC-attached CodeBuild can reach the internet |
| `neptune_codebuild_subnet_cidr` | string | `"172.31.64.0/24"` | CIDR for the private CodeBuild subnet created when `neptune_create_nat_gateway = true` |
| `neptune_create_proxy` | bool | `false` | Create an API Gateway + Lambda proxy for Neptune queries from outside the VPC |

---

## Using the RAG Index with AI Coding Assistants

### Claude Code via MCP Server

The MCP server in `mcp/server.py` exposes the Kendra index and Neptune graph as tools that Claude Code calls automatically.

```bash
task mcp:install    # install mcp + boto3 + requests into .venv
task mcp:setup      # register with Claude Code (auto-detects IDs from Terraform), then restart
task mcp:test       # smoke-test retrieval (Kendra + Neptune if deployed)
```

Available tools:

- **`search_hashicorp_docs`** — keyword + semantic search with optional `product`, `product_family`, and `source_type` filters; URI and content deduplication
- **`get_index_info`** — inspect region, Kendra index status, and available metadata filters
- **`get_resource_dependencies`** — traverse Terraform resource dependency graph (downstream, upstream, or both), optionally filtered by repository
- **`find_resources_by_type`** — list all resources of a given type, optionally filtered by repository, with configurable limit (1-500)
- **`get_graph_info`** — inspect Neptune connectivity, node/edge counts, and repository count

### Claude Code via Amazon Bedrock

The `task claude:setup` command configures Claude Code to route requests through your AWS account's Bedrock endpoint instead of the Anthropic API directly. This keeps all traffic within your cloud environment — the same account that hosts the Kendra index.

The task runs `scripts/setup_claude_bedrock.sh`, which performs four steps:

1. **Verifies AWS credentials** — confirms `aws sts get-caller-identity` succeeds
2. **Sets environment variables** — exports `CLAUDE_CODE_USE_BEDROCK=1`, `ANTHROPIC_BEDROCK_REGION`, and `ANTHROPIC_MODEL` in the current shell
3. **Optionally persists** — with `PERSIST=true`, appends the exports to `~/.bashrc` (idempotent — checks for an existing marker before writing)
4. **Verifies the setup** — confirms the Bedrock API is accessible and the `claude` CLI is available

```bash
# Default setup (us-east-1, claude-sonnet-4-20250514)
task claude:setup

# Override region or model
task claude:setup CLAUDE_REGION=eu-west-2
task claude:setup CLAUDE_MODEL=claude-sonnet-4-20250514

# Persist to ~/.bashrc so future shells inherit the config
task claude:setup PERSIST=true
```

After running the task, start Claude Code with `claude` in the same shell. To revert to the Anthropic API:

```bash
unset CLAUDE_CODE_USE_BEDROCK ANTHROPIC_BEDROCK_REGION ANTHROPIC_MODEL
```

### Retrieve-then-prompt (programmatic)

For programmatic use, Claude does not have native Kendra integration. Use a retrieve-then-prompt pattern — query the index first, then pass the results as context:

```python
import boto3

region = "us-east-1"

# 1. Retrieve relevant context from Kendra.
kendra = boto3.client("kendra", region_name=region)
response = kendra.query(
    IndexId="<KENDRA_INDEX_ID>",
    QueryText="How do I use Vault dynamic secrets with Terraform?",
    PageSize=5,
)
context = "\n\n---\n\n".join(
    item["DocumentExcerpt"]["Text"]
    for item in response.get("ResultItems", [])
)

# 2. Pass context to Claude via Bedrock.
bedrock = boto3.client("bedrock-runtime", region_name=region)
response = bedrock.converse(
    modelId="anthropic.claude-sonnet-4-20250514-v1:0",
    messages=[{
        "role": "user",
        "content": [{"text": (
            f"Using the following HashiCorp documentation as context:\n\n"
            f"{context}\n\n---\n\n"
            f"How do I use Vault dynamic secrets with Terraform?"
        )}],
    }],
)
print(response["output"]["message"]["content"][0]["text"])
```

### MCP Server (any assistant)

For assistants that support the Model Context Protocol (MCP), you can build a lightweight MCP server that exposes the index as a tool. The server handles the Kendra retrieval call and returns results in MCP format, making the index available to any MCP-compatible client (Claude Code, VS Code Copilot with MCP, etc.).

---

## Chunking Strategy — Why Semantic Chunking Matters

RAG systems work by splitting documents into chunks, embedding each chunk as a vector, and retrieving the most similar chunks at query time. The quality of those chunks directly determines the quality of the answers.

### The problem with naive chunking

Most RAG tutorials demonstrate fixed-length chunking — splitting every N tokens regardless of content structure. For technical documentation, this produces poor results:

| Scenario | What happens | Impact |
|---|---|---|
| Code block split mid-example | A Terraform `resource` block is cut after the opening `{` | The embedding captures half a config — matches irrelevant queries about syntax errors |
| Heading separated from content | `## Argument Reference` ends up in one chunk, the argument table in the next | A query for "aws_instance arguments" matches the heading chunk (no useful content) instead of the table |
| Context loss at boundaries | A sentence referencing "the resource above" starts a new chunk | The embedding has no referent — it matches nothing useful |

### How this pipeline solves it: semantic pre-splitting

Documents are split at `##` and `###` heading boundaries before upload — by `process_docs.py` for docs, providers, modules, and sentinel content, and by `fetch_blogs.py` for blog posts. Kendra then applies its own chunking during sync. The pre-splitting ensures that chunk windows land on structural boundaries rather than mid-sentence or mid-code-block.

- Sections smaller than 200 characters are merged with the previous section to avoid tiny fragments
- Sections larger than 2,000 characters are split at code-fence boundaries to keep sections within reasonable chunk windows
- Code blocks are compressed before splitting (comments stripped, blank lines collapsed) to reduce per-chunk token count
- Single-section documents preserve their original path structure
- Multi-section documents are written as `{stem}_s0.md`, `{stem}_s1.md`, etc.
- Each section carries a `section_title` value embedded in its compact body prefix

This approach means chunks align with natural document structure — "Argument Reference", "Example Usage", "Import" each become their own embedding. A query for "aws_instance arguments" retrieves the argument table chunk, not an arbitrary fixed-length window that starts mid-table and ends mid-example.

---

## Advanced Pipeline Features

The pipeline includes several features designed to maximise the quality and relevance of the RAG index. These go beyond naive "dump docs into a vector store" approaches.

### Product Taxonomy Normalisation

**Problem:** Without a consistent taxonomy, the same concept gets different product labels across sources. An AWS provider question on Discuss might be tagged `product: terraform` (from the `terraform-providers` category), while the same content in docs is `product: aws`. Blog posts default to `product: hashicorp` regardless of topic.

**Solution:** Every document now carries two fields:
- `product` — the most specific identifier (e.g. `aws`, `vault`, `consul`)
- `product_family` — the top-level grouping (e.g. `terraform` for all providers and sentinel, `vault` for core Vault)

Blog posts detect the product family by scanning the title and body for product keywords. Discuss threads derive it from their category slug.

**Why it matters:** A query about "Terraform AWS provider" can now match across docs, issues, and Discuss threads using `product_family: terraform`, even though each source uses a different `product` value. This eliminates the retrieval silo problem where community Q&A never surfaces alongside official docs.

### Accepted-Answer Prioritisation

**Problem:** In Discuss threads, replies are stored chronologically. The accepted answer (the community-verified resolution) might be reply #4, buried after three earlier attempts. When the chunker truncates the thread, the answer is lost.

**Solution:** `fetch_discuss.py` detects accepted answers and reorders them to appear immediately after the question, under a dedicated `## Accepted Answer` heading. The `has_accepted_answer` metadata field flags these threads.

**Why it matters:** The highest-value content in a Q&A thread is the resolution, not the initial speculation. By front-loading it, the answer falls within the same chunk as the question — exactly the shape that RAG retrieval optimises for.

### URL Attribution

**Problem:** When a RAG system returns a chunk, the user has no way to verify the source or read the full document. This is a trust problem — users cannot distinguish hallucinated content from grounded retrieval.

**Solution:** Every document includes a `url` metadata field pointing to its canonical source:
- Docs: GitHub blob URL (e.g. `https://github.com/hashicorp/terraform-provider-aws/blob/main/website/docs/r/instance.html.markdown`)
- Issues: GitHub issue URL
- Discuss: Thread URL (`https://discuss.hashicorp.com/t/{id}`)
- Blogs: Original post URL

**Why it matters:** The LLM can cite sources in its response, and users can click through to the full context. This turns the RAG system from a black box into a verifiable reference tool.

### Resource Type Extraction

**Problem:** Provider docs follow a predictable structure (`r/instance.html.markdown` for the `aws_instance` resource), but this structure is lost during processing. A query for "aws_instance" has to rely on embedding similarity alone.

**Solution:** `process_docs.py` extracts the Terraform resource or data source name from the file path and includes it as a `resource_type` metadata field (e.g. `aws_instance`, `google_compute_network`). The `doc_category` field (`resource-reference` or `data-source-reference`) provides additional signal.

**Why it matters:** Exact-match metadata filtering is far more precise than embedding similarity for structured queries. A query for "aws_instance arguments" can filter to `resource_type: aws_instance` before doing vector search, eliminating false positives from similarly-named resources.

### HTML-to-Markdown Fidelity

**Problem:** Discourse forum posts are stored as HTML. A naive regex-based tag stripper loses tables, blockquotes (commonly used for error messages), nested lists, and heading structure — all of which carry semantic meaning.

**Solution:** Both `fetch_discuss.py` and `fetch_blogs.py` use BeautifulSoup for HTML-to-markdown conversion. This preserves:
- Code blocks (fenced with triple backticks)
- Tables (converted to markdown pipe syntax)
- Blockquotes (prefixed with `>`)
- Links (converted to `[text](url)` format)
- Headings (converted to `#` syntax)
- Nested lists

**Why it matters:** A Discuss answer that includes a comparison table or a multi-step code solution retains its structure. The embedding model can represent "step 1, step 2, step 3" as a procedural answer rather than a blob of concatenated text.

---

## Demonstrating Token Efficiency: RAG vs Raw Sources

A key benefit of this RAG index is token efficiency. Instead of pasting entire documentation pages, README files, or GitHub issue threads into an LLM's context window, you retrieve only the relevant chunks. This section walks through a concrete comparison.

### Setup

```bash
# Ensure the index is populated
task docs:test

# Install the token counting dependency
pip install tiktoken
```

### Step 1: Measure the raw source approach

Pick a question you'd normally answer by reading HashiCorp docs — for example, "How do I configure an S3 backend in Terraform?"

Without RAG, you'd need to provide context manually. Measure the token cost of the raw source material:

```python
import tiktoken

enc = tiktoken.encoding_for_model("gpt-4o")  # cl100k_base, similar to Claude's tokeniser

# Simulate pasting the full S3 backend documentation page (~3,500 words)
with open("raw_s3_backend_docs.md") as f:
    raw_text = f.read()

raw_tokens = len(enc.encode(raw_text))
print(f"Raw source: {raw_tokens:,} tokens")
```

For a typical Terraform backend configuration question, the raw source material (the full `s3` backend docs page, plus related pages on state locking and workspaces) is **8,000-12,000 tokens**.

### Step 2: Measure the RAG approach

Query the index for the same question and measure the retrieved context:

```python
import boto3
import tiktoken

enc = tiktoken.encoding_for_model("gpt-4o")

kendra = boto3.client("kendra", region_name="us-east-1")
response = kendra.query(
    IndexId="<KENDRA_INDEX_ID>",
    QueryText="How do I configure an S3 backend in Terraform?",
    PageSize=3,
)

rag_context = "\n\n---\n\n".join(
    item["DocumentExcerpt"]["Text"]
    for item in response.get("ResultItems", [])
)

rag_tokens = len(enc.encode(rag_context))
print(f"RAG context: {rag_tokens:,} tokens")
print(f"Results retrieved: {len(response.get('ResultItems', []))}")
```

With `PageSize=3` and semantically-split section chunks, the retrieved context is typically **900-2,000 tokens** — each chunk is a complete document section that directly addresses the query, with no token budget wasted on adjacent sections or metadata boilerplate.

### Step 3: Compare

| Approach | Tokens | Content quality |
|---|---|---|
| Raw docs (full pages) | 8,000-12,000 | Includes navigation boilerplate, unrelated sections, version history |
| RAG retrieval (top 3 results) | 900-2,000 | Only the relevant sections: configuration block, required arguments, example HCL |

**Result:** The RAG approach uses **80-90% fewer tokens** while providing more focused context. This translates directly to:

- **Lower cost** — fewer input tokens per API call
- **Faster responses** — less context for the model to process
- **Better answers** — the model's attention is focused on relevant content rather than diluted across entire pages
- **Longer conversations** — more of the context window is available for conversation history and reasoning

The efficiency gain compounds when answering questions that span multiple products. A question like "How do I use Vault dynamic secrets with the Terraform AWS provider?" would require pasting docs from three separate sources (~25,000 tokens raw). The RAG index retrieves the 5 most relevant results across all sources in ~3,000 tokens.

### Benchmark results across query types

The table below shows measured token counts for ten representative queries — a mix of single-product lookups and cross-product questions that require spanning multiple documentation sources.

| Query | RAG tokens | Raw tokens | Saving |
|---|---|---|---|
| S3 backend configuration | — | 9,500 | — |
| AWS provider setup | — | 11,000 | — |
| Vault dynamic secrets | — | 14,000 | — |
| Consul service mesh | — | 16,000 | — |
| Packer AMI builds | — | 8,500 | — |
| Cross-product: Vault + AWS provider | — | 22,000 | — |
| Nomad job scheduling | — | 12,000 | — |
| Sentinel policy enforcement | — | 13,500 | — |
| Terraform module composition | — | 10,000 | — |
| Cross-product: Consul + Vault | — | 19,500 | — |

> **Note:** RAG token and saving columns show `—` until you run `task test:token-efficiency MODE=kendra` against a live environment. Raw token estimates are based on the equivalent manual sources.

### Combined queries (RAG + Graph)

Some questions require both documentation context (from the Kendra index) and infrastructure structure (from the Neptune graph store). The `combined` mode runs queries that neither backend can fully answer alone — for example, auditing deployed IAM roles against HashiCorp least-privilege guidance, or verifying resource configuration against Terraform best practices.

Each combined query issues a Kendra retrieval for documentation and a Neptune graph lookup for structural data. The output shows a per-source token breakdown:

| Query | RAG tokens | Graph tokens | Total | Raw tokens | Saving |
|---|---|---|---|---|---|
| IAM roles vs least-privilege guidance | — | — | — | 18,000 | — |
| Service account security posture | — | — | — | 15,000 | — |
| CI/CD pipeline structure and configuration | — | — | — | 17,000 | — |
| Neptune deployment vs Terraform guidance | — | — | — | 14,000 | — |
| Workflow orchestration design and implementation | — | — | — | 16,000 | — |
| State backend storage and bucket configuration | — | — | — | 15,500 | — |
| Scheduler-driven workflow orchestration patterns | — | — | — | 14,500 | — |
| SNS event-driven architecture | — | — | — | 13,000 | — |
| Vault-managed secrets for AWS IAM | — | — | — | 16,500 | — |
| RAG index ingestion and Kendra configuration | — | — | — | 19,000 | — |

> **Note:** RAG, graph, and total token columns show `—` until you run `task test:token-efficiency MODE=combined` against a live environment. Raw token estimates are based on the equivalent manual sources (documentation pages + `.tf` file grepping + `terraform graph` output).

Run the full benchmark:

```bash
# Kendra-only mode
python3 scripts/test_token_efficiency.py \
    --region <REGION> \
    --kendra-index-id <KENDRA_INDEX_ID> \
    --mode kendra

# Combined mode (requires both Kendra and Neptune)
python3 scripts/test_token_efficiency.py \
    --region <REGION> \
    --kendra-index-id <KENDRA_INDEX_ID> \
    --neptune-endpoint <ENDPOINT> --neptune-port 8182 \
    --mode combined

# All modes (kendra + graph + combined + overall summary)
python3 scripts/test_token_efficiency.py \
    --region <REGION> \
    --kendra-index-id <KENDRA_INDEX_ID> \
    --neptune-endpoint <ENDPOINT> --neptune-port 8182 \
    --mode all

# Add --verbose for per-query detail (rows/chunks, token breakdowns, raw sources)
python3 scripts/test_token_efficiency.py \
    --region <REGION> \
    --kendra-index-id <KENDRA_INDEX_ID> \
    --neptune-endpoint <ENDPOINT> --neptune-port 8182 \
    --mode all --verbose

# Or via Taskfile
task test:token-efficiency MODE=all VERBOSE=true
```

---

## How to add new providers or modules

### Add a new Terraform provider

1. Open `codebuild/scripts/clone_repos.sh`.
2. Add an entry to the `PROVIDER_REPOS` associative array:
   ```bash
   ["terraform-provider-<NAME>"]="https://github.com/hashicorp/terraform-provider-<NAME>.git"
   ```
3. Commit and push. The next scheduled pipeline run (or a manual trigger) will ingest the new docs.

### Add a new HashiCorp product repo

1. Open `codebuild/scripts/clone_repos.sh`.
2. Add an entry to the `CORE_REPOS` associative array.
3. Open `codebuild/scripts/process_docs.py`.
4. Add an entry to the `REPO_CONFIG` dict, specifying `docs_subdir`, `source_type`, and `product`.

---

## How to monitor and troubleshoot

See [docs/RUNBOOK.md](docs/RUNBOOK.md) for the full operational runbook.

**Quick links (replace `REGION` with your deployment region):**

- Step Functions: `https://console.aws.amazon.com/states/home?region=REGION`
- CodeBuild: `https://console.aws.amazon.com/codesuite/codebuild/projects?region=REGION`
- Kendra: `https://console.aws.amazon.com/kendra/home?region=REGION`
- Neptune: `https://console.aws.amazon.com/neptune/home?region=REGION`
- CloudWatch Logs (CodeBuild): `https://console.aws.amazon.com/cloudwatch/home?region=REGION#logsV2:log-groups/log-group/$252Faws$252Fcodebuild$252Frag-hashicorp-pipeline`

---

## Task Reference

| Task | Description |
|---|---|
| `task up` | Full deploy: preflight -> bootstrap -> apply -> populate Kendra + Neptune |
| `task down` | Alias for `task destroy` |
| `task destroy` | Destroy all Terraform-managed infrastructure |
| `task login` | Verify AWS credentials and print caller identity |
| `task bootstrap` | Create S3 state bucket via `terraform/bootstrap` (idempotent) |
| `task init` | Initialise Terraform with remote S3 backend (skipped if `.terraform.lock.hcl` exists) |
| `task init:upgrade` | Re-initialise Terraform with `-upgrade` (refresh providers/modules) |
| `task fmt` | Format all Terraform files |
| `task fmt:check` | Check Terraform formatting (CI-friendly, no writes) |
| `task validate` | Validate Terraform configuration |
| `task plan` | Plan and save to `tfplan` |
| `task apply` | Plan then apply (depends on `plan`, prompts for confirmation) |
| `task output` | Print all Terraform outputs |
| `task oidc:import` | Import an existing GitHub OIDC provider into Terraform state |
| `task preflight` | Run all preflight checks (tools, auth, packages, files, Terraform) |
| `task docs:run` | Trigger a docs pipeline run and wait for completion (`TARGET`: all/docs/registry/discuss/blogs) |
| `task docs:test` | Run retrieval validation queries against Kendra |
| `task docs:status` | List last 5 docs pipeline executions |
| `task bucket:report` | File counts per subfolder in RAG docs S3 bucket |
| `task test:token-efficiency` | Compare token cost across backends (`MODE`: kendra/graph/combined/all) |
| `task graph:populate` | Trigger a graph pipeline run and wait for completion |
| `task graph:status` | List last 5 graph pipeline executions |
| `task graph:test` | Validate Neptune graph has nodes and edges |
| `task mcp:install` | Install MCP server dependencies |
| `task mcp:setup` | Register MCP server with Claude Code (auto-detects IDs from Terraform) |
| `task mcp:test` | Smoke-test MCP server tools |
| `task claude:setup` | Configure Claude Code to use Amazon Bedrock |
| `task ci` | All CI checks (parallel: fmt:check + validate + shellcheck + test) |
| `task shellcheck` | Lint all shell scripts |
| `task test` | Run Python unit tests (pytest) |

---

## Costs

| Component | Notes |
|---|---|
| **Amazon Kendra Enterprise Edition** | ~$1,400/month flat + $35/SCU/month for additional storage. Includes 10,000 queries/day. |
| **Amazon Kendra Developer Edition** | ~$810/month, 10,000 document limit. Suitable for evaluation only. |
| **Amazon Neptune** | ~$0.20-$0.35/hour per instance (`db.r6g.large`). Optional — only deployed when `create_neptune = true`. |
| **Neptune Proxy (API Gateway + Lambda)** | API Gateway: per-request pricing; Lambda: per-invocation + compute. Optional — deployed when `neptune_create_proxy = true`. Negligible for MCP query volumes. |
| **Amazon Bedrock** | Pay-per-token for Claude inference; negligible for query-time use |
| **S3** | Storage for processed markdown and graph staging; negligible cost |
| **CodeBuild** | Per-build-minute (`BUILD_GENERAL1_MEDIUM`); ~weekly runs |
| **Step Functions** | Per-state-transition; negligible for weekly runs |
| **EventBridge Scheduler** | Negligible |

**Kendra is the dominant cost driver.** Use `DEVELOPER_EDITION` for evaluation and switch to `ENTERPRISE_EDITION` when document volume exceeds 10,000 or query volume exceeds 4,000/day.

---

## Licence

Apache 2.0 — see [LICENSE](LICENSE).
