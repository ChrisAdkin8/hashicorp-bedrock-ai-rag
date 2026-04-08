# Operational Runbook — HashiCorp RAG + Graph Pipeline

This runbook covers day-to-day operations, failure diagnosis, and recovery procedures for both the HashiCorp docs pipeline (Kendra) and the Terraform graph pipeline (Neptune).

---

## Quick Links

| Resource | Link pattern |
|---|---|
| Step Functions | `https://console.aws.amazon.com/states/home?region=REGION#/statemachines` |
| CodeBuild history | `https://console.aws.amazon.com/codesuite/codebuild/projects?region=REGION` |
| CloudWatch Logs (docs CodeBuild) | `https://console.aws.amazon.com/cloudwatch/home?region=REGION#logsV2:log-groups/log-group/$252Faws$252Fcodebuild$252Frag-hashicorp-pipeline` |
| Amazon Kendra | `https://console.aws.amazon.com/kendra/home?region=REGION` |
| Amazon Neptune | `https://console.aws.amazon.com/neptune/home?region=REGION` |
| S3 RAG bucket | `https://s3.console.aws.amazon.com/s3/buckets/hashicorp-rag-docs-REGION-SUFFIX` |

Replace `REGION` with your deployment region (auto-detected from `terraform/terraform.tfvars`; defaults to `us-east-1` if not set).

---

## Docs Pipeline Operations

### Trigger a pipeline run manually

```bash
# Full run (all content sources)
task pipeline:run

# With explicit IDs if terraform output is unavailable
task pipeline:run KENDRA_INDEX_ID=<INDEX_ID> KENDRA_DS_ID=<DATA_SOURCE_ID>

# Targeted run — refresh a single content source
task pipeline:run TARGET=blogs      # HashiCorp blog posts only
task pipeline:run TARGET=discuss    # HashiCorp Discuss threads only
task pipeline:run TARGET=docs       # product repo documentation only
task pipeline:run TARGET=registry   # Terraform public registry modules only
```

Valid `TARGET` values: `all` (default), `docs`, `registry`, `discuss`, `blogs`.

### Check pipeline status

```bash
task pipeline:status
```

Or directly:

```bash
aws stepfunctions list-executions \
  --state-machine-arn $(terraform -chdir=terraform output -raw state_machine_arn) \
  --max-results 5
```

### Validate retrieval quality

```bash
task pipeline:test KENDRA_INDEX_ID=$(terraform -chdir=terraform output -raw kendra_index_id)
```

### Measure token efficiency

```bash
task pipeline:token-efficiency
# or explicitly:
task pipeline:token-efficiency KENDRA_INDEX_ID=<INDEX_ID> REGION=us-east-1
```

### Re-ingest after adding new content sources

1. Update `codebuild/scripts/clone_repos.sh` or the appropriate fetch script.
2. Update `codebuild/scripts/process_docs.py` if the new repo requires a custom `docs_subdirs` entry.
3. Commit and push.
4. Run `task pipeline:run` to trigger a full re-ingest.

---

## Graph Pipeline Operations

### Trigger a graph population run

```bash
# Single repo
task graph:populate GRAPH_REPO_URIS="https://github.com/org/infra-repo"

# Multiple repos (space-separated)
task graph:populate GRAPH_REPO_URIS="https://github.com/org/infra-repo https://github.com/org/app-repo"

# Or set graph_repo_uris in terraform.tfvars and omit the override
task graph:populate
```

The task starts the graph Step Functions state machine, waits for completion, and prints the execution status.

### Check graph pipeline status

```bash
task graph:status
```

Or directly:

```bash
aws stepfunctions list-executions \
  --state-machine-arn $(terraform -chdir=terraform output -raw graph_state_machine_arn) \
  --max-results 5
```

### Manually query Neptune (openCypher)

Neptune does not expose a public endpoint. Access it from within the VPC (e.g., via a bastion, Cloud9, or a VPC-enabled Lambda):

```bash
# List all resource types in the graph
curl -X POST \
  "https://<NEPTUNE_ENDPOINT>:8182/openCypher" \
  -H "Content-Type: application/json" \
  -d '{"query": "MATCH (n) RETURN labels(n), count(n) ORDER BY count(n) DESC LIMIT 20"}'

# Find dependencies of a specific resource
curl -X POST \
  "https://<NEPTUNE_ENDPOINT>:8182/openCypher" \
  -H "Content-Type: application/json" \
  -d '{"query": "MATCH (a)-[:DEPENDS_ON]->(b) WHERE a.address = \"aws_instance.web\" RETURN b.address, b.type"}'
```

