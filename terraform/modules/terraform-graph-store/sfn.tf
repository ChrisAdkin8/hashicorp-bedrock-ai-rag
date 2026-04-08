# ── Step Functions State Machine ──────────────────────────────────────────────

resource "aws_sfn_state_machine" "graph_pipeline" {
  name     = "${var.cluster_identifier}-graph-pipeline"
  role_arn = aws_iam_role.step_functions.arn

  definition = templatefile("${path.root}/../step-functions/graph_pipeline.asl.json", {
    codebuild_project_name = aws_codebuild_project.graph_pipeline.name
  })

  depends_on = [aws_iam_role_policy.step_functions]
}

# ── EventBridge Scheduler ─────────────────────────────────────────────────────

resource "aws_scheduler_schedule" "graph_weekly_refresh" {
  name       = "${var.cluster_identifier}-graph-refresh"
  group_name = "default"

  schedule_expression          = var.refresh_schedule
  schedule_expression_timezone = var.scheduler_timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_sfn_state_machine.graph_pipeline.arn
    role_arn = aws_iam_role.scheduler.arn

    input = jsonencode({
      repo_uris            = var.repo_uris
      graph_staging_bucket = aws_s3_bucket.graph_staging.id
      neptune_endpoint     = aws_neptune_cluster.main.endpoint
      neptune_port         = aws_neptune_cluster.main.port
      region               = data.aws_region.current.name
    })
  }
}

# ── CloudWatch Monitoring ─────────────────────────────────────────────────────

resource "aws_sns_topic" "alerts" {
  count = var.notification_email != "" ? 1 : 0
  name  = "${var.cluster_identifier}-graph-pipeline-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.notification_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts[0].arn
  protocol  = "email"
  endpoint  = var.notification_email
}

resource "aws_cloudwatch_metric_alarm" "sfn_failures" {
  count               = var.notification_email != "" ? 1 : 0
  alarm_name          = "${var.cluster_identifier}-graph-pipeline-sfn-failures"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ExecutionsFailed"
  namespace           = "AWS/States"
  period              = 86400
  statistic           = "Sum"
  threshold           = 0
  alarm_actions       = [aws_sns_topic.alerts[0].arn]

  dimensions = {
    StateMachineArn = aws_sfn_state_machine.graph_pipeline.arn
  }
}

resource "aws_cloudwatch_metric_alarm" "codebuild_failures" {
  count               = var.notification_email != "" ? 1 : 0
  alarm_name          = "${var.cluster_identifier}-graph-pipeline-codebuild-failures"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "FailedBuilds"
  namespace           = "AWS/CodeBuild"
  period              = 86400
  statistic           = "Sum"
  threshold           = 0
  alarm_actions       = [aws_sns_topic.alerts[0].arn]

  dimensions = {
    ProjectName = aws_codebuild_project.graph_pipeline.name
  }
}
