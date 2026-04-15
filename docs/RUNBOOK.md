# Runbook — HashiCorp RAG Pipeline

## Deployer IAM roles

The authenticated IAM user (or role) running `task up` must hold the
following permissions. These are the minimum required to create all resources
managed by Terraform and the helper scripts.

| IAM Policy / Permission | Why |
|---|---|
| `AmazonKendraFullAccess` | Create and manage Kendra index + data sources |
| `AmazonS3FullAccess` | Create S3 buckets (state, RAG docs, graph staging) |
| `AWSCodeBuildAdminAccess` | Create CodeBuild projects |
| `AWSStepFunctionsFullAccess` | Create Step Functions state machines |
| `AmazonEventBridgeSchedulerFullAccess` | Create EventBridge Scheduler schedules |
| `IAMFullAccess` | Create IAM roles, policies, and OIDC providers |
| `CloudWatchFullAccessV2` | Create alarms, dashboards, log groups |
| `AmazonVPCFullAccess` | VPC/subnet/security-group for Neptune (when enabled) |
| `NeptuneFullAccess` | Create Neptune cluster (when `create_neptune = true`) |
| `SecretsManagerReadWrite` | Store GitHub token for elevated API rate limits |
| `AWSLambda_FullAccess` | Create Neptune proxy Lambda (when `neptune_create_proxy = true`) |

Grant all policies in one pass (replace `USERNAME`):

```bash
USERNAME=chris.adkin
for policy in \
  arn:aws:iam::policy/AmazonKendraFullAccess \
  arn:aws:iam::policy/AmazonS3FullAccess \
  arn:aws:iam::policy/AWSCodeBuildAdminAccess \
  arn:aws:iam::policy/AWSStepFunctionsFullAccess \
  arn:aws:iam::policy/AmazonEventBridgeSchedulerFullAccess \
  arn:aws:iam::policy/IAMFullAccess \
  arn:aws:iam::policy/CloudWatchFullAccessV2 \
  arn:aws:iam::policy/AmazonVPCFullAccess \
  arn:aws:iam::policy/NeptuneFullAccess \
  arn:aws:iam::policy/SecretsManagerReadWrite \
  arn:aws:iam::policy/AWSLambda_FullAccess; do
  aws iam attach-user-policy --user-name "$USERNAME" --policy-arn "$policy"
done
```

The preflight check (`task preflight`) verifies AWS credentials and basic
access automatically — run it to catch missing permissions before deploying.

---

## Initial deployment

The entire pipeline — infrastructure and first data ingestion — is deployed with a single command:

```bash
task up REPO_URI=https://github.com/my-org/aws-hashi-knowledge-base
```

`REGION` is auto-detected from `terraform/terraform.tfvars`. Override with `REGION=<region>` if needed (defaults to `us-east-1`).

`task up` runs preflight checks first, then calls `scripts/deploy.sh`, which runs four idempotent steps:

1. **Bootstrap** — creates the S3 state bucket and initialises Terraform remote backend.
2. **Apply** — `terraform init` + `terraform apply` to provision all AWS resources (IAM roles, S3 bucket, Kendra index + data source, CodeBuild projects, Step Functions state machines, EventBridge Scheduler jobs, CloudWatch alarms).
3. **Populate Kendra** — triggers the docs ingestion pipeline (unless `--skip-pipeline`).
4. **Populate Neptune** — triggers the graph extraction pipeline if `create_neptune = true` is detected in Terraform outputs (unless `--skip-pipeline`).

Re-running `task up` is safe — each step detects existing state and skips automatically.

### Running preflight checks independently

You can run all preflight checks without deploying:

```bash
task preflight
```

The preflight script validates CLI tools (terraform >= 1.5, aws, python3 >= 3.11, jq, shellcheck), AWS authentication and account access, Python packages, repository file integrity, and Terraform formatting/validation.

### Re-deploying to a second environment

```bash
task up \
  REGION=eu-west-1 \
  REPO_URI=https://github.com/my-org/aws-hashi-knowledge-base
```

For a different environment, use a separate AWS account or Terraform workspace. Each environment gets its own Kendra index and S3 state bucket.

---

## Monitoring

### Console Links

Replace `REGION` with your deployment region.

