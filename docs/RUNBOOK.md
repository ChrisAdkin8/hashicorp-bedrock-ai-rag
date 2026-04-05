# Operational Runbook — HashiCorp Bedrock RAG Pipeline

This runbook covers day-to-day operations, failure diagnosis, and recovery procedures for the HashiCorp RAG pipeline running on Amazon Bedrock.

---

## Quick Links

| Resource | Link pattern |
|---|---|
| Step Functions | `https://console.aws.amazon.com/states/home?region=REGION#/statemachines` |
| CodeBuild history | `https://console.aws.amazon.com/codesuite/codebuild/projects?region=REGION` |
| CloudWatch Logs (CodeBuild) | `https://console.aws.amazon.com/cloudwatch/home?region=REGION#logsV2:log-groups/log-group/$252Faws$252Fcodebuild$252Frag-hashicorp-pipeline` |
| Bedrock Knowledge Base | `https://console.aws.amazon.com/bedrock/home?region=REGION#/knowledge-bases` |
| OpenSearch Serverless | `https://console.aws.amazon.com/aos/home?region=REGION#/collections` |

Replace `REGION` with your deployment region (default: `us-west-2`).

---

## Regular Operations

### Trigger a pipeline run manually

```bash
task pipeline:run
# or with explicit IDs if tf output unavailable:
task pipeline:run KB_ID=ABCDEFGHIJ DS_ID=KLMNOPQRST
```

### Check pipeline status

```bash
aws stepfunctions list-executions \
  --state-machine-arn arn:aws:states:REGION:ACCOUNT_ID:stateMachine:rag-hashicorp-pipeline \
  --max-results 5
```

### Validate retrieval quality

```bash
task pipeline:test KB_ID=ABCDEFGHIJ
```

### Re-ingest after adding new content sources

1. Update `codebuild/scripts/clone_repos.sh` or the appropriate fetch script.
2. Commit and push.
3. Run `task pipeline:run` to trigger a full re-ingest.

---

## Failure Diagnosis

### CodeBuild build failed

1. Check the CloudWatch Logs group `/aws/codebuild/rag-hashicorp-pipeline`.
2. Common causes:
   - **Git clone failure** — transient network issue; re-run the pipeline.
   - **Python import error** — missing dependency in `codebuild/scripts/requirements.txt`.
   - **S3 upload permission denied** — verify the CodeBuild IAM role has `s3:PutObject` on the RAG bucket.
   - **GitHub rate limit** — unauthenticated API limit (60 req/hr). Add `GITHUB_TOKEN` to Secrets Manager.

### Bedrock ingestion job failed

1. In the Step Functions console, expand the `GetIngestionStatus` state output to find `IngestionJob.FailureReasons`.
2. Common causes:
   - **S3 object format** — Bedrock rejects non-UTF-8 files. Check for binary files in the RAG bucket.
   - **Vector index missing** — the OpenSearch index was not created before the first ingestion. Run `create_knowledge_base.py` again.
   - **IAM permission** — the Bedrock KB role needs `aoss:APIAccessAll` on the collection. Verify with `task output`.

### Zero retrieval results

1. Run `task pipeline:test KB_ID=ABCDEFGHIJ` — check which topics return 0 results.
2. Verify the ingestion job completed successfully (check Bedrock console → Knowledge Base → Sync jobs).
3. Check that the S3 bucket contains documents: `aws s3 ls s3://BUCKET_NAME/ --recursive | head -20`.
4. If the bucket is empty, the CodeBuild build may have run but the upload step failed — check CloudWatch Logs.

### EventBridge Scheduler not triggering

1. Check the scheduler: `aws scheduler get-schedule --name rag-weekly-refresh`.
2. Verify the scheduler IAM role has `states:StartExecution` on the state machine.
3. Check for failed targets in CloudWatch: look for `SchedulerInvocationError` events.

---

## Cost Management

### Pause the pipeline (stop weekly refresh)

```bash
aws scheduler update-schedule \
  --name rag-weekly-refresh \
  --state DISABLED \
  --schedule-expression "cron(0 2 ? * SUN *)" \
  --flexible-time-window '{"Mode":"OFF"}' \
  --target '{"Arn":"STATE_MACHINE_ARN","RoleArn":"SCHEDULER_ROLE_ARN"}'
```

### Delete the Knowledge Base (stops OpenSearch OCU billing)

```bash
# WARNING: This deletes all indexed content. Re-run the pipeline to rebuild.
aws bedrock-agent delete-knowledge-base --knowledge-base-id ABCDEFGHIJ
```

OpenSearch Serverless collections continue billing (~$350/mo) even with no KB attached. Delete the collection in the console or via Terraform to stop all vector store billing.

### Full teardown

```bash
task destroy
```

This destroys all Terraform-managed resources. The Knowledge Base is **not** managed by Terraform and must be deleted separately (see above).

---

## Adding a GitHub Token

Store the token in Secrets Manager and tag it so the CodeBuild IAM policy can access it:

```bash
aws secretsmanager create-secret \
  --name github-token \
  --secret-string '{"token":"ghp_..."}' \
  --tags Key=Project,Value=hashicorp-rag-pipeline
```

The `buildspec.yml` already references `github-token:token` in its `secrets-manager` block. No other changes are required.

---

## Updating the Pipeline

### Terraform changes

```bash
task plan    # review proposed changes
task apply   # apply
```

### CodeBuild script changes

Commit and push. The next pipeline run will pick up the latest scripts from the repository (CodeBuild clones the repo on every build).

### Modifying chunking parameters

Edit `chunk_size` and `chunk_overlap_pct` in `terraform/terraform.tfvars`, then:

```bash
task plan && task apply   # updates the data source configuration
task pipeline:run         # re-ingest with new chunking
```
