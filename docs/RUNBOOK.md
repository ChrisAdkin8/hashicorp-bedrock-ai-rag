# Operational Runbook — HashiCorp Kendra RAG Pipeline

This runbook covers day-to-day operations, failure diagnosis, and recovery procedures for the HashiCorp RAG pipeline running on Amazon Kendra and Amazon Bedrock.

---

## Quick Links

| Resource | Link pattern |
|---|---|
| Step Functions | `https://console.aws.amazon.com/states/home?region=REGION#/statemachines` |
| CodeBuild history | `https://console.aws.amazon.com/codesuite/codebuild/projects?region=REGION` |
| CloudWatch Logs (CodeBuild) | `https://console.aws.amazon.com/cloudwatch/home?region=REGION#logsV2:log-groups/log-group/$252Faws$252Fcodebuild$252Frag-hashicorp-pipeline` |
| Amazon Kendra | `https://console.aws.amazon.com/kendra/home?region=REGION` |
| S3 RAG bucket | `https://s3.console.aws.amazon.com/s3/buckets/hashicorp-rag-docs-REGION-SUFFIX` |

Replace `REGION` with your deployment region (default: `us-east-1`).

---

## Regular Operations

### Trigger a pipeline run manually

```bash
task pipeline:run
# or with explicit IDs if terraform output is unavailable:
task pipeline:run KENDRA_INDEX_ID=<INDEX_ID> KENDRA_DS_ID=<DATA_SOURCE_ID>
```

### Check pipeline status

```bash
aws stepfunctions list-executions \
  --state-machine-arn $(terraform -chdir=terraform output -raw state_machine_arn) \
  --max-results 5
```

### Validate retrieval quality

```bash
task pipeline:test KENDRA_INDEX_ID=$(terraform -chdir=terraform output -raw kendra_index_id)
```

### Re-ingest after adding new content sources

1. Update `codebuild/scripts/clone_repos.sh` or the appropriate fetch script.
2. Update `codebuild/scripts/process_docs.py` if the new repo requires a custom `docs_subdirs` entry.
3. Commit and push.
4. Run `task pipeline:run` to trigger a full re-ingest.

---

## Failure Diagnosis

### CodeBuild build failed

1. Open CloudWatch Logs → `/aws/codebuild/rag-hashicorp-pipeline` and find the failing build stream.
2. Common causes:
   - **Git clone failure** — transient network issue; re-run the pipeline.
   - **Python import error** — missing dependency; add to `codebuild/scripts/requirements.txt`.
   - **S3 upload permission denied** — verify the CodeBuild IAM role has `s3:PutObject` on the RAG bucket.
   - **GitHub rate limit** — unauthenticated API limit (60 req/hr); add `GITHUB_TOKEN` to Secrets Manager.
   - **No markdown files found for a product** — the repo may have changed its docs directory layout; update `docs_subdirs` in `process_docs.py`.

### Kendra sync failed

1. In the Step Functions console, find the failed `StartSync` or `ListSyncJobs` state and expand its output for the error.
2. In the Kendra console → your index → Data sources → sync history, expand the failed sync for `FailureReasons`.
3. Common causes:
   - **ResourceNotFoundException on data source** — the `kendra_data_source_id` Terraform output may be stale. Run `terraform -chdir=terraform output` and verify `kendra_data_source_id` differs from `kendra_index_id`. If they match, the `split()` index in `main.tf` is wrong — check the fix in commit history.
   - **AccessDeniedException** — the Kendra S3 role lacks `s3:GetObject` or `s3:ListBucket` on the RAG bucket.
   - **Invalid metadata sidecar** — a `.metadata.json` file is malformed. Check `generate_metadata.py` output.
   - **Index capacity exceeded** — the index has hit the edition document limit (10,000 for Developer, 100,000/SCU for Enterprise). Check the Kendra console → index metrics.

### Zero retrieval results

1. Run `task pipeline:test KENDRA_INDEX_ID=<INDEX_ID>` — check which topics return 0 results.
2. Verify the last Kendra sync succeeded: Kendra console → index → Data sources → last sync status.
3. Check that the S3 bucket contains documents for the missing product:
   ```bash
   aws s3 ls s3://$(terraform -chdir=terraform output -raw rag_bucket_name)/documentation/ --region us-east-1
   ```
