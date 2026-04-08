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

  definition = templatefile("${path.root}/../step-functions/rag_pipeline.asl.json", {
    codebuild_project_name = aws_codebuild_project.rag_pipeline.name
  })

  depends_on = [aws_iam_role_policy.step_functions]
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
      region                = data.aws_region.current.name
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
