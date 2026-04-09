# AGENTS.md — Universal AI Operational Guide

This repository provisions a high-precision Amazon Kendra RAG pipeline for the HashiCorp ecosystem (Terraform, Vault, Consul, Nomad, Packer, Boundary).

---

## Quick Commands

| Category | Command |
| :--- | :--- |
| **Deploy** | `task up REPO_URI={url}` — REGION auto-detected from `terraform.tfvars` |
| **Docs (full)** | `task docs:run` |
| **Docs (targeted)** | `task docs:run TARGET=blogs` — `all`, `docs`, `registry`, `discuss`, `blogs` |
| **Docs status** | `task docs:status` |
| **Docs test** | `task docs:test` |
| **Token efficiency** | `task test:token-efficiency` |
| **Graph populate** | `task graph:populate GRAPH_REPO_URIS="https://github.com/org/repo"` |
| **Graph status** | `task graph:status` |
| **Graph test** | `task graph:test` |
| **MCP Setup** | `task mcp:setup` (auto-detects IDs from Terraform output) |
| **Claude Bedrock** | `task claude:setup` (routes Claude Code through Bedrock) |
| **Terraform** | `task plan` \| `task apply` \| `task validate` \| `task destroy` |
| **CI** | `task ci` (fmt:check + validate + shellcheck + tests) |

---

## Architectural Pillars

* **Single-apply deployment**: All infrastructure — IAM, S3, Kendra, CodeBuild, Step Functions, EventBridge — is provisioned in a single `terraform apply`. No two-step bootstrapping required.

* **Step Functions orchestration**: The state machine uses `.sync` integration for CodeBuild (no polling loop — Step Functions uses CloudWatch Events to detect build completion). Kendra sync uses a manual poll loop (`WaitForSync` → `ListSyncJobs` → `CheckSyncStatus`) because `kendra:startDataSourceSyncJob` has no `.sync` integration.

* **Semantic Pre-splitting**: `process_docs.py` splits docs at `##`/`###` heading boundaries before upload. Sections under 200 chars are merged into the previous section; sections over ~4,000 chars are split at code-fence boundaries. Kendra then applies its own NLP-powered passage extraction — no chunking configuration required.

* **Metadata Engine**: `generate_metadata.py` produces `.metadata.json` sidecar files next to every document. These are uploaded to S3 alongside the markdown and read by Kendra at sync time. Attributes (`product`, `product_family`, `source_type`) are indexed for faceted filtering in the MCP server.

* **Cross-Source Deduplication**: `deduplicate.py` removes near-duplicate files by SHA-256 of normalised body content before upload. Prevents the same content entering through multiple sources.

* **Sequential Validation**: The `ValidateRetrieval` state uses a Step Functions `Map` state with `MaxConcurrency: 1` to run 10 test queries covering all product families sequentially. Sequential execution avoids Kendra query throttling. Zero results log a warning but do NOT fail the pipeline.

* **Targeted Pipeline Runs**: The `PIPELINE_TARGET` environment variable (set in Step Functions input and passed to CodeBuild) controls which content sources are ingested. Each CodeBuild phase gates its steps on this variable, enabling partial re-ingestion without a full rebuild.

---

## Project Structure

| Path | Purpose |
| :--- | :--- |
| `terraform/` | All AWS infrastructure — Kendra, Neptune, CodeBuild, Step Functions, EventBridge, S3, IAM |
| `terraform/modules/hashicorp-docs-pipeline/` | Kendra RAG pipeline module |
| `terraform/modules/terraform-graph-store/` | Neptune graph pipeline module (opt-in: `create_neptune = true`) |
| `terraform/bootstrap/` | State bucket bootstrap (runs before main module) |
| `step-functions/rag_pipeline.asl.json` | Docs pipeline ASL state machine (8 states) |
| `step-functions/graph_pipeline.asl.json` | Graph pipeline ASL state machine (Map over repos) |
| `codebuild/buildspec.yml` | Docs pipeline CodeBuild phases — PIPELINE_TARGET gating |
| `codebuild/buildspec_graph.yml` | Graph pipeline CodeBuild phases (terraform plan → rover → ingest) |
| `codebuild/scripts/` | Data processing scripts (clone, discover, process, fetch, deduplicate, metadata) |
| `scripts/` | Deploy, bootstrap, and operational scripts |
| `mcp/server.py` | MCP server — exposes Kendra index and Neptune graph as Claude Code tools |
| `docs/` | Architecture, runbook, MCP guide, diagrams |