---

## Failure Diagnosis — Docs Pipeline

### CodeBuild build failed

1. Open CloudWatch Logs → `/aws/codebuild/rag-hashicorp-pipeline` and find the failing build stream.
2. Common causes:
   - **Git clone failure** — transient network issue; re-run the pipeline.
   - **Python import error** — missing dependency; add to `codebuild/scripts/requirements.txt`.
   - **S3 upload permission denied** — verify the CodeBuild IAM role has `s3:PutObject` on the RAG bucket.
   - **GitHub rate limit** — unauthenticated API limit (60 req/hr); add `GITHUB_TOKEN` to Secrets Manager.
   - **No markdown files found for a product** — the repo may have changed its docs directory layout; update `docs_subdirs` in `process_docs.py`.

### Kendra sync failed

1. In the Step Functions console, find the failed `StartSync` or `ListSyncJobs` state and expand its output.
2. In the Kendra console → your index → Data sources → sync history, expand the failed sync for `FailureReasons`.
3. Common causes:
   - **ResourceNotFoundException on data source** — the `kendra_data_source_id` Terraform output may be stale. Run `terraform -chdir=terraform output` and verify `kendra_data_source_id` differs from `kendra_index_id`.
   - **AccessDeniedException** — the Kendra S3 role lacks `s3:GetObject` or `s3:ListBucket` on the RAG bucket.
   - **Invalid metadata sidecar** — a `.metadata.json` file is malformed. Check `generate_metadata.py` output.
   - **Index capacity exceeded** — check the Kendra console → index metrics. Switch to Enterprise Edition if on Developer Edition.

### Token efficiency task fails

- **"Region X does not support Kendra"** — Supported regions: `us-east-1`, `us-east-2`, `us-west-2`, `eu-west-1`, `eu-west-2`, `ap-southeast-1`, `ap-southeast-2`, `ap-northeast-1`, `ap-northeast-2`, `ca-central-1`.
- **"Index not found in region X … Found it in Y"** — the script scans supported regions and suggests the correct one.
- **"Index is not ACTIVE"** — wait for Kendra to finish initialising; check the Kendra console.
- **No results / empty output** — run `task pipeline:run` to ingest and sync.

### Zero retrieval results

1. Run `task pipeline:test KENDRA_INDEX_ID=<INDEX_ID>`.
2. Verify the last Kendra sync succeeded: Kendra console → Data sources → last sync status.
3. Check that the S3 bucket contains documents for the missing product:
   ```bash
   aws s3 ls s3://$(terraform -chdir=terraform output -raw rag_bucket_name)/documentation/ --region us-east-1
   ```
4. If a product folder is missing, check CloudWatch Logs for `No markdown files found in <repo>`.

### Zero blog files in S3

1. Check CloudWatch Logs for `fetch_blogs.py complete — 0 files written`.
2. `fetch_blogs.py` reads content from RSS/Atom feed inline tags — it does **not** scrape article URLs.
3. If feed URLs are failing, verify `https://www.hashicorp.com/blog/feed.xml` returns HTTP 200.
4. If `lxml` is missing: verify `lxml>=5.0` is in `codebuild/scripts/requirements.txt`.

### Kendra index at capacity (Developer Edition)

1. Change `kendra_edition` to `ENTERPRISE_EDITION` in `terraform/variables.tf`.
2. Run `terraform -chdir=terraform apply -auto-approve` — destroys and recreates the index (10–30 minutes).
3. Re-run `task pipeline:run` to re-sync all documents.

---

## Failure Diagnosis — Graph Pipeline

### Graph CodeBuild build failed

1. Open CloudWatch Logs → `/aws/codebuild/graph-pipeline` and find the failing build stream.
2. Common causes:
   - **Terraform init/plan failed** — verify the workspace repo is accessible and the CodeBuild IAM role has permissions to read the repo (or that `GITHUB_TOKEN` is set if private).
   - **rover not found** — check `codebuild/buildspec_graph.yml` installs rover from the expected URL.
   - **Neptune connection refused** — verify CodeBuild's security group has port 8182 egress to the Neptune security group. Check the VPC config on the CodeBuild project.
   - **IAM auth failure on Neptune** — confirm `neptune_iam_auth_enabled = true` and the CodeBuild role has `neptune-db:connect` and `neptune-db:*` data operation actions on the cluster resource ARN.
   - **SigV4 signature mismatch (403 "signature we calculated does not match")** — the `ingest_graph.py` script must send Neptune openCypher requests using form-encoded `data=` with `parameters` as a JSON string, **not** `json=` body. The `requests-aws4auth` library computes different payload hashes for JSON vs form-encoded bodies, causing Neptune to reject the signature. Fix: use `data={"query": ..., "parameters": json.dumps(...)}` in the `requests.post()` call.
   - **`ingest_graph.py` "No resource nodes found"** — `terraform graph` produced a DOT file with no nodes matching the resource filter. Check that the cloned repo has `.tf` files with real resources (not just module calls with `count = 0`).