| Resource | URL |
|---|---|
| Step Functions executions | `https://console.aws.amazon.com/states/home?region=REGION#/statemachines` |
| CodeBuild history | `https://console.aws.amazon.com/codesuite/codebuild/projects?region=REGION` |
| CloudWatch Logs (docs) | `https://console.aws.amazon.com/cloudwatch/home?region=REGION#logsV2:log-groups/log-group/$252Faws$252Fcodebuild$252Frag-hashicorp-pipeline` |
| Amazon Kendra | `https://console.aws.amazon.com/kendra/home?region=REGION` |
| Amazon Neptune | `https://console.aws.amazon.com/neptune/home?region=REGION` |
| S3 RAG bucket | `https://s3.console.aws.amazon.com/s3/buckets/hashicorp-rag-docs-REGION-SUFFIX` |

### Key Log Queries

**Step Functions execution failures:**
```
fields @timestamp, @message
| filter ispresent(execution_arn) and status = "FAILED"
| sort @timestamp desc
| limit 20
```

**CodeBuild failures:**
```
fields @timestamp, @message
| filter @logStream like /rag-hashicorp-pipeline/
| filter @message like /FAILED|Error|Exception/
| sort @timestamp desc
| limit 20
```

**Pipeline summary (process_docs output):**
```
fields @timestamp, @message
| filter @message like /files processed/
| sort @timestamp desc
```

---

## Investigating a Failed Run — troubleshoot

### Step 1 — Identify the failure point

1. Open Step Functions in the Console.
2. Find the failed execution.
3. Click it and expand the step graph. The first red state is the failure point.

### Step 2 — Check CodeBuild logs

If the failure is in a CodeBuild step:

1. Note the build ID from the Step Functions execution details.
2. Navigate to CodeBuild → Build history → find the build by ID.
3. Expand the failing phase and read the CloudWatch logs.

Common build failures:
- **`clone_repos.sh` timeout** — increase the CodeBuild step timeout or reduce the number of repos.
- **`process_docs.py` crash** — a malformed markdown file caused an unhandled exception. Check logs for the filename.
- **S3 upload permission denied** — the CodeBuild IAM role lacks `s3:PutObject` on the RAG bucket. Re-run `task apply` to reconcile IAM.

### Step 3 — Check Kendra sync errors

If the failure is in the `StartSync` or `ListSyncJobs` state:

- **ResourceNotFoundException on data source** — the `kendra_data_source_id` Terraform output may be stale. Run `task output` and verify.
- **AccessDeniedException** — the Kendra S3 role lacks `s3:GetObject` or `s3:ListBucket` on the RAG bucket.
- **Invalid metadata sidecar** — a `.metadata.json` file is malformed. Check `generate_metadata.py` output.

### Common Errors

| Error | Cause | Fix |
|---|---|---|
| `Permission denied on S3 bucket` | CodeBuild role missing `s3:PutObject` | Re-run `task apply` to reconcile IAM |
| `ResourceNotFoundException: Data source` | Kendra data source ID mismatch | Run `task output` and verify IDs; re-apply if stale |
| `CodeBuild timeout` | Too many repos to clone within the timeout | Increase timeout or reduce repo count |
| `GitHub API rate limit (403)` | Unauthenticated limit is 60 req/hr | Add `GITHUB_TOKEN` to Secrets Manager (see below) |
| `Kendra index at capacity` | Developer Edition document limit reached | Switch `kendra_edition` to `ENTERPRISE_EDITION` and re-apply |
| `Kendra sync failed — AccessDeniedException` | S3 role missing bucket permissions | Re-run `task apply` |
| `Discourse rate limit (429)` | `fetch_discuss.py` hit discuss.hashicorp.com rate limit | Script retries automatically; increase `REQUEST_DELAY` if persistent |
| `Blog fetch timeout` | `fetch_blogs.py` timed out | Increase step timeout or reduce page safety limit |
| `Medium RSS empty` | `fetch_blogs.py` returned 0 SE posts | Check if `medium.com/feed/hashicorp-engineering` is still active |
| `No markdown files found` | Repo changed its docs directory layout | Update `docs_subdirs` in `process_docs.py` |
| `Region X does not support Kendra` | Kendra not available in that region | Use a supported region: `us-east-1`, `us-east-2`, `us-west-2`, `eu-west-1`, `eu-west-2`, `ap-southeast-1`, `ap-southeast-2`, `ap-northeast-1`, `ap-northeast-2`, `ca-central-1` |

---

## How to add a new provider

1. Open `codebuild/scripts/clone_repos.sh`.
2. Add an entry to the `PROVIDER_REPOS` associative array:
   ```bash
   ["terraform-provider-<NAME>"]="https://github.com/hashicorp/terraform-provider-<NAME>.git"
   ```
