# AGENTS.md — Universal AI Operational Guide

This repository provisions a high-precision Amazon Kendra RAG pipeline for the HashiCorp ecosystem (Terraform, Vault, Consul, Nomad, Packer, Boundary).

---

## Quick Commands

| Category | Command |
| :--- | :--- |
| **Deploy** | `task up REPO_URI={url} REGION=us-west-2` |
| **Pipeline (full)** | `task pipeline:run` |
| **Pipeline (targeted)** | `task pipeline:run TARGET=blogs` — `all`, `docs`, `registry`, `discuss`, `blogs` |
| **Validation** | `task pipeline:test KENDRA_INDEX_ID={id}` |
| **Token efficiency** | `task pipeline:token-efficiency` (auto-detects KENDRA_INDEX_ID from Terraform output) |
| **MCP Setup** | `task mcp:setup KENDRA_INDEX_ID={id}` (exposes RAG to Claude Code) |
| **Claude Bedrock** | `task claude:setup` (routes Claude Code through Bedrock) |
| **Terraform** | `task plan` \| `task apply` \| `task validate` |
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
| `terraform/` | All AWS infrastructure (S3, IAM, CodeBuild, Step Functions, EventBridge, Kendra, CloudWatch) |
| `step-functions/rag_pipeline.asl.json` | ASL state machine definition (8 states) |
| `codebuild/buildspec.yml` | CodeBuild build phases — PIPELINE_TARGET gating |
| `codebuild/scripts/` | Data processing scripts (clone, discover, process, fetch, deduplicate, metadata) |
| `scripts/` | Deploy, bootstrap, and operational scripts |
| `mcp/server.py` | MCP server — exposes Kendra index as Claude Code tools |
| `docs/` | Architecture, runbook, MCP guide, diagrams |

---

## Critical Constraints

* **Region**: Kendra is not available in all regions. Supported: `us-east-1`, `us-east-2`, `us-west-2`, `eu-west-1`, `eu-west-2`, `ap-southeast-1`, `ap-southeast-2`, `ap-northeast-1`, `ap-northeast-2`, `ca-central-1`. Bedrock Claude models require `us-west-2` or `us-east-1` for broadest availability.

* **Kendra edition cannot be changed in-place**: Changing `kendra_edition` (DEVELOPER → ENTERPRISE or vice versa) destroys and recreates the Kendra index. Re-run `task pipeline:run` after to re-sync all documents.

* **DEVELOPER_EDITION document limit**: Capped at 10,000 docs. This pipeline typically generates 10,000–30,000+ documents across all source types. Use `ENTERPRISE_EDITION` for production.

* **Bedrock model access**: Must be explicitly enabled per region in the Bedrock console (Model access → Request access for the desired Claude model). Used at query time only — not during ingestion.

* **DynamoDB lock table**: The S3 Terraform backend requires a separate DynamoDB table for state locking. `bootstrap_state.sh` creates it. `task up` handles this automatically.

* **S3 bucket creation in us-east-1**: Must omit `--create-bucket-configuration`. `bootstrap_state.sh` handles this conditionally.

* **`inclusion_patterns` not `exclusion_patterns`**: The Kendra S3 data source must use `inclusion_patterns = ["*.md"]` — using `exclusion_patterns = ["*.metadata.json"]` blocks sidecars from all sync participation and causes `"invalid metadata"` errors.

---

## Maintenance Workflow

1. **Add new content sources**: Edit `codebuild/scripts/clone_repos.sh` (new repo) or create a new fetch script. Commit and push — the next pipeline run picks up changes automatically.

2. **Apply infra changes**: `task plan && task apply`.

3. **Re-sync index**: `task pipeline:run` (full) or `task pipeline:run TARGET=blogs` (targeted).

4. **Validate**: `task pipeline:test KENDRA_INDEX_ID={id}` — verify all 10 topics return results.

5. **Check token efficiency**: `task pipeline:token-efficiency` — compares RAG retrieval token cost against pasting full documentation pages.

---

## Known Gotchas

| Issue | Fix |
| :--- | :--- |
| Kendra metadata `"invalid metadata"` errors | Use `inclusion_patterns = ["*.md"]`, not `exclusion_patterns = ["*.metadata.json"]` |
| `DocumentId` validation failure | Omit `DocumentId` entirely — Kendra auto-assigns from the S3 object key |
| `_source_uri` validation failure | Omit `_source_uri` — Kendra requires HTTP/HTTPS; only `s3://` is available at ingestion time |
| Blog posts not fetched (0 files) | `hashicorp.com` is Cloudflare-protected — extract inline content from `<content>` (Atom) / `<content:encoded>` (RSS) tags |
| `lxml` not installed | `BeautifulSoup(..., "xml")` requires lxml — `lxml>=5.0` is in `requirements.txt` |
| Vault/Consul/Nomad missing from S3 | Individual product repos deprecated their `website/` trees — use `hashicorp/web-unified-docs` with `repo_dir` override in `REPO_CONFIG` |
| Kendra edition change requires destroy | Edition cannot be changed in-place — `terraform destroy` + `terraform apply`, then re-run `task pipeline:run` |
| `kendra_data_source_id` wrong format | `aws_kendra_data_source.s3.id` = `"<data_source_id>/<index_id>"` — use `split("/", ...)[0]` in locals |
| `DEVELOPER_EDITION` document limit | Capped at 10,000 docs — use `ENTERPRISE_EDITION` for production |
| GitHub Issues API rate limit | 60 req/hr unauthenticated — store token in Secrets Manager and uncomment the `secrets-manager` block in `buildspec.yml` |
| YAML parse error in buildspec | Avoid bare `VAR="${VAR:-default}"` as a YAML list item — prefix with `export` so the parser sees a plain string |
