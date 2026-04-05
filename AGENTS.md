# AGENTS.md — Universal AI Operational Guide

This repository provisions a high-precision Amazon Bedrock RAG pipeline for the HashiCorp ecosystem (Terraform, Vault, Consul, Nomad, Packer, Boundary).

---

## ⚡ Quick Commands

| Category | Command |
| :--- | :--- |
| **Deploy** | `task up REPO_URI={url} REGION=us-west-2` |
| **Pipeline** | `task pipeline:run` (KB ID auto-detected from `kb.auto.tfvars`) |
| **Validation** | `task pipeline:test KB_ID={id}` |
| **MCP Setup** | `task mcp:setup KB_ID={id}` (exposes RAG to Claude Code) |
| **Claude Bedrock** | `task claude:setup` (routes Claude Code through Bedrock) |
| **Terraform** | `task plan` \| `task apply` \| `task validate` |
| **CI** | `task ci` (fmt:check + validate + shellcheck + tests) |

---

## 🏗️ Architectural Pillars

* **Two-phase deployment**: The Bedrock Knowledge Base is **not** a Terraform resource — `aws_bedrock_knowledge_base` requires the OpenSearch collection ARN, which is only available after `terraform apply`. `deploy.sh` runs Terraform twice: first to provision infra, then `create_knowledge_base.py` writes `kb.auto.tfvars`, then a second apply wires the IDs into the EventBridge Scheduler target.

* **Step Functions orchestration**: The state machine uses `.sync` integration for CodeBuild (no polling loop needed — Step Functions uses CloudWatch Events to detect build completion). Bedrock ingestion lacks a `.sync` integration, so the pipeline uses a manual poll loop (`WaitForIngestion` → `GetIngestionStatus` → `CheckIngestionStatus`).

* **Semantic Chunking**: `process_docs.py` splits docs at `##`/`###` heading boundaries before upload. Sections under 200 chars are merged; sections over ~4000 chars are split at code-fence boundaries. Bedrock applies `FIXED_SIZE` chunking (1024 tokens, 20% overlap) during ingestion — the pre-splitting ensures chunks land on structural boundaries.

* **Metadata Engine**: `generate_metadata.py` produces `.metadata.json` sidecar files next to every document. These are uploaded to S3 alongside the markdown and registered by Bedrock at ingestion time. The MCP server infers `product`, `product_family`, and `source_type` from the S3 object path when the `retrieve()` API does not return custom metadata.

* **Cross-Source Deduplication**: `deduplicate.py` removes near-duplicate files by SHA-256 of normalised body content before upload. Prevents the same content entering through multiple sources.

* **Parallel Validation**: The `ValidateRetrieval` state uses a Step Functions `Map` state with `MaxConcurrency: 5` to run 10 test queries covering all product families simultaneously. Zero results log a warning but do NOT fail the pipeline.

---

## 📂 Project Structure

| Path | Purpose |
| :--- | :--- |
| `terraform/` | All AWS infrastructure (S3, IAM, CodeBuild, Step Functions, EventBridge, OpenSearch, CloudWatch) |
| `step-functions/rag_pipeline.asl.json` | ASL state machine definition |
| `codebuild/buildspec.yml` | CodeBuild build phases |
| `codebuild/scripts/` | Data processing scripts (cloud-agnostic) |
| `scripts/` | Deploy, bootstrap, and operational scripts |
| `mcp/server.py` | MCP server — exposes KB as Claude Code tools |
| `docs/` | Architecture, runbook, MCP guide, diagrams |

---

## ⚠️ Critical Constraints

* **Region**: Use `us-west-2` or `us-east-1` for broadest Bedrock availability. Not all foundation models are available in all regions.

* **OpenSearch minimum cost**: 2 OCUs minimum ≈ $350/month. Use Aurora PostgreSQL Serverless v2 (`type = "RDS"` storage config in `create_knowledge_base.py`) if cost is a concern.

* **Bedrock model access**: Must be explicitly enabled per region in the Bedrock console (Model access → Request access for Titan Embeddings V2).

* **Two-apply pattern**: Never pass `knowledge_base_id` or `data_source_id` in the initial Terraform apply — these values don't exist until `create_knowledge_base.py` runs. `deploy.sh` handles the sequencing automatically.

* **DynamoDB lock table**: Unlike GCS (built-in locking), the S3 Terraform backend requires a separate DynamoDB table for state locking. `bootstrap_state.sh` creates it.

* **S3 bucket creation in us-east-1**: Must omit `--create-bucket-configuration` for `us-east-1`. `bootstrap_state.sh` handles this conditionally.

---

## 🛠️ Maintenance Workflow

1. **Add new content sources**: Edit `codebuild/scripts/clone_repos.sh` (new repo) or create a new fetch script. Commit and push — the next pipeline run picks up changes automatically.

2. **Modify chunking**: Change `chunk_size`/`chunk_overlap_pct` in `terraform.tfvars`. Run `task plan && task apply` then `task pipeline:run`.

3. **Apply infra changes**: `task plan && task apply`.

4. **Re-sync knowledge base**: `task pipeline:run`.

5. **Validate**: `task pipeline:test KB_ID={id}` — verify all 10 topics return results.

---

## 📋 Known Gotchas

| Issue | Fix |
| :--- | :--- |
| KB creation takes 5–10 min | `create_knowledge_base.py` polls `DescribeKnowledgeBase` until `ACTIVE` |
| OpenSearch index must exist before first ingestion | `create_knowledge_base.py` creates the HNSW index via the OpenSearch client |
| `us-east-1` bucket creation fails with `LocationConstraint` | `bootstrap_state.sh` conditionally omits `--create-bucket-configuration` |
| `DynamoDB ResourceInUseException` on re-bootstrap | `bootstrap_state.sh` checks existence before creating |
| Bedrock no `.sync` for ingestion | Step Functions uses manual poll: `WaitForIngestion` → `GetIngestionStatus` loop |
| GitHub rate limit (60 req/hr unauth) | Store token in Secrets Manager tagged `Project=hashicorp-rag-pipeline` |