---

## Critical Constraints

* **Region**: Kendra is not available in all regions. Supported: `us-east-1`, `us-east-2`, `us-west-2`, `eu-west-1`, `eu-west-2`, `ap-southeast-1`, `ap-southeast-2`, `ap-northeast-1`, `ap-northeast-2`, `ca-central-1`. Bedrock Claude models require `us-west-2` or `us-east-1` for broadest availability.

* **Kendra edition cannot be changed in-place**: Changing `kendra_edition` (DEVELOPER → ENTERPRISE or vice versa) destroys and recreates the Kendra index. Re-run `task docs:run` after to re-sync all documents.

* **DEVELOPER_EDITION document limit**: Capped at 10,000 docs. This pipeline typically generates 10,000–30,000+ documents across all source types. Use `ENTERPRISE_EDITION` for production.

* **Bedrock model access**: Must be explicitly enabled per region in the Bedrock console (Model access → Request access for the desired Claude model). Used at query time only — not during ingestion.

* **Neptune is opt-in**: Set `create_neptune = true` in `terraform/terraform.tfvars` and supply `neptune_vpc_id` and `neptune_subnet_ids`. Without this, `task graph:populate` will fail with `graph_state_machine_arn not found`.

* **Neptune proxy is opt-in**: Set `neptune_create_proxy = true` to deploy an API Gateway + Lambda proxy for Neptune access from outside the VPC. The MCP server uses `NEPTUNE_PROXY_URL` to route through the proxy instead of connecting directly.

* **`s3_configuration` with `inclusion_patterns`**: The Kendra S3 data source uses `s3_configuration` with `inclusion_patterns = ["**/*.md"]`. Do not use `exclusion_patterns` — it blocks `.metadata.json` sidecars from sync participation and causes `"invalid metadata"` errors. Do not use `template_configuration` — it is invalid for S3 type and fails with `S3ConnectorConfiguration` error.

---

## Maintenance Workflow

1. **Add new content sources**: Edit `codebuild/scripts/clone_repos.sh` (new repo) or create a new fetch script. Commit and push — the next pipeline run picks up changes automatically.

2. **Apply infra changes**: `task plan && task apply`.

3. **Re-sync index**: `task docs:run` (full) or `task docs:run TARGET=blogs` (targeted).

4. **Validate**: `task docs:test` — verify all 10 topics return results.

5. **Check token efficiency**: `task test:token-efficiency` — compares RAG retrieval token cost against pasting full documentation pages.

---

## Known Gotchas

| Issue | Fix |
| :--- | :--- |
| Kendra metadata `"invalid metadata"` errors | Use `s3_configuration` with `inclusion_patterns = ["**/*.md"]`, not `exclusion_patterns`. Do not use `template_configuration` — it is invalid for S3 type. See `kendra.tf` |
| `DocumentId` validation failure | Omit `DocumentId` entirely — Kendra auto-assigns from the S3 object key |
| `_source_uri` validation failure | Omit `_source_uri` — Kendra requires HTTP/HTTPS; only `s3://` is available at ingestion time |
| Blog posts not fetched (0 files) | `hashicorp.com` is Cloudflare-protected — extract inline content from `<content>` (Atom) / `<content:encoded>` (RSS) tags |
| `lxml` not installed | `BeautifulSoup(..., "xml")` requires lxml — `lxml>=5.0` is in `requirements.txt` |
| Vault/Consul/Nomad missing from S3 | Individual product repos deprecated their `website/` trees — use `hashicorp/web-unified-docs` with `repo_dir` override in `REPO_CONFIG` |
| Kendra edition change requires destroy | Edition cannot be changed in-place — `terraform destroy` + `terraform apply`, then re-run `task docs:run` |
| `kendra_data_source_id` wrong format | `aws_kendra_data_source.s3.id` = `"<data_source_id>/<index_id>"` — use `split("/", ...)[0]` in locals |
| `DEVELOPER_EDITION` document limit | Capped at 10,000 docs — use `ENTERPRISE_EDITION` for production |
| GitHub Issues API rate limit | 60 req/hr unauthenticated — store token in Secrets Manager and uncomment the `secrets-manager` block in `buildspec.yml` |
| YAML parse error in buildspec | Avoid bare `VAR="${VAR:-default}"` as a YAML list item — prefix with `export` so the parser sees a plain string |
