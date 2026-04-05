# Architecture — HashiCorp Bedrock RAG Pipeline

## Overview

A production-grade pipeline that ingests HashiCorp documentation into an Amazon Bedrock Knowledge Base for use as a grounding source in AI coding assistants. The pipeline runs weekly via EventBridge Scheduler and self-heals from transient failures via Step Functions retry logic.

## Components

### Orchestration Layer

| Component | Role |
|---|---|
| **EventBridge Scheduler** | Cron trigger — fires weekly, passes input JSON to Step Functions |
| **Step Functions** | Pipeline orchestrator — 5-state ASL machine: Init → StartBuild → StartIngestionJob → WaitForIngestion → ValidateRetrieval |

### Data Ingestion

| Component | Role |
|---|---|
| **AWS CodeBuild** | Runs data processing scripts inside an isolated Linux container (7 GB, 4 vCPU) |
| **Amazon S3** | Staging area for processed markdown documents and metadata sidecar files |

### Knowledge Base

| Component | Role |
|---|---|
| **Amazon Bedrock Knowledge Base** | Managed RAG service — owns the embedding pipeline and retrieval API |
| **Amazon OpenSearch Serverless** | Vector store — stores embeddings for semantic search |
| **Titan Embeddings V2** | Embedding model — 1024-dimension dense vectors |

### Supporting Infrastructure

| Component | Role |
|---|---|
| **IAM Roles** | Least-privilege execution roles for each service |
| **CloudWatch Logs** | Build logs from CodeBuild; Step Functions execution logs |
| **SNS + CloudWatch Alarms** | Optional email alerts on pipeline failure (set `notification_email`) |
| **GitHub Actions (OIDC)** | CI/CD via OIDC federation — no long-lived IAM keys |

## Data Flow

```
EventBridge Scheduler
    │  weekly cron (cron(0 2 ? * SUN *))
    ▼
Step Functions: Init
    │  inject validation queries + params
    ▼
Step Functions: StartBuild
    │  codebuild:startBuild.sync (auto-polls via CloudWatch Events)
    ▼
CodeBuild
    ├── pre_build: clone_repos.sh, discover_modules.py, clone_modules.sh
    ├── build:     process_docs.py | fetch_github_issues.py & fetch_discuss.py & fetch_blogs.py
    │              deduplicate.py | generate_metadata.py
    └── post_build: aws s3 sync → S3 bucket
    ▼
Step Functions: StartIngestionJob
    │  bedrockagent:startIngestionJob
    ▼
Step Functions: WaitForIngestion (poll loop, 30s interval)
    │  bedrockagent:getIngestionJob until COMPLETE
    ▼
Bedrock Knowledge Base
    │  chunk (FIXED_SIZE, 1024 tokens, 20% overlap)
    │  embed (Titan Embeddings V2, 1024 dims)
    └─ index (OpenSearch Serverless, HNSW)
    ▼
Step Functions: ValidateRetrieval
    │  Map state — 10 parallel queries covering all product families
    └─ bedrockagentruntime:retrieve (HYBRID search, top 5)
    ▼
PipelineComplete
```

## IAM Design

Each service has a dedicated least-privilege execution role:

| Role | Principals | Key permissions |
|---|---|---|
| `bedrock-kb-hashicorp-rag` | `bedrock.amazonaws.com` | `s3:GetObject`, `bedrock:InvokeModel`, `aoss:APIAccessAll` |
| `rag-pipeline-codebuild` | `codebuild.amazonaws.com` | `s3:PutObject/GetObject/ListBucket/DeleteObject`, `logs:PutLogEvents`, `secretsmanager:GetSecretValue` |
| `rag-pipeline-step-functions` | `states.amazonaws.com` | `codebuild:StartBuild/BatchGetBuilds`, `bedrock:StartIngestionJob/GetIngestionJob/Retrieve` |
| `rag-pipeline-scheduler` | `scheduler.amazonaws.com` | `states:StartExecution` |
| `github-actions-terraform` | GitHub Actions OIDC | Terraform state read/write, infra describe |

## Chunking Strategy

Documents go through two chunking stages:

1. **Semantic pre-splitting** (`process_docs.py`): markdown files split at `##`/`###` heading boundaries. Each section becomes a separate file with its own metadata attribution prefix. Sections under 200 chars are merged with the previous section. Sections over ~4000 chars are split at code-fence boundaries.

2. **Bedrock fixed-size chunking**: The knowledge base data source is configured with `FIXED_SIZE` chunking (1024 tokens max, 20% overlap). Because files are already semantically split, the fixed-size chunker rarely cuts within a semantic unit.

## State Machine Design

The Step Functions ASL uses:
- `.sync` integration for CodeBuild (automatic poll via CloudWatch Events — no sleep loop)
- Manual poll loop for Bedrock ingestion (no native `.sync` for `StartIngestionJob`)
- `Map` state with `MaxConcurrency: 5` for parallel validation queries
- Retry on `States.TaskFailed` for the CodeBuild step (2 retries, 30s interval, 2x backoff)
- Catch-all error states for CodeBuild and ingestion failures
