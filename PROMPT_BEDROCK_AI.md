# PROMPT_BEDROCK_AI.md — HashiCorp RAG Pipeline Infrastructure (AWS / Bedrock)

> **Note:** This file documents the AWS equivalent of the Vertex AI RAG pipeline described in `PROMPT_VERTEX_AI.md`. It maps every GCP resource and pattern to its AWS counterpart and provides a complete blueprint for deploying the same HashiCorp documentation RAG system on Amazon Web Services using Amazon Bedrock Knowledge Bases.

---

## Project Overview

A production-grade repository that provisions and operates a Retrieval-Augmented Generation (RAG) system on Amazon Web Services. The system ingests HashiCorp's public documentation from GitHub repositories and the Terraform Registry API into an Amazon Bedrock Knowledge Base, and keeps it current via automated weekly refresh.

A user can clone this repo, set a handful of variables, run `task up`, and have a fully operational RAG pipeline.

---

## Architecture

```
Amazon EventBridge Scheduler (weekly cron)
        │
        ▼
AWS Step Functions (orchestrator)
        │
        ├──► AWS CodeBuild (inline submission via SDK — no webhook trigger)
        │         clone repos → discover modules → process markdown → upload to S3
        │
        ├──► Amazon Bedrock Knowledge Base (sync data source from S3)
        │
        └──► Validation (retrieve query to confirm knowledge base health)
```

All infrastructure is provisioned by Terraform. Data processing runs inside CodeBuild. Step Functions orchestrates the end-to-end pipeline. EventBridge Scheduler triggers it on a cron schedule.

**Key design decision:** The state machine starts CodeBuild projects directly via the `codebuild:StartBuild` SDK integration. There is no CodePipeline resource and no GitHub App connection required for public repositories.

### GCP-to-AWS Resource Mapping

| GCP Resource | AWS Equivalent | Notes |
|---|---|---|
| Cloud Scheduler | Amazon EventBridge Scheduler | Cron-based trigger, invokes Step Functions |
| Cloud Workflows | AWS Step Functions | Amazon States Language (ASL) JSON definition |
| Cloud Build | AWS CodeBuild | `buildspec.yml` replaces `cloudbuild.yaml` |
| GCS Bucket | Amazon S3 Bucket | Staging area for processed documents |
| Vertex AI RAG Engine | Amazon Bedrock Knowledge Base | Managed RAG with automatic chunking and embedding |
| Vertex AI Corpus | Bedrock Knowledge Base + Data Source | KB wraps the vector store; data source points to S3 |
| text-embedding-005 | Amazon Titan Embeddings V2 (`amazon.titan-embed-text-v2:0`) | 1024-dimension embeddings; Cohere Embed v3 is an alternative |
| Managed Spanner (vector store) | Amazon OpenSearch Serverless | Serverless vector search collection; Aurora PostgreSQL with pgvector is an alternative |
| Cloud Monitoring | Amazon CloudWatch | Alarms, dashboards, and log groups |
| Service Account | IAM Role | Execution roles for Step Functions, CodeBuild, and Bedrock |
| Workload Identity Federation | OIDC Provider + IAM Role | GitHub Actions assumes role via OIDC — no long-lived keys |
| `google_project_service` (API enablement) | Not required | AWS services are available by default; no API enablement step |

### Data Flow — Two Parallel Tracks

The pipeline ingests content from two parallel tracks inside CodeBuild:

1. **Git Clone Track:** Shallow-clones HashiCorp repos (9 core, 14 providers, dynamically-discovered modules, 4 sentinel), runs semantic section splitting via `process_docs.py`, and writes metadata-enriched markdown to the build workspace.

2. **API Fetch Track:** Runs in parallel with git cloning. Three scripts (`fetch_github_issues.py`, `fetch_discuss.py`, `fetch_blogs.py`) query external APIs and write cleaned output to the build workspace.

Both tracks converge at a single `aws s3 sync` upload step, after which Step Functions calls the Bedrock `StartIngestionJob` API.

### Chunking Strategy

Documents are processed through a two-stage chunking pipeline:

1. **Semantic pre-splitting** (`process_docs.py`): Documents are split at `##` and `###` heading boundaries before upload. Each section becomes a self-contained file with its own metadata header. Sections smaller than 200 characters are merged with the previous section. Multi-section documents are written as `{stem}_s0.md`, `{stem}_s1.md`, etc.

2. **Fixed-size chunking** (Bedrock Knowledge Base): Bedrock's built-in chunker (1024 tokens max, 200 token overlap) operates on the pre-split sections. Because each input file is already a coherent content unit, the chunker rarely splits within a section.

3. **Code block integrity** (`process_docs.py`): After semantic splitting, sections exceeding ~4000 characters (approximately 1024 tokens) are further split at code block boundaries rather than at arbitrary positions. This ensures fenced code blocks (HCL configurations, CLI examples) are never split mid-block by the downstream fixed-length chunker. Sections without code fences or below the threshold are left intact.

### Cross-Source Deduplication

After all data processing scripts complete and before the S3 upload, `deduplicate.py` removes near-duplicate files across sources. It extracts the body text (ignoring metadata headers), normalises whitespace and case, computes a SHA-256 hash, and removes files whose body matches a previously seen file. Files shorter than 100 characters are excluded from dedup (too short to be meaningful duplicates). Files are processed in sorted path order for determinism — the first file encountered wins.

This prevents the same content from entering the corpus through multiple sources (e.g., a Vault feature described in both official docs and a blog post announcement).

**Bedrock chunking configuration:** Set via the `chunkingConfiguration` parameter when creating or updating the data source:
```json
{
  "chunkingStrategy": "FIXED_SIZE",
  "fixedSizeChunkingConfiguration": {
    "maxTokens": 1024,
    "overlapPercentage": 20
  }
}
```

Alternatively, Bedrock supports `SEMANTIC` and `HIERARCHICAL` chunking strategies. The `FIXED_SIZE` strategy is used here because semantic pre-splitting is already handled by `process_docs.py`, and we want deterministic chunk boundaries.

---

## Repository Layout