3. Open `codebuild/scripts/process_docs.py`.
4. Add an entry to `REPO_CONFIG`:
   ```python
   "terraform-provider-<NAME>": {
       "source_type": "provider",
       "product": "<NAME>",
       "docs_subdir": "website/docs",
   },
   ```
5. Commit, push, and trigger the pipeline with `task docs:run`.

---

## How to Add a New HashiCorp Product Repo

For products in `hashicorp/web-unified-docs` (Vault, Consul, Nomad, TFE, HCP Terraform):

1. Add a `REPO_CONFIG` entry in `process_docs.py` with `"repo_dir": "web-unified-docs"` and the correct `docs_subdirs`.
2. Commit, push, and run `task docs:run`.

For products with their own GitHub repo:

1. Add the repo to `clone_repos.sh` `CORE_REPOS`.
2. Add a config entry in `process_docs.py` `REPO_CONFIG` with the appropriate `docs_subdirs`.
3. Commit, push, and run `task docs:run`.

---

## How to Force a Full Re-import

### Option A — Re-upload S3 objects and re-trigger

```bash
aws s3 rm s3://$(terraform -chdir=terraform output -raw rag_bucket_name)/ --recursive
task docs:run
```

### Option B — Delete and recreate the Kendra index

Destroy and re-provision the index via Terraform:

```bash
# Destroy just the Kendra index (takes 10–30 minutes to recreate)
terraform -chdir=terraform destroy -target=module.hashicorp_docs_pipeline.aws_kendra_index.main

# Re-apply to recreate
task apply

# Trigger a fresh ingestion run
task docs:run
```

---

## How to Tune Chunking

Chunks are defined by `codebuild/scripts/process_docs.py` before upload. Kendra applies its own document processing during sync.

- **Section boundary**: change `MIN_SECTION_SIZE` (default 200 chars) to merge more or fewer small sections.
- **Large-section split**: change the `max_chars` parameter in `_split_large_section` (default 2000 chars) to control how oversized sections are further split at code-fence boundaries.
- **Code block compression**: `_compress_code_blocks()` strips comments and collapses blank lines inside fenced code blocks. Disable by removing the call in `process_file()` if you need verbatim code in chunks.

After changing any of these, force a full re-import (see above) to apply to the index.

---

## How to Change the Kendra Edition

Kendra offers two editions:

- **Developer Edition** (~$810/month) — 750 queries/day, 10,000 documents.
- **Enterprise Edition** (~$1,400/month) — 8,000 queries/day, 100,000 documents, higher availability.

To switch:

1. Edit `terraform/terraform.tfvars`:
   ```hcl
   kendra_edition = "ENTERPRISE_EDITION"
   ```
2. Run `task apply`.
3. **Note:** Changing the edition destroys and recreates the Kendra index (10–30 minutes). All documents must be re-synced. Run `task docs:run` after apply completes.

---

## Cost Management

### Estimating costs

| Component | Pricing basis | Estimate |
|---|---|---|
| Amazon Kendra (Enterprise) | Flat monthly | ~$1,400/month while index exists |
| Amazon Kendra (Developer) | Flat monthly | ~$810/month while index exists |
| S3 storage | Per GB-month | ~$0.023/GB/month |
| CodeBuild | Per build-minute (general1.medium) | ~$0.005/min; expect 30–60 min/week |
| Step Functions | Per state transition | Negligible for weekly runs |
| EventBridge Scheduler | Per invocation | Negligible for weekly runs |
| Neptune (db.r6g.large) | Per instance-hour | ~$0.348/hour (~$254/month) |
| Neptune proxy (Lambda) | Per invocation + duration | Negligible at MCP query volumes |

### Reducing costs

- **Delete the Kendra index when not in use.** Set `force_destroy = true` in tfvars, then run `task destroy`. Kendra billing stops immediately.
- **Use Developer Edition** if under 10,000 documents. Saves ~$590/month.
- **Reduce clone frequency.** Change `refresh_schedule` from weekly to monthly if docs don't change often.
- **Use a smaller CodeBuild machine type.** Switch `BUILD_COMPUTE_TYPE` if the build fits within the timeout.
- **Filter repos.** Remove infrequently-updated repos from `clone_repos.sh`.
- **Disable Neptune when not needed.** Set `create_neptune = false` and run `task apply` — the cluster and VPC resources are destroyed.

### Monitoring costs

Set up a budget alert in the AWS Billing console for the account. Use Cost Explorer with service-level filtering to see Kendra, S3, CodeBuild, and Neptune costs separately.

---

## Graph pipeline (Neptune)

The graph pipeline is opt-in (`create_neptune = true` in `terraform.tfvars`) and provisioned by `terraform/modules/terraform-graph-store/`.

