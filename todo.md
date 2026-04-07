# hashicorp-bedrock-ai-rag — Build Checklist

## Status: Complete

---

## Terraform

- [x] terraform/versions.tf
- [x] terraform/variables.tf
- [x] terraform/main.tf
- [x] terraform/outputs.tf
- [x] terraform/terraform.tfvars.example

---

## Step Functions

- [x] step-functions/rag_pipeline.asl.json

---

## CodeBuild

- [x] codebuild/buildspec.yml
- [x] codebuild/scripts/clone_repos.sh
- [x] codebuild/scripts/discover_modules.py
- [x] codebuild/scripts/process_docs.py
- [x] codebuild/scripts/fetch_github_issues.py
- [x] codebuild/scripts/fetch_discuss.py
- [x] codebuild/scripts/fetch_blogs.py
- [x] codebuild/scripts/deduplicate.py
- [x] codebuild/scripts/requirements.txt
- [x] codebuild/scripts/generate_metadata.py
- [x] codebuild/scripts/tests/__init__.py
- [x] codebuild/scripts/tests/test_process_docs.py
- [x] codebuild/scripts/tests/test_fetch_github_issues.py
- [x] codebuild/scripts/tests/test_deduplicate.py

---

## Scripts

- [x] scripts/deploy.sh
- [x] scripts/bootstrap_state.sh
- [x] scripts/create_knowledge_base.py
- [x] scripts/run_pipeline.sh
- [x] scripts/setup_claude_bedrock.sh
- [x] scripts/setup_mcp.sh
- [x] scripts/test_retrieval.py
- [x] scripts/test_token_efficiency.py

---

## MCP Server

- [x] mcp/server.py
- [x] mcp/test_server.py
- [x] mcp/requirements.txt

---

## CI

- [x] .github/workflows/terraform.yml

---

## Docs

- [x] docs/ARCHITECTURE.md
- [x] docs/MCP_SERVER.md
- [x] docs/RUNBOOK.md
- [x] docs/diagrams/architecture.svg
- [x] docs/diagrams/ingestion_pipeline.svg

---

## Update existing files

- [x] Taskfile.yml  (AWS equivalents: gcloud→awscli, Vertex→Bedrock, GCS→S3, Workflows→SFN)
- [x] README.md    (rewrite for AWS/Bedrock)
- [x] AGENTS.md    (rewrite for AWS/Bedrock)

---

## Key spec decisions (from PROMPT.md)

- Backend: S3 + DynamoDB lock table (not GCS)
- Orchestration: Step Functions ASL JSON (not Cloud Workflows YAML)
- Build: CodeBuild buildspec.yml (not cloudbuild.yaml)
- Vector store: OpenSearch Serverless (hashicorp-rag-vectors)
- Embedding: amazon.titan-embed-text-v2:0 (1024 dims)
- Chunking: FIXED_SIZE 1024 tokens / 20% overlap on data source
- Auth: IAM roles (not service accounts); OIDC for GitHub Actions
- Secrets: AWS Secrets Manager (not GCP Secret Manager)
- Workspace path in CodeBuild: /codebuild/output/ (not /workspace/)
- Upload: aws s3 sync --delete (not gsutil rsync)
- MCP env vars: AWS_REGION, AWS_KNOWLEDGE_BASE_ID (not VERTEX_*)
- SFN .sync integration for CodeBuild (no manual poll loop needed)
- Bedrock ingestion: manual poll loop required (no .sync)
- deploy.sh: 5 steps — bootstrap → tf apply → create KB → tf apply (kb.auto.tfvars) → run pipeline