```
.
├── Taskfile.yml                        # Primary entry point (task up / task pipeline:run / etc.)
├── AGENTS.md                           # Operational guide for Claude Code / AI agents
├── PROMPT_BEDROCK_AI.md                # This file — implementation reference
├── README.md
├── .gitignore
├── .github/
│   └── workflows/
│       └── terraform.yml               # CI: fmt check, validate, Trivy scan (OIDC auth)
├── docs/
│   ├── ARCHITECTURE.md
│   ├── MCP_SERVER.md
│   ├── RUNBOOK.md
│   └── diagrams/
│       ├── architecture.svg
│       └── ingestion_pipeline.svg
├── terraform/
│   ├── versions.tf                     # Provider constraints + S3 backend
│   ├── variables.tf                    # Input variables
│   ├── main.tf                         # All AWS resources
│   ├── outputs.tf
│   ├── terraform.tfvars                # gitignored — created by deploy.sh or manually
│   └── terraform.tfvars.example
├── mcp/
│   ├── server.py                       # MCP server — exposes knowledge base as Claude Code tools
│   ├── test_server.py                  # Smoke tests for MCP server tool functions
│   └── requirements.txt               # mcp, boto3
├── step-functions/
│   └── rag_pipeline.asl.json           # Step Functions state machine definition (ASL)
├── codebuild/
│   ├── buildspec.yml                   # CodeBuild build specification
│   └── scripts/
│       ├── clone_repos.sh              # Clone HashiCorp GitHub repos in parallel
│       ├── discover_modules.py         # Query Terraform Registry for module repos
│       ├── process_docs.py             # Extract and clean markdown from cloned repos
│       ├── fetch_github_issues.py      # Fetch GitHub issues for context
│       ├── fetch_discuss.py            # Fetch HashiCorp Discuss forum posts
│       ├── fetch_blogs.py              # Fetch HashiCorp blog posts
│       ├── deduplicate.py              # Remove near-duplicate files before upload
│       ├── generate_metadata.py        # Generate metadata.jsonl sidecar files for S3 objects
│       ├── requirements.txt            # pyyaml, requests, pytest, beautifulsoup4
│       └── tests/
│           ├── __init__.py
│           ├── test_process_docs.py
│           ├── test_fetch_github_issues.py
│           └── test_deduplicate.py
└── scripts/
    ├── deploy.sh                       # End-to-end deploy orchestrator (called by task up)
    ├── bootstrap_state.sh              # Create S3 state bucket + DynamoDB lock table (one-time)
    ├── create_knowledge_base.py        # Create Bedrock Knowledge Base (uses boto3)
    ├── run_pipeline.sh                 # Trigger Step Functions execution via AWS CLI
    ├── setup_claude_bedrock.sh         # Configure Claude Code for Amazon Bedrock backend
    ├── setup_mcp.sh                    # Register MCP server with Claude Code settings
    ├── test_retrieval.py               # Validate knowledge base retrieval quality
    └── test_token_efficiency.py        # Compare RAG token cost vs raw documentation
```

---

## Terraform Implementation

### terraform/versions.tf

```hcl
terraform {
  required_version = ">= 1.5, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }

  backend "s3" {
    # Bucket and DynamoDB table supplied at init time via -backend-config
    # Run scripts/bootstrap_state.sh to create them first.
    key = "terraform/state/rag-pipeline/terraform.tfstate"
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "hashicorp-rag-pipeline"
      ManagedBy = "terraform"
    }
  }
}
```

**S3 backend with DynamoDB locking:** Unlike GCS (which has built-in object locking), the S3 backend requires a separate DynamoDB table for state locking. The `bootstrap_state.sh` script creates both.

### terraform/variables.tf

| Variable | Type | Default | Description |
|---|---|---|---|
| `region` | string | `"us-west-2"` | AWS region for all resources |
| `knowledge_base_name` | string | `"hashicorp-knowledge-base"` | Bedrock Knowledge Base display name |
| `knowledge_base_id` | string | `""` | Bedrock Knowledge Base ID (populated by deploy.sh) |
| `data_source_id` | string | `""` | Bedrock Data Source ID (populated by deploy.sh) |
| `refresh_schedule` | string | `"cron(0 2 ? * SUN *)"` | EventBridge cron expression (UTC) |
| `repo_uri` | string | (required) | GitHub HTTPS URL of this repo |
| `chunk_size` | number | `1024` | Max tokens per chunk |
| `chunk_overlap_pct` | number | `20` | Chunk overlap as percentage (Bedrock uses percentage, not absolute tokens) |
| `embedding_model_arn` | string | `"arn:aws:bedrock:<region>::foundation-model/amazon.titan-embed-text-v2:0"` | Bedrock embedding model ARN |
| `notification_email` | string | `""` | Email for CloudWatch alarm notifications |
| `collection_name` | string | `"hashicorp-rag-vectors"` | OpenSearch Serverless collection name |

**Important:** `rag_bucket_name` is computed in `locals`:
```hcl
locals {
  account_id      = data.aws_caller_identity.current.account_id
  rag_bucket_name = "hashicorp-rag-docs-${substr(sha256(local.account_id), 0, 8)}"
}

data "aws_caller_identity" "current" {}
```

### terraform/main.tf — Resource Inventory

**S3 Bucket** (`aws_s3_bucket.rag_docs` + associated resources):
```hcl
resource "aws_s3_bucket" "rag_docs" {
  bucket        = local.rag_bucket_name
  force_destroy = false
}

resource "aws_s3_bucket_versioning" "rag_docs" {
  bucket = aws_s3_bucket.rag_docs.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "rag_docs" {
  bucket = aws_s3_bucket.rag_docs.id
  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "rag_docs" {
  bucket = aws_s3_bucket.rag_docs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "rag_docs" {
  bucket                  = aws_s3_bucket.rag_docs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
```

**OpenSearch Serverless Collection** (vector store):
```hcl
resource "aws_opensearchserverless_collection" "vectors" {
  name = var.collection_name
  type = "VECTORSEARCH"
}

resource "aws_opensearchserverless_security_policy" "encryption" {
  name = "${var.collection_name}-encryption"
  type = "encryption"
  policy = jsonencode({
    Rules = [{
      ResourceType = "collection"
      Resource     = ["collection/${var.collection_name}"]
    }]
    AWSOwnedKey = true
  })
}

resource "aws_opensearchserverless_security_policy" "network" {
  name = "${var.collection_name}-network"
  type = "network"
  policy = jsonencode([{
    Rules = [{
      ResourceType = "collection"
      Resource     = ["collection/${var.collection_name}"]
    }, {
      ResourceType = "dashboard"
      Resource     = ["collection/${var.collection_name}"]
    }]
    AllowFromPublic = true
  }])
}

resource "aws_opensearchserverless_access_policy" "data" {
  name = "${var.collection_name}-data"
  type = "data"
  policy = jsonencode([{
    Rules = [{
      ResourceType = "index"
      Resource     = ["index/${var.collection_name}/*"]
      Permission   = [
        "aoss:CreateIndex",
        "aoss:UpdateIndex",
        "aoss:DescribeIndex",
        "aoss:ReadDocument",
        "aoss:WriteDocument"
      ]
    }, {
      ResourceType = "collection"
      Resource     = ["collection/${var.collection_name}"]
      Permission   = [
        "aoss:CreateCollectionItems",
        "aoss:UpdateCollectionItems",
        "aoss:DescribeCollectionItems"
      ]
    }]
    Principal = [aws_iam_role.bedrock_kb.arn]
  }])
}
```

