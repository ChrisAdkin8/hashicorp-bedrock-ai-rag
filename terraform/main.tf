data "aws_caller_identity" "current" {}

locals {
  account_id      = data.aws_caller_identity.current.account_id
  rag_bucket_name = "hashicorp-rag-docs-${var.region}-${substr(sha256(local.account_id), 0, 8)}"

  # The aws_kendra_data_source id format is "<data_source_id>/<index_id>".
  # Step Functions needs the Data Source ID (element 0) to trigger the sync.
  kendra_data_source_id = split("/", aws_kendra_data_source.s3.id)[0]
}

# ── S3 Bucket (RAG document staging) ──────────────────────────────────────────

resource "aws_s3_bucket" "rag_docs" {
  bucket        = local.rag_bucket_name
  force_destroy = true
}

resource "aws_s3_bucket_versioning" "rag_docs" {
  bucket = aws_s3_bucket.rag_docs.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_lifecycle_configuration" "rag_docs" {
  bucket = aws_s3_bucket.rag_docs.id
  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 90 }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "rag_docs" {
  bucket = aws_s3_bucket.rag_docs.id
  rule {
    apply_server_side_encryption_by_default {
      # FIX: Switched to AES256 (SSE-S3) to avoid complex KMS IAM permission requirements.
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "rag_docs" {
  bucket                  = aws_s3_bucket.rag_docs.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}

# ── Kendra Index ──────────────────────────────────────────────────────────────

resource "aws_kendra_index" "main" {
  name     = "hashicorp-rag-index"
  edition  = var.kendra_edition
  role_arn = aws_iam_role.kendra.arn

  # ==========================================
  # --- YOUR CUSTOM METADATA FIELDS ---
  # ==========================================

  document_metadata_configuration_updates {
    name = "product"
    type = "STRING_VALUE"
    search {
      displayable = true
      facetable   = true
      searchable  = true
      sortable    = false
    }
    relevance { importance = 1 }
  }

  document_metadata_configuration_updates {
    name = "product_family"
    type = "STRING_VALUE"
    search {
      displayable = true
      facetable   = true
      searchable  = true
      sortable    = false
    }
    relevance { importance = 1 }
  }

  document_metadata_configuration_updates {
    name = "source_type"
    type = "STRING_VALUE"
    search {
      displayable = true
      facetable   = true
      searchable  = true
      sortable    = false
    }
    relevance { importance = 1 }
  }
}
resource "aws_kendra_data_source" "s3" {
  index_id = aws_kendra_index.main.id
  name     = "hashicorp-docs-s3"
  type     = "S3"
  role_arn = aws_iam_role.kendra.arn

  configuration {
    s3_configuration {
      bucket_name = aws_s3_bucket.rag_docs.id
      # Inclusion pattern: only .md files are indexed as documents.
      # Using exclusion_patterns = ["*.metadata.json"] would cause Kendra to
      # ignore those files entirely — including as attribute sidecars — which
      # produces "invalid metadata" errors. An inclusion pattern for *.md
      # achieves the same goal (sidecars are never indexed as documents) while
      # leaving Kendra free to read them as metadata for their parent documents.
      inclusion_patterns = ["*.md"]
    }
  }
}

# ── IAM: Kendra execution role ────────────────────────────────────────────────

resource "aws_iam_role" "kendra" {
  name = "hashicorp-rag-kendra"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "kendra.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
      }
    }]
  })
}

resource "aws_iam_role_policy" "kendra" {
  name = "kendra-policy"
  role = aws_iam_role.kendra.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "cloudwatch:PutMetricData"
        Resource = "*"
        Condition = {
          StringEquals = { "cloudwatch:namespace" = "Kendra" }
        }
      },
      {
        # FIX: Combined logging permissions for Group and Stream
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:DescribeLogGroups",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams"
        ]
        Resource = [
          "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/kendra/*",
          "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/kendra/*:*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket",
          "s3:GetBucketLocation"
        ]
        Resource = [
          aws_s3_bucket.rag_docs.arn,
          "${aws_s3_bucket.rag_docs.arn}/*"
        ]
      },
      {
        # FIX: Required for S3 buckets using Default KMS encryption
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey"
        ]
        Resource = ["*"] # Narrow this to your specific KMS ARN if using a Custom Key
        Condition = {
          StringLike = { "kms:ViaService" = "s3.${var.region}.amazonaws.com" }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "kendra:BatchPutDocument",
          "kendra:BatchDeleteDocument"
        ]
        Resource = ["arn:aws:kendra:${var.region}:${local.account_id}:index/*"]
      }
    ]
  })
}