4. If a product folder is missing, the CodeBuild job found no markdown in its docs path — check CloudWatch Logs for `No markdown files found in <repo>`.

### Kendra index at capacity (Developer Edition)

The Developer Edition is capped at 10,000 documents. Switch to Enterprise Edition:

1. Change `kendra_edition` default to `ENTERPRISE_EDITION` in `terraform/variables.tf`.
2. Run `terraform -chdir=terraform apply -auto-approve` — Terraform will destroy and recreate the Kendra index (10–30 minutes).
3. Re-run `task pipeline:run` to re-sync all documents into the new index.

### Zero blog files in S3

1. Check CloudWatch Logs for `fetch_blogs.py complete — 0 files written`.
2. `fetch_blogs.py` reads content from RSS/Atom feed inline tags — it does **not** scrape article URLs. hashicorp.com is Cloudflare-protected and cannot be scraped directly.
3. If the feed URLs themselves are failing: check `https://www.hashicorp.com/blog/feed.xml` and `https://medium.com/feed/hashicorp-engineering` return HTTP 200. Both are public and unauthenticated.
4. If `lxml` is missing (rare): verify `lxml>=5.0` is in `codebuild/scripts/requirements.txt` — BeautifulSoup's `"xml"` parser requires it. A missing `lxml` causes a `FeatureNotFound` exception logged at ERROR level.
5. CDKTF filtering threshold: posts with ≥3 CDKTF mentions in the body are skipped. If a legitimate post is being dropped, lower the threshold in `fetch_blogs.py`.

### EventBridge Scheduler not triggering

1. Check the scheduler: `aws scheduler get-schedule --name rag-weekly-refresh`.
2. Verify the scheduler IAM role has `states:StartExecution` on the state machine ARN.
3. Look for `SchedulerInvocationError` events in CloudWatch.

---

## Cost Management

### Pause the pipeline (stop weekly refresh)

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

This destroys all Terraform-managed resources including the Kendra index, S3 bucket, CodeBuild project, Step Functions state machine, and EventBridge scheduler.

> **Note:** Unlike Bedrock Knowledge Bases, the Kendra index **is** managed by Terraform. `task destroy` removes everything.

---

## Adding a GitHub Token

Store the token in Secrets Manager so CodeBuild can raise the GitHub API rate limit from 60 to 5,000 requests/hour:

```bash
aws secretsmanager create-secret \
  --name github-token \
  --secret-string '{"token":"ghp_..."}' \
  --tags Key=Project,Value=hashicorp-rag-pipeline
```

Uncomment the `secrets-manager` block in `codebuild/buildspec.yml` to activate it. No other changes are required.

---

## Updating the Pipeline

### Terraform changes

```bash
task plan    # review proposed changes
task apply   # apply
```

### CodeBuild script changes

Commit and push. The next pipeline run will pick up the latest scripts — CodeBuild clones this repository fresh on every build using the `REPO_URL` environment variable.

### Adjusting content exclusions

CDKTF content is excluded at the script level. To modify the exclusion rules:

- **Docs paths** — edit `CDKTF_EXCLUDE_RE` in `process_docs.py`
- **Blog posts** — edit `_CDKTF_RE` and the body mention threshold (currently 3) in `fetch_blogs.py`
- **Discuss threads / GitHub issues** — edit `_CDKTF_RE` in `fetch_discuss.py` / `fetch_github_issues.py`

### Adding new product documentation

For products whose docs live in `hashicorp/web-unified-docs` (Vault, Consul, Nomad, Terraform Enterprise, HCP Terraform):
1. Add a REPO_CONFIG entry in `process_docs.py` with `"repo_dir": "web-unified-docs"` and the correct `docs_subdirs` path (e.g. `["content/myproduct"]`).
2. Commit, push, and run `task pipeline:run`.

For products with their own GitHub repo:
1. Add the repo to `clone_repos.sh` CORE_REPOS.
2. Add a config entry in `process_docs.py` REPO_CONFIG with the appropriate `docs_subdirs`.
3. Commit, push, and run `task pipeline:run`.