**IAM Roles:**

*Bedrock Knowledge Base execution role:*
```hcl
resource "aws_iam_role" "bedrock_kb" {
  name = "bedrock-kb-hashicorp-rag"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = local.account_id
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "bedrock_kb" {
  name = "bedrock-kb-policy"
  role = aws_iam_role.bedrock_kb.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [
          aws_s3_bucket.rag_docs.arn,
          "${aws_s3_bucket.rag_docs.arn}/*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = [var.embedding_model_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["aoss:APIAccessAll"]
        Resource = [aws_opensearchserverless_collection.vectors.arn]
      }
    ]
  })
}
```

*CodeBuild execution role:*
```hcl
resource "aws_iam_role" "codebuild" {
  name = "rag-pipeline-codebuild"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "codebuild.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "codebuild" {
  name = "codebuild-policy"
  role = aws_iam_role.codebuild.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:ListBucket", "s3:DeleteObject"]
        Resource = [
          aws_s3_bucket.rag_docs.arn,
          "${aws_s3_bucket.rag_docs.arn}/*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/codebuild/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = ["arn:aws:secretsmanager:${var.region}:${local.account_id}:secret:github-token-*"]
        Condition = {
          StringEquals = { "aws:ResourceTag/Project" = "hashicorp-rag-pipeline" }
        }
      }
    ]
  })
}
```

*Step Functions execution role:*
```hcl
resource "aws_iam_role" "step_functions" {
  name = "rag-pipeline-step-functions"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "step_functions" {
  name = "step-functions-policy"
  role = aws_iam_role.step_functions.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["codebuild:StartBuild", "codebuild:BatchGetBuilds"]
        Resource = [aws_codebuild_project.rag_pipeline.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:StartIngestionJob", "bedrock:GetIngestionJob", "bedrock:Retrieve"]
        Resource = ["arn:aws:bedrock:${var.region}:${local.account_id}:knowledge-base/*"]
      },
      {
        Effect = "Allow"
        Action = [
          "events:PutTargets",
          "events:PutRule",
          "events:DescribeRule"
        ]
        Resource = ["arn:aws:events:${var.region}:${local.account_id}:rule/StepFunctionsGetEventsForCodeBuildRule"]
      }
    ]
  })
}
```

**CodeBuild Project:**
```hcl
resource "aws_codebuild_project" "rag_pipeline" {
  name         = "rag-hashicorp-pipeline"
  service_role = aws_iam_role.codebuild.arn

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    compute_type    = "BUILD_GENERAL1_MEDIUM"
    image           = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    type            = "LINUX_CONTAINER"
    privileged_mode = false

    environment_variable {
      name  = "RAG_BUCKET"
      value = aws_s3_bucket.rag_docs.id
    }
  }

  source {
    type            = "GITHUB"
    location        = var.repo_uri
    git_clone_depth = 1
    buildspec       = "codebuild/buildspec.yml"
  }

  logs_config {
    cloudwatch_logs {
      group_name = "/aws/codebuild/rag-hashicorp-pipeline"
    }
  }

  build_timeout = 120  # minutes
}
```

**Step Functions State Machine:**
```hcl
resource "aws_sfn_state_machine" "rag_pipeline" {
  name     = "rag-hashicorp-pipeline"
  role_arn = aws_iam_role.step_functions.arn

  definition = templatefile("${path.module}/../step-functions/rag_pipeline.asl.json", {
    codebuild_project_name = aws_codebuild_project.rag_pipeline.name
    knowledge_base_id      = var.knowledge_base_id
    data_source_id         = var.data_source_id
    rag_bucket             = aws_s3_bucket.rag_docs.id
    region                 = var.region
  })
}
```

**EventBridge Scheduler:**
```hcl
resource "aws_scheduler_schedule" "rag_weekly_refresh" {
  name       = "rag-weekly-refresh"
  group_name = "default"

  schedule_expression          = var.refresh_schedule
  schedule_expression_timezone = "Europe/London"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_sfn_state_machine.rag_pipeline.arn
    role_arn = aws_iam_role.scheduler.arn

    input = jsonencode({
      knowledge_base_id = var.knowledge_base_id
      data_source_id    = var.data_source_id
      bucket_name       = aws_s3_bucket.rag_docs.id
      chunk_size        = var.chunk_size
      chunk_overlap_pct = var.chunk_overlap_pct
      region            = var.region
      repo_url          = var.repo_uri
    })
  }
}

resource "aws_iam_role" "scheduler" {
  name = "rag-pipeline-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
      }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = "scheduler-policy"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["states:StartExecution"]
      Resource = [aws_sfn_state_machine.rag_pipeline.arn]
    }]
  })
}
```

**CloudWatch Monitoring** (conditional on `notification_email != ""`):
```hcl
resource "aws_sns_topic" "alerts" {
  count = var.notification_email != "" ? 1 : 0
  name  = "rag-pipeline-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.notification_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts[0].arn
  protocol  = "email"
  endpoint  = var.notification_email
}

resource "aws_cloudwatch_metric_alarm" "sfn_failures" {
  count               = var.notification_email != "" ? 1 : 0
  alarm_name          = "rag-pipeline-sfn-failures"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ExecutionsFailed"
  namespace           = "AWS/States"
  period              = 86400
  statistic           = "Sum"
  threshold           = 0
  alarm_actions       = [aws_sns_topic.alerts[0].arn]

  dimensions = {
    StateMachineArn = aws_sfn_state_machine.rag_pipeline.arn
  }
}

resource "aws_cloudwatch_metric_alarm" "codebuild_failures" {
  count               = var.notification_email != "" ? 1 : 0
  alarm_name          = "rag-pipeline-codebuild-failures"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "FailedBuilds"
  namespace           = "AWS/CodeBuild"
  period              = 86400
  statistic           = "Sum"
  threshold           = 0
  alarm_actions       = [aws_sns_topic.alerts[0].arn]

  dimensions = {
    ProjectName = aws_codebuild_project.rag_pipeline.name
  }
}
```