# ── IAM: CodeBuild execution role ─────────────────────────────────────────────

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
        Effect = "Allow"
        Action = ["s3:PutObject", "s3:GetObject", "s3:ListBucket", "s3:DeleteObject"]
        Resource = [
          aws_s3_bucket.rag_docs.arn,
          "${aws_s3_bucket.rag_docs.arn}/*",
        ]
      },
      {
        # Required if bucket is KMS encrypted
        Effect   = "Allow"
        Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
        Resource = ["*"]
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
      },
    ]
  })
}

# ── IAM: Step Functions execution role ────────────────────────────────────────

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
        Action   = ["codebuild:StartBuild", "codebuild:StopBuild", "codebuild:BatchGetBuilds"]
        Resource = [aws_codebuild_project.rag_pipeline.arn]
      },
      {
        Effect = "Allow"
        Action = [
          "kendra:StartDataSourceSyncJob",
          "kendra:ListDataSourceSyncJobs",
          "kendra:StopDataSourceSyncJob",
          "kendra:Query",
          "kendra:Retrieve"
        ]
        Resource = [
          aws_kendra_index.main.arn,
          "${aws_kendra_index.main.arn}/data-source/*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "events:PutTargets",
          "events:PutRule",
          "events:DescribeRule",
          "events:DeleteRule",
          "events:RemoveTargets",
        ]
        Resource = [
          "*",
          # FIX: Explicitly allow Step Functions to create the managed rule for CodeBuild .sync integration
          "arn:aws:events:${var.region}:${local.account_id}:rule/StepFunctionsGetEventsForCodeBuildStartBuildRule"
        ]
      },
    ]
  })
}

# ── IAM: EventBridge Scheduler execution role ─────────────────────────────────

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

# ── IAM: GitHub Actions OIDC ──────────────────────────────────────────────────

resource "aws_iam_openid_connect_provider" "github" {
  count           = var.create_github_oidc_provider ? 1 : 0
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["ffffffffffffffffffffffffffffffffffffffff"]
}

resource "aws_iam_role" "github_actions" {
  count = var.create_github_oidc_provider ? 1 : 0
  name  = "github-actions-terraform"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github[0].arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          "token.actions.githubusercontent.com:sub" = "repo:*/${basename(var.repo_uri)}:*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_actions" {
  count = var.create_github_oidc_provider ? 1 : 0
  name  = "github-actions-terraform-policy"
  role  = aws_iam_role.github_actions[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["*"]
        Resource = "*"
      },
    ]
  })
}

# ── CodeBuild Project ─────────────────────────────────────────────────────────

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

  build_timeout = 120
}

# ── Step Functions State Machine ──────────────────────────────────────────────

resource "aws_sfn_state_machine" "rag_pipeline" {
  name     = "rag-hashicorp-pipeline"
  role_arn = aws_iam_role.step_functions.arn

  definition = templatefile("${path.module}/../step-functions/rag_pipeline.asl.json", {
    codebuild_project_name = aws_codebuild_project.rag_pipeline.name
  })

  depends_on = [
    aws_iam_role_policy.step_functions
  ]
}

# ── EventBridge Scheduler ─────────────────────────────────────────────────────

resource "aws_scheduler_schedule" "rag_weekly_refresh" {
  name       = "rag-weekly-refresh"
  group_name = "default"

  schedule_expression          = var.refresh_schedule
  schedule_expression_timezone = var.scheduler_timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_sfn_state_machine.rag_pipeline.arn
    role_arn = aws_iam_role.scheduler.arn

    input = jsonencode({
      kendra_index_id       = aws_kendra_index.main.id
      kendra_data_source_id = local.kendra_data_source_id
      bucket_name           = aws_s3_bucket.rag_docs.id
      region                = var.region
      repo_url              = var.repo_uri
    })
  }
}

# ── CloudWatch Monitoring ─────────────────────────────────────────────────────

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