### Enabling

1. Edit `terraform/terraform.tfvars`:
   ```hcl
   create_neptune          = true
   neptune_vpc_id          = "vpc-0123456789abcdef0"
   neptune_subnet_ids      = ["subnet-aaa", "subnet-bbb"]
   neptune_iam_auth_enabled = true
   graph_repo_uris = [
     "https://github.com/my-org/my-tf-workspace",
     "https://github.com/my-org/another-workspace",
   ]
   ```
2. `task apply` — provisions the Neptune cluster, security groups, CodeBuild project, Step Functions state machine, and EventBridge Scheduler job.
3. `task graph:populate` — triggers a one-off run rather than waiting for the weekly cron.
4. `task graph:test` — verifies that Neptune has nodes and edges.

### Daily operations

| Action | Command |
|---|---|
| Trigger an ad-hoc refresh | `task graph:populate` |
| Smoke-test the store | `task graph:test` |
| Inspect last 5 runs | `task graph:status` |
| File counts in RAG bucket | `task bucket:report` |
| Inspect index from MCP | `mcp__hashicorp_rag__get_index_info` |
| Inspect graph from MCP | `mcp__hashicorp_rag__get_graph_info` |

### Investigating a failed graph run

1. `task graph:status` to find the failing execution.
2. Open the Step Functions execution in the Console.
3. Identify the failed Map iteration or CodeBuild step. Click into it to find the CodeBuild build ID.
4. Open CodeBuild → Build history → that build, and read the phase logs:
   - `install-terraform` failures: usually transient `releases.hashicorp.com` 5xx — re-trigger.
   - `clone-workspace` failures: the workspace repo is private or the URL is wrong. Add a deploy key or fix the URL.
   - `terraform-graph` failures: missing provider plugin or backend block that doesn't strip cleanly. Check the strip-backend regex in `codebuild/buildspec_graph.yml`.
   - `ingest-graph` failures: usually IAM or VPC connectivity. The CodeBuild role needs `neptune-db:connect` and `neptune-db:*` data actions on the cluster ARN, and the security group must allow port 8182.

### Common errors

| Error | Cause | Fix |
|---|---|---|
| `graph_repo_uris is empty` | The variable is empty | Set `graph_repo_uris` in tfvars and re-apply |
| `neptune-db:connect AccessDeniedException` | CodeBuild role missing Neptune IAM permissions | Re-run `task apply` |
| `Connection refused on port 8182` | CodeBuild security group cannot reach Neptune | Verify VPC config and security group rules in Terraform |
| `SigV4 signature mismatch (403)` | `ingest_graph.py` using `json=` body instead of form-encoded `data=` | Use `data={"query": ..., "parameters": json.dumps(...)}` in `requests.post()` |
| `No resource nodes found` | `terraform graph` produced DOT with no matching nodes | Check that the cloned repo has `.tf` files with real resources |
| `Neptune cluster unavailable` | Cluster is `creating` or `modifying` | Wait for `available` status in the Neptune console |
| `parse error in buildspec` | Malformed YAML in `buildspec_graph.yml` | Validate the buildspec syntax |

### Re-ingesting a single repo

The pipeline is authoritative per `repo_uri`: each ingestion run deletes existing nodes/edges for that repo before inserting fresh data. To force a clean re-ingest of one repo, trigger the state machine with just that repo in `graph_repo_uris`:

```bash
task graph:populate GRAPH_REPO_URIS="https://github.com/org/single-repo"
```

### Neptune query access

Neptune does not expose a public endpoint. Options for querying from outside the VPC:

- **Neptune proxy (recommended)**: Deploy the Lambda proxy (`neptune_create_proxy = true`), then use the proxy URL. No VPC connectivity needed.
- **SSH tunnel**: `ssh -L 8182:<NEPTUNE_ENDPOINT>:8182 bastion-host`.
- **AWS Client VPN**: Connect to the VPC, then use the Neptune endpoint directly.

The MCP server uses SigV4-signed HTTP POST. Ensure `NEPTUNE_IAM_AUTH=true` and that credentials have `neptune-db:connect` and `neptune-db:ReadDataViaQuery` permissions.

### Cost notes

- Neptune is the only continuously-billed graph resource. A single `db.r6g.large` instance is roughly **$254/month**.
- To pause Neptune billing, set `create_neptune = false` and run `task apply` — the cluster and associated VPC resources are destroyed (subject to `neptune_deletion_protection`; set to `false` first if needed).
- DOT snapshots are stored in the graph staging bucket with a lifecycle delete policy.

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