---

## Step Functions — step-functions/rag_pipeline.asl.json

The state machine definition in Amazon States Language (ASL). Five states:

1. **Init** — Pass state that extracts input parameters and sets defaults.

2. **StartBuild** — Task state using the `codebuild:StartBuild` SDK integration (`.sync` pattern for synchronous execution). Passes environment variable overrides to the CodeBuild project. Step Functions automatically polls the build until completion — no manual polling loop required (unlike Cloud Workflows which needs explicit poll_build).

```json
{
  "Type": "Task",
  "Resource": "arn:aws:states:::codebuild:startBuild.sync",
  "Parameters": {
    "ProjectName": "${codebuild_project_name}",
    "EnvironmentVariablesOverride": [
      { "Name": "RAG_BUCKET", "Value.$": "$.bucket_name", "Type": "PLAINTEXT" },
      { "Name": "REPO_URL", "Value.$": "$.repo_url", "Type": "PLAINTEXT" }
    ]
  },
  "ResultPath": "$.buildResult"
}
```

**Key difference from Cloud Workflows:** The `.sync` integration pattern handles polling automatically. Step Functions creates a CloudWatch Events rule to detect build completion and resumes the execution — no `sleep` + `GET` loop needed.

3. **StartIngestionJob** — Task state calling `bedrock:StartIngestionJob`. Triggers a sync of the S3 data source into the Knowledge Base.

```json
{
  "Type": "Task",
  "Resource": "arn:aws:states:::aws-sdk:bedrockagent:startIngestionJob",
  "Parameters": {
    "KnowledgeBaseId.$": "$.knowledge_base_id",
    "DataSourceId.$": "$.data_source_id"
  },
  "ResultPath": "$.ingestionResult"
}
```

4. **WaitForIngestion** — Wait + poll loop checking `bedrock:GetIngestionJob` until status is `COMPLETE` or `FAILED`. Unlike CodeBuild, Bedrock ingestion does not have a native `.sync` integration in Step Functions, so manual polling is required.

```json
{
  "Type": "Task",
  "Resource": "arn:aws:states:::aws-sdk:bedrockagent:getIngestionJob",
  "Parameters": {
    "KnowledgeBaseId.$": "$.knowledge_base_id",
    "DataSourceId.$": "$.data_source_id",
    "IngestionJobId.$": "$.ingestionResult.IngestionJob.IngestionJobId"
  },
  "ResultPath": "$.ingestionStatus"
}
```

5. **ValidateRetrieval** — A `Map` state that iterates over 10 validation queries covering all product families (terraform-provider, vault, consul, nomad, sentinel, packer, terraform-module) and source types (github-issue, discuss-thread, blog-post). Each iteration calls `bedrock:Retrieve` with `NumberOfResults: 5`. Per-query results are collected into a results array. A final `Pass` state counts successes/failures and logs a summary. Zero results for a topic logs a warning via CloudWatch but does NOT fail the pipeline — this ensures operators have visibility into which product areas may have coverage gaps.

```json
{
  "Type": "Map",
  "ItemsPath": "$.validationQueries",
  "ItemSelector": {
    "KnowledgeBaseId.$": "$.knowledge_base_id",
    "Topic.$": "$$.Map.Item.Value.topic",
    "QueryText.$": "$$.Map.Item.Value.query"
  },
  "Iterator": {
    "StartAt": "RunQuery",
    "States": {
      "RunQuery": {
        "Type": "Task",
        "Resource": "arn:aws:states:::aws-sdk:bedrockagentruntime:retrieve",
        "Parameters": {
          "KnowledgeBaseId.$": "$.KnowledgeBaseId",
          "RetrievalQuery": {
            "Text.$": "$.QueryText"
          },
          "RetrievalConfiguration": {
            "VectorSearchConfiguration": {
              "NumberOfResults": 5
            }
          }
        },
        "ResultPath": "$.retrievalResult",
        "End": true
      }
    }
  },
  "ResultPath": "$.validationResults"
}
```

The `validationQueries` array is injected during the Init state and contains the same 10 queries used in the Vertex AI workflow:

| Topic | Query |
|---|---|
| terraform-provider | How do I configure the AWS provider in Terraform? |
| vault | How do I generate dynamic database credentials using HashiCorp Vault? |
| consul | How do I set up mTLS between services using Consul Connect? |
| nomad | How do I define a Nomad job specification for a Docker container? |
| sentinel | How do I write a Sentinel policy to restrict resource creation in Terraform? |
| packer | How do I build an AMI with Packer using an HCL template? |
| terraform-module | What is the structure of a reusable Terraform module? |
| github-issue | What are common issues when upgrading the Terraform AWS provider? |
| discuss-thread | How do I troubleshoot Terraform state locking errors? |
| blog-post | What new features were announced for HashiCorp products? |

---

## CodeBuild Pipeline — codebuild/buildspec.yml

```yaml
version: 0.2

env:
  secrets-manager:
    GITHUB_TOKEN: "github-token:token"

phases:
  install:
    runtime-versions:
      python: 3.12
    commands:
      - pip install -r codebuild/scripts/requirements.txt

  pre_build:
    commands:
      - echo "Cloning HashiCorp repositories..."
      - bash codebuild/scripts/clone_repos.sh
      - python3 codebuild/scripts/discover_modules.py
      - bash codebuild/scripts/clone_modules.sh

  build:
    commands:
      # Git Clone Track (sequential after pre_build)
      - python3 codebuild/scripts/process_docs.py
      # API Fetch Track (can run in parallel via background processes)
      - |
        python3 codebuild/scripts/fetch_github_issues.py &
        python3 codebuild/scripts/fetch_discuss.py &
        python3 codebuild/scripts/fetch_blogs.py &
        wait
      - echo "Deduplicating..."
      - python3 codebuild/scripts/deduplicate.py
      - echo "Generating metadata sidecars..."
      - python3 codebuild/scripts/generate_metadata.py --bucket ${RAG_BUCKET}

  post_build:
    commands:
      - echo "Uploading to S3..."
      - aws s3 sync /codebuild/output/cleaned/ s3://${RAG_BUCKET}/ --delete

cache:
  paths:
    - '/root/.cache/pip/**/*'
```