### Neptune cluster unavailable

1. Check the Neptune console → cluster status. If `creating` or `modifying`, wait for it to become `available`.
2. Verify the cluster is in the same VPC as the CodeBuild security group.
3. Check the Neptune security group allows inbound on port 8182 from the CodeBuild security group.

### Graph pipeline not populating on schedule

1. Check the EventBridge scheduler: `aws scheduler get-schedule --name graph-weekly-refresh`.
2. Verify the scheduler IAM role has `states:StartExecution` on the graph state machine ARN.
3. Verify `graph_repo_uris` is non-empty in `terraform.tfvars` (an empty list causes the Map state to complete immediately with no builds).

### EventBridge Scheduler not triggering (either pipeline)

1. `aws scheduler get-schedule --name rag-weekly-refresh` (or `graph-weekly-refresh`).
2. Verify the scheduler IAM role has `states:StartExecution` on the correct state machine ARN.
3. Look for `SchedulerInvocationError` events in CloudWatch.

---

## Cost Management

### Pause the docs pipeline

```bash
aws scheduler update-schedule \
  --name rag-weekly-refresh \
  --state DISABLED \
  --schedule-expression "cron(0 2 ? * SUN *)" \
  --flexible-time-window '{"Mode":"OFF"}' \
  --target "{\"Arn\":\"$(terraform -chdir=terraform output -raw state_machine_arn)\",\"RoleArn\":\"SCHEDULER_ROLE_ARN\"}"
```

### Full teardown

```bash
task destroy
```

> **Warning:** This destroys all Terraform-managed resources including the Kendra index, Neptune cluster (if deployed), S3 buckets, CodeBuild projects, Step Functions state machines, and EventBridge schedulers. The S3 buckets have `prevent_destroy = true` in the Terraform lifecycle — you must set `force_destroy = true` first if they contain objects.

---

## Adding a GitHub Token

Store the token in Secrets Manager so CodeBuild can raise the GitHub API rate limit from 60 to 5,000 req/hour:

```bash
aws secretsmanager create-secret \
  --name github-token \
  --secret-string '{"token":"ghp_..."}' \
  --tags Key=Project,Value=hashicorp-rag-pipeline
```

Uncomment the `secrets-manager` block in `codebuild/buildspec.yml` to activate it. No other changes required.

---

## Updating the Pipeline

### Terraform changes

```bash
task plan    # review proposed changes
task apply   # apply
```

### CodeBuild script changes

Commit and push. The next pipeline run will pick up the latest scripts — CodeBuild clones this repository fresh on every build using the `REPO_URL` environment variable.

### Adding a new Terraform workspace to the graph pipeline

1. Add the repo URL to `graph_repo_uris` in `terraform/terraform.tfvars`.
2. Run `task apply` to update the Step Functions input.
3. Run `task graph:populate` to ingest immediately.

### Adjusting content exclusions (docs pipeline)

- **Docs paths** — edit `CDKTF_EXCLUDE_RE` in `process_docs.py`
- **Blog posts** — edit `_CDKTF_RE` and the body mention threshold (currently 3) in `fetch_blogs.py`
- **Discuss threads / GitHub issues** — edit `_CDKTF_RE` in `fetch_discuss.py` / `fetch_github_issues.py`

### Adding new product documentation

For products in `hashicorp/web-unified-docs` (Vault, Consul, Nomad, TFE, HCP Terraform):
1. Add a REPO_CONFIG entry in `process_docs.py` with `"repo_dir": "web-unified-docs"` and the correct `docs_subdirs`.
2. Commit, push, and run `task pipeline:run`.

For products with their own GitHub repo:
1. Add the repo to `clone_repos.sh` CORE_REPOS.
2. Add a config entry in `process_docs.py` REPO_CONFIG with the appropriate `docs_subdirs`.
3. Commit, push, and run `task pipeline:run`.
