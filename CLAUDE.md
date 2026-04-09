# CLAUDE.md — Project Instructions for Claude Code

## Build & Test Commands

```bash
task ci                # all CI checks: fmt:check + validate + shellcheck + tests
task plan              # terraform plan
task apply             # terraform apply (interactive confirm)
task docs:test         # validate Kendra retrieval (10 test queries)
task graph:test        # validate Neptune graph has nodes/edges
task test              # Python unit tests (pytest)
task shellcheck        # lint all shell scripts
task fmt               # format Terraform files
task fmt:check         # check Terraform formatting (no writes)
```

## Code Conventions

- **Python**: type hints required, `ruff check` clean, `logging` module (not `print`), docstrings on public functions
- **Bash**: all scripts must pass `shellcheck`
- **Terraform**: `terraform fmt` + `terraform validate` must pass; no hardcoded account IDs, bucket names, or index IDs
- **No secrets**: no credentials or tokens in code, logs, or committed files

## AWS Constraints (Hard Failures)

- **Kendra S3 data source**: must use `s3_configuration` with `inclusion_patterns = ["**/*.md"]`. Do NOT use `exclusion_patterns` (blocks `.metadata.json` sidecars) or `template_configuration` (invalid for S3 type, fails with `S3ConnectorConfiguration` error)
- **Kendra edition**: cannot be changed in-place. Changing `kendra_edition` destroys and recreates the index
- **Security group descriptions**: ASCII only. No em dashes, smart quotes, or other non-ASCII characters — EC2 rejects them with `InvalidParameterValue`
- **Lambda env vars**: never set `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, or other reserved keys. Lambda injects them at runtime. Setting them causes `InvalidParameterValueException`
- **Neptune SigV4 signing**: use `botocore.auth.SigV4Auth` + `botocore.awsrequest.AWSRequest`, NOT `requests-aws4auth`. The third-party library produces signature mismatches with Neptune's openCypher endpoint
- **Step Functions `.sync` integrations**: the execution role must trust both `states.amazonaws.com` AND `events.amazonaws.com`, and `iam:PassRole` must allow both services. Missing either causes `AccessDeniedException: not authorized to create managed-rule`

## Architecture (Key Facts)

- Two pipelines: **Docs** (Kendra) and **Graph** (Neptune, opt-in via `create_neptune = true`)
- Neptune proxy is opt-in: `neptune_create_proxy = true` deploys API Gateway + Lambda
- Orchestration: EventBridge Scheduler -> Step Functions -> CodeBuild -> S3/Kendra/Neptune
- MCP server (`mcp/server.py`) exposes both backends to Claude Code
- `PIPELINE_TARGET` env var gates CodeBuild phases: `all`, `docs`, `registry`, `discuss`, `blogs`
- Blog content comes from RSS/Atom feed inline tags, NOT scraped URLs (Cloudflare blocks scraping)
- CDKTF content is excluded at every ingestion stage

## Terraform Module Layout

| Module | Path | Opt-in |
|---|---|---|
| hashicorp-docs-pipeline | `terraform/modules/hashicorp-docs-pipeline/` | Always |
| terraform-graph-store | `terraform/modules/terraform-graph-store/` | `create_neptune = true` |
| state-backend | `terraform/modules/state-backend/` | Always (via bootstrap) |

## Don't

- Don't use `exclusion_patterns` or `template_configuration` on Kendra S3 data sources
- Don't set reserved AWS env vars in Lambda definitions
- Don't use non-ASCII in AWS resource `description` fields
- Don't use `requests-aws4auth` for Neptune SigV4
- Don't add `.terraform/`, `__pycache__`, `*.tfstate`, `node_modules`, `.git/`, or logs to version control