**Key differences from Cloud Build:**

| Feature | Cloud Build (GCP) | CodeBuild (AWS) |
|---|---|---|
| Parallel steps | Explicit `waitFor: ['-']` | Background processes with `wait` |
| Workspace | `/workspace/` | `/codebuild/output/` (or `$CODEBUILD_SRC_DIR`) |
| Upload | `gsutil -m rsync -r -d` | `aws s3 sync --delete` |
| Secrets | `availableSecrets` + Secret Manager | `env.secrets-manager` block |
| Machine type | No override (default) | `BUILD_GENERAL1_MEDIUM` (7 GB, 4 vCPU) |
| Timeout | Inline in workflow build spec | `build_timeout = 120` (in Terraform, minutes) |
| Logging | `CLOUD_LOGGING_ONLY` | CloudWatch Logs (default) |
| Venv setup | Dedicated `setup-venv` step (explicit `waitFor`) | `install` phase (sequential before `build`) |
| Metadata generation | `generate-metadata` step after `deduplicate`, before upload | `generate_metadata.py` in `build` phase, before `post_build` upload |

---

## Scripts

### scripts/deploy.sh

End-to-end deploy orchestrator. Called by `task up`. Steps:
1. Bootstrap S3 state bucket + DynamoDB lock table (`scripts/bootstrap_state.sh`)
2. `terraform init -backend-config="bucket=..." -backend-config="dynamodb_table=..."` + `terraform apply`
3. Create Knowledge Base (`scripts/create_knowledge_base.py --output-id-only`) → write `kb.auto.tfvars`
4. Second `terraform apply` (auto-loads `kb.auto.tfvars`)
5. Trigger first pipeline run (`scripts/run_pipeline.sh --wait`)

### scripts/bootstrap_state.sh

Creates the S3 state bucket and DynamoDB lock table if they don't exist.

```bash
BUCKET_NAME="${ACCOUNT_ID}-tf-state-$(echo -n "${ACCOUNT_ID}" | sha256sum | cut -c1-8)"
TABLE_NAME="terraform-state-lock"

# Create S3 bucket with versioning and encryption
aws s3api create-bucket \
  --bucket "${BUCKET_NAME}" \
  --region "${REGION}" \
  --create-bucket-configuration LocationConstraint="${REGION}"

aws s3api put-bucket-versioning \
  --bucket "${BUCKET_NAME}" \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption \
  --bucket "${BUCKET_NAME}" \
  --server-side-encryption-configuration '{
    "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"}}]
  }'

# Create DynamoDB table for state locking
aws dynamodb create-table \
  --table-name "${TABLE_NAME}" \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST
```

**Key difference from GCS:** GCS buckets support built-in object locking for Terraform state. S3 requires a separate DynamoDB table for distributed locking.

### scripts/create_knowledge_base.py

Creates an Amazon Bedrock Knowledge Base with an OpenSearch Serverless vector store using boto3.

```python
import boto3

bedrock_agent = boto3.client("bedrock-agent", region_name=region)

# Create the Knowledge Base
response = bedrock_agent.create_knowledge_base(
    name=knowledge_base_name,
    roleArn=bedrock_kb_role_arn,
    knowledgeBaseConfiguration={
        "type": "VECTOR",
        "vectorKnowledgeBaseConfiguration": {
            "embeddingModelArn": embedding_model_arn,
            "embeddingModelConfiguration": {
                "bedrockEmbeddingModelConfiguration": {
                    "dimensions": 1024
                }
            }
        }
    },
    storageConfiguration={
        "type": "OPENSEARCH_SERVERLESS",
        "opensearchServerlessConfiguration": {
            "collectionArn": collection_arn,
            "vectorIndexName": "bedrock-knowledge-base-default-index",
            "fieldMapping": {
                "vectorField": "bedrock-knowledge-base-default-vector",
                "textField": "AMAZON_BEDROCK_TEXT_CHUNK",
                "metadataField": "AMAZON_BEDROCK_METADATA"
            }
        }
    }
)

knowledge_base_id = response["knowledgeBase"]["knowledgeBaseId"]

# Create the S3 Data Source
ds_response = bedrock_agent.create_data_source(
    knowledgeBaseId=knowledge_base_id,
    name="hashicorp-docs-s3",
    dataSourceConfiguration={
        "type": "S3",
        "s3Configuration": {
            "bucketArn": f"arn:aws:s3:::{bucket_name}"
        }
    },
    vectorIngestionConfiguration={
        "chunkingConfiguration": {
            "chunkingStrategy": "FIXED_SIZE",
            "fixedSizeChunkingConfiguration": {
                "maxTokens": 1024,
                "overlapPercentage": 20
            }
        }
    }
)

data_source_id = ds_response["dataSource"]["dataSourceId"]
```

With `--output-id-only`, prints `knowledge_base_id` and `data_source_id` to stdout (used by `deploy.sh` to write `kb.auto.tfvars`).

**Key differences from Vertex AI corpus creation:**
- Bedrock uses boto3 (not a specialised SDK like `vertexai`)
- The Knowledge Base and Data Source are separate resources (vs a single Corpus in Vertex AI)
- Storage configuration (OpenSearch Serverless) must be specified at KB creation time (Vertex AI manages storage internally)
- Chunking configuration is set on the Data Source, not during import

### scripts/run_pipeline.sh

Triggers the Step Functions state machine via the AWS CLI:

```bash
EXECUTION_ARN=$(aws stepfunctions start-execution \
  --state-machine-arn "${STATE_MACHINE_ARN}" \
  --input "${INPUT_JSON}" \
  --query 'executionArn' \
  --output text)

if [[ "${WAIT}" == "true" ]]; then
  aws stepfunctions describe-execution \
    --execution-arn "${EXECUTION_ARN}" \
    --query 'status' \
    --output text
  # Poll until SUCCEEDED, FAILED, TIMED_OUT, or ABORTED
fi
```

**Key difference from Cloud Workflows:** The AWS CLI natively supports `start-execution` with `--input` for passing JSON data. No double-encoding of arguments is required (unlike the Workflows REST API).

### scripts/test_retrieval.py

Runs built-in retrieval test queries against the Knowledge Base using boto3:

```python
bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=region)

response = bedrock_runtime.retrieve(
    knowledgeBaseId=knowledge_base_id,
    retrievalQuery={"text": query_text},
    retrievalConfiguration={
        "vectorSearchConfiguration": {
            "numberOfResults": top_k,
            "overrideSearchType": "HYBRID"
        }
    }
)

results = response.get("retrievalResults", [])
```

**Key difference from Vertex AI:** Bedrock uses `retrieve()` (not `retrieval_query()`). The `overrideSearchType` can be `SEMANTIC` (vector only) or `HYBRID` (vector + keyword). There is no explicit `vector_distance_threshold` parameter — Bedrock returns results sorted by relevance score, and filtering is done client-side.

### scripts/setup_claude_bedrock.sh

Configures Claude Code to use Amazon Bedrock as its backend. AWS equivalent of `setup_claude_vertex.sh`.

**What it does:**
1. Authenticates with AWS (checks for valid credentials via `aws sts get-caller-identity`)
2. Sets environment variables: `CLAUDE_CODE_USE_BEDROCK=1`, `ANTHROPIC_BEDROCK_REGION`, `AWS_REGION`, `ANTHROPIC_MODEL`
3. Optionally persists configuration to `~/.bashrc` (with `--persist` flag, idempotent — checks for existing marker before appending)
4. Verifies Bedrock model access is enabled and `claude` CLI is available

**Options:** `--region` (default: `us-west-2`), `--model` (default: `claude-sonnet-4-20250514`), `--persist`.

**Key difference from GCP version:** AWS authentication uses IAM credentials (environment variables, instance profile, or AWS SSO) — no `gcloud auth` equivalent needed. The `CLAUDE_CODE_USE_BEDROCK=1` flag activates Bedrock routing in the Claude Code CLI.

### scripts/setup_mcp.sh

Registers the HashiCorp RAG MCP server with Claude Code by writing the `mcpServers` entry into `.claude/settings.local.json`. AWS equivalent of the GCP `setup_mcp.sh`, but references `mcp/server.py` configured for the Bedrock backend.

**Arguments:** `--region` (default: `us-west-2`), `--knowledge-base-id` (required).

**Environment variables written:**
- `AWS_REGION` — AWS region
- `AWS_KNOWLEDGE_BASE_ID` — Bedrock Knowledge Base ID

**Prerequisites:** Run `task mcp:install` first to create the venv and install `mcp` and `boto3`.

### scripts/test_token_efficiency.py

Measures token efficiency of RAG retrieval versus pasting raw documentation into context. Works with both Vertex AI and Bedrock deployments (pass `--backend bedrock` to switch from the default `vertexai`).

Runs a set of cross-product queries (e.g., "Vault + Terraform provider", "Consul + Nomad scheduling") and compares:
- **RAG path:** tokens retrieved from the knowledge base (`numberOfResults=5`, 1024-token chunks)
- **Raw path:** estimated tokens if the user pasted the corresponding full documentation pages

Outputs a per-query breakdown and a summary showing average token savings. Typical results: 3,000–5,000 tokens from RAG vs 8,000–12,000+ from raw docs for single-product queries; compounding savings for cross-product queries.

### cloudbuild/scripts/ (shared with Vertex AI version)

The data processing scripts are identical between GCP and AWS deployments:

- **`process_docs.py`** — Semantic section splitting, metadata enrichment (including `last_updated` from git), code block integrity, product taxonomy. No cloud-specific dependencies.
- **`fetch_github_issues.py`** — GitHub API client. Tiered repo filtering, rate limit handling, label denylist filtering, resolution quality scoring. 365-day lookback. No cloud-specific dependencies.
- **`fetch_discuss.py`** — Discourse API client. BeautifulSoup HTML conversion. 365-day lookback. `last_updated` from `last_posted_at`. No cloud-specific dependencies.
- **`fetch_blogs.py`** — HashiCorp blog fetcher. Frequency-weighted product family detection. 365-day lookback. `last_updated` from publication date. No cloud-specific dependencies.
- **`deduplicate.py`** — SHA-256 content deduplication across all sources. Runs after all fetch/process steps, before upload. No cloud-specific dependencies.
- **`generate_metadata.py`** — Generates `metadata.jsonl` sidecar files mapping S3 URIs to `product`, `product_family`, and `source_type` metadata. Runs after deduplication, before upload. No cloud-specific dependencies (bucket name is passed as `--bucket` argument; output paths use `gs://` prefix in GCP, `s3://` in AWS — the script should be invoked with the appropriate bucket URI).
- **`clone_repos.sh`** — Git clone operations. No cloud-specific dependencies.
- **`discover_modules.py`** — Terraform Registry API client. No cloud-specific dependencies.

Only the upload step differs: `gsutil -m rsync` → `aws s3 sync`.

**Quality filters applied to GitHub issues:** body < 100 chars excluded, label denylist (`stale`, `wontfix`, `duplicate`, `invalid`, `spam`) filtering, resolution quality scoring (`high`/`medium`/`low`) based on issue state and maintainer response detection.

**Metadata fields added to all sources:** `last_updated` (YYYY-MM-DD). Issues also include `resolution_quality`.

---

## Bedrock Knowledge Base Configuration

### Embedding Model Options

| Model | Dimensions | Max Tokens | Notes |
|---|---|---|---|
| Amazon Titan Embeddings V2 (`amazon.titan-embed-text-v2:0`) | 1024 (configurable: 256, 512, 1024) | 8192 | Default choice. No additional model access required. |
| Cohere Embed English v3 (`cohere.embed-english-v3`) | 1024 | 512 | Requires model access approval. Better for English-only use cases. |
| Cohere Embed Multilingual v3 (`cohere.embed-multilingual-v3`) | 1024 | 512 | Requires model access approval. Best for multilingual docs. |

**Recommendation:** Use Titan Embeddings V2 with 1024 dimensions. It's available by default in all Bedrock regions, handles the 1024-token chunk size well, and requires no model access approval.

### Vector Store Options

| Option | Managed | Serverless | Cost Model | Notes |
|---|---|---|---|---|
| OpenSearch Serverless | Partially (AWS manages infra) | Yes | OCU-hours (min 2 OCUs = ~$350/mo) | Default for Bedrock. Best integration. |
| Aurora PostgreSQL (pgvector) | Yes (RDS) | Aurora Serverless v2 | ACU-hours (can scale to 0) | Lower cost at low query volumes. Requires manual index creation. |
| Pinecone | Fully managed | Yes | Pod or serverless pricing | Third-party. Requires API key management. |
| Redis Enterprise Cloud | Fully managed | Yes | Per-shard pricing | Third-party. Good for low-latency use cases. |
| MongoDB Atlas | Fully managed | Yes | Per-cluster pricing | Third-party. Good if already using MongoDB. |

**Recommendation:** OpenSearch Serverless for simplest setup. Aurora PostgreSQL Serverless v2 if cost is a concern (can scale to 0 ACUs during idle periods, though there's a ~30s cold start).

### Data Source Sync Configuration

Bedrock's `StartIngestionJob` API initiates a full or incremental sync:

```python
response = bedrock_agent.start_ingestion_job(
    knowledgeBaseId=knowledge_base_id,
    dataSourceId=data_source_id
)
```

The sync automatically:
1. Detects new, modified, and deleted files in S3
2. Chunks new/modified files according to the `chunkingConfiguration`
3. Generates embeddings using the configured model
4. Indexes vectors in the configured vector store
5. Removes vectors for deleted files

**No manual import API path complexity.** Unlike Vertex AI (where `rag_file_chunking_config` nesting under `rag_file_transformation_config` is a known gotcha), Bedrock chunking is configured once on the data source and applied automatically on every sync.

---

## Deployed State (Example)

| Resource | Value |
|---|---|
| AWS Account | `123456789012` |
| Region | `us-west-2` |
| Knowledge Base ID | `ABCDEFGHIJ` |
| Data Source ID | `KLMNOPQRST` |
| Chunk size | 1024 tokens max |
| Chunk overlap | 20% (~200 tokens) |
| RAG bucket | `hashicorp-rag-docs-a1b2c3d4` |
| State bucket | `123456789012-tf-state-a1b2c3d4` |
| State lock table | `terraform-state-lock` |
| State machine | `rag-hashicorp-pipeline` |
| CodeBuild project | `rag-hashicorp-pipeline` |
| OpenSearch collection | `hashicorp-rag-vectors` |
| Embedding model | `amazon.titan-embed-text-v2:0` |
| CodeBuild role | `rag-pipeline-codebuild` |
| Step Functions role | `rag-pipeline-step-functions` |
| Bedrock KB role | `bedrock-kb-hashicorp-rag` |

---

## CI/CD — .github/workflows/terraform.yml

Runs on push and PR. Uses OIDC federation with GitHub Actions — no IAM access keys. Steps:
- `terraform fmt -check -recursive`
- `terraform validate`
- Trivy vulnerability scan
- `tfsec` static analysis (optional, recommended for AWS)

```yaml
permissions:
  id-token: write
  contents: read

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::${{ secrets.AWS_ACCOUNT_ID }}:role/github-actions-terraform
          aws-region: us-west-2

      - uses: hashicorp/setup-terraform@v3

      - run: terraform fmt -check -recursive
      - run: |
          cd terraform
          terraform init -backend=false
          terraform validate

      - uses: aquasecurity/trivy-action@master
        with:
          scan-type: config
          scan-ref: terraform/
```

**OIDC federation setup:**
```hcl
resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["ffffffffffffffffffffffffffffffffffffffff"]
}

resource "aws_iam_role" "github_actions" {
  name = "github-actions-terraform"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          "token.actions.githubusercontent.com:sub" = "repo:<org>/<repo>:*"
        }
      }
    }]
  })
}
```

---

## Known Gotchas

| Issue | Root Cause | Fix |
|---|---|---|
| OpenSearch Serverless minimum cost | 2 OCU minimum = ~$350/month even at zero queries | Use Aurora PostgreSQL Serverless v2 if cost matters; it scales to 0 ACU |
| Bedrock model access | Foundation models require explicit enablement per region | Go to Bedrock console → Model access → Request access for Titan Embeddings V2 |
| `us-east-1` bucket creation | `create-bucket` in `us-east-1` must NOT include `LocationConstraint` | Conditionally omit `--create-bucket-configuration` for `us-east-1` |
| DynamoDB lock table already exists | `bootstrap_state.sh` fails on second run | Check for existence before creating; ignore `ResourceInUseException` |
| Step Functions input size | Max 256 KB for state machine input | Should not be an issue for this pipeline; flag if adding large payloads |
| CodeBuild GitHub source | Public repos work without credentials; private repos need a CodeStar connection or OAuth token | Use `GITHUB` source type for public repos; `GITHUB_ENTERPRISE` + connection for private |
| Bedrock KB creation time | Knowledge Base + OpenSearch index creation can take 5-10 minutes | Script should poll `DescribeKnowledgeBase` until status is `ACTIVE` |
| Ingestion job polling | No `.sync` integration for `StartIngestionJob` in Step Functions | Must implement a Wait + GetIngestionJob poll loop in ASL |
| S3 eventual consistency | Rare: newly uploaded files may not appear immediately in ListObjects | `aws s3 sync` handles retries; Bedrock ingestion is not affected (reads individual objects) |
| OpenSearch Serverless index creation | The vector index must exist before the first ingestion job | `create_knowledge_base.py` should create the index via the OpenSearch client after collection is active |
| Bedrock region availability | Not all models/features available in all regions | Use `us-east-1` or `us-west-2` for broadest availability |
| Python venv split (3.13/3.14) | `.venv` has symlinked `python3` (3.13) but `pip` pointed to 3.14 | Use `.venv/bin/python3 -m pip install` |
| GitHub issues rate limit | Unauthenticated GitHub API: 60 req/hr; fetch script fails fast on limit | Set `GITHUB_TOKEN` in Secrets Manager; see below |

---

## Adding a GITHUB_TOKEN (optional, improves issue fetch quality)

1. Store the token in AWS Secrets Manager:
   ```bash
   aws secretsmanager create-secret \
     --name github-token \
     --secret-string '{"token":"ghp_..."}' \
     --tags Key=Project,Value=hashicorp-rag-pipeline
   ```

2. Grant the CodeBuild role access (already included in the Terraform IAM policy above).

3. Reference in `buildspec.yml`:
   ```yaml
   env:
     secrets-manager:
       GITHUB_TOKEN: "github-token:token"
   ```

---

## Key Differences Summary: Vertex AI vs Bedrock

| Aspect | Vertex AI (GCP) | Bedrock (AWS) |
|---|---|---|
| **RAG abstraction** | Corpus (auto-provisioned by workflow on first run) | Knowledge Base + Data Source (created by `create_knowledge_base.py`, IDs passed to Step Functions) |
| **Vector store** | Managed internally (Spanner) | External: OpenSearch Serverless, Aurora, Pinecone, etc. |
| **Chunking config** | Set per-import call | Set once on data source |
| **Embedding** | text-embedding-005 (always available) | Titan Embeddings V2 (requires model access) |
| **Import/sync** | `ragFiles:import` REST API | `StartIngestionJob` SDK call |
| **Incremental sync** | Manual (reimport all) | Automatic (detects changes in S3) |
| **Retrieval** | `retrieveContexts` with `vector_distance_threshold` | `retrieve` with `numberOfResults` + client-side score filtering |
| **Orchestration** | Cloud Workflows (YAML) | Step Functions (ASL JSON) |
| **Build polling** | Manual poll loop (30s sleep) | Automatic (`.sync` integration) |
| **State locking** | Built into GCS backend | Requires separate DynamoDB table |
| **Secret injection** | `availableSecrets` in Cloud Build | `env.secrets-manager` in buildspec |
| **IAM model** | Service Account + IAM bindings | IAM Roles + policies per service |
| **Self-impersonation** | Required (`actAs` on self) | Not applicable (roles are assumed, not impersonated) |
| **API enablement** | Required (`google_project_service`) | Not required (services available by default) |
| **CI/CD auth** | Workload Identity Federation | OIDC Provider + AssumeRoleWithWebIdentity |
| **Minimum cost** | Pay-per-use (no minimum) | OpenSearch Serverless: ~$350/mo minimum (2 OCUs) |

---

## MCP Server

`mcp/server.py` implements a [Model Context Protocol](https://modelcontextprotocol.io) server that exposes the Bedrock Knowledge Base as two tools callable from Claude Code:

- **`search_hashicorp_docs`** — semantic search with optional `product`, `product_family`, and `source_type` metadata filters
- **`get_knowledge_base_info`** — inspect active region/knowledge-base/data-source configuration

Once registered, Claude Code calls these tools automatically when answering questions about HashiCorp products — no manual retrieval step required.

**Environment variables:**
- `AWS_REGION` — AWS region (default: `us-west-2`)
- `AWS_KNOWLEDGE_BASE_ID` — Bedrock Knowledge Base ID
- Standard AWS credential chain (env vars, `~/.aws/credentials`, instance profile, SSO)

**Setup:**
```bash
task mcp:install                              # install mcp package and boto3 into .venv
task mcp:setup KB_ID=ABCDEFGHIJ              # write .claude/settings.local.json, then restart Claude Code
task mcp:test  KB_ID=ABCDEFGHIJ              # smoke-test retrieval
```

**Key differences from the Vertex AI MCP server:**

| Aspect | Vertex AI | Bedrock |
|---|---|---|
| Framework | `FastMCP` (from `mcp.server.fastmcp`) | `FastMCP` (from `mcp.server.fastmcp`) |
| SDK | `google-cloud-aiplatform` (`vertexai.rag`) | `boto3` (`bedrock-agent-runtime.retrieve`) |
| Auth | Application Default Credentials (ADC) | AWS credential chain |
| Distance threshold | `vector_distance_threshold: 0.35` (server-side filter) | Client-side score filtering (`score >= threshold`) |
| Search type | Vector only | `HYBRID` (vector + keyword) or `SEMANTIC` |
| Metadata filtering | Path-based inference client-side (GCS URI) | Path-based inference client-side (S3 URI) |
| Env vars | `VERTEX_PROJECT`, `VERTEX_REGION`, `VERTEX_CORPUS_ID` | `AWS_REGION`, `AWS_KNOWLEDGE_BASE_ID` |

**Path-based metadata inference** (mirrors the Vertex AI implementation): Because Bedrock does not expose arbitrary document metadata through the `retrieve()` API (only the S3 URI is returned), the MCP server infers `product`, `product_family`, and `source_type` from the S3 object path structure:
- `provider/terraform-provider-{product}/...` → `product_family=terraform`, `source_type=provider`
- `documentation/{product}/...` → `product_family={product}`, `source_type=documentation`
- `issues/{product}/...` → `source_type=issue`
- `module/...` → `product_family=terraform`, `source_type=module`
- `discuss/...` → `source_type=discuss`
- `blogs/...` → `source_type=blog`

---

## Diagrams

Two hand-crafted SVG diagrams are maintained in `docs/diagrams/`:

- **`architecture.svg`** — High-level architecture showing all AWS resources: EventBridge Scheduler, Step Functions, CodeBuild (with Git Clone and API Fetch tracks), S3 bucket, Amazon Bedrock Knowledge Base (Embedding → OpenSearch Serverless → Retrieval), consumers (Claude Code, Claude/OpenAI, MCP Server), CloudWatch, and IAM Roles. Infrastructure-as-code layer shows Terraform and GitHub Actions CI.

- **`ingestion_pipeline.svg`** — Detailed 5-state pipeline design: Scheduler → Step Functions → CodeBuild (parallel tracks with all scripts, process-docs section splitting, fetch configurations, generate-metadata) → S3 staging → Bedrock Knowledge Base (chunk, embed, index, validate).

Both use a dark theme (`#0a0a0f` background) with high-contrast colored lines and text. They are linked from the README using centered `<p align="center">` HTML blocks. The diagram content mirrors the GCP equivalents with AWS service names and resource shapes substituted.

---

## Token Efficiency

The RAG Knowledge Base provides the same token savings as described in `PROMPT_VERTEX_AI.md`. With `numberOfResults=5` and 1024-token chunks, a typical retrieval returns 3,000–5,000 tokens of focused, relevant content — compared to 8,000–12,000+ tokens when pasting full documentation pages.

The `scripts/test_token_efficiency.py` script works with both Vertex AI and Bedrock — pass `--backend bedrock` to switch the retrieval backend from the default `vertexai`. No other script changes are needed; all data processing scripts are cloud-agnostic.

---

## Code Quality Requirements

- All Python must have type hints on all functions.
- All Python must pass `ruff check` with no errors.
- All bash scripts must pass `shellcheck` with no errors.
- All Terraform must pass `terraform fmt` and `terraform validate`.
- Use `logging` module in Python for operational output (not bare `print()`).
- All functions must have docstrings.
- CodeBuild phases must have clear sequential dependencies.
- Never hardcode account IDs, bucket names, or knowledge base IDs in committed files.
- Never include secrets or credentials.
- All IAM roles must use least-privilege policies with conditions where applicable.
