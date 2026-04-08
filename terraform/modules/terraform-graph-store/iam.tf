# ── CodeBuild execution role ───────────────────────────────────────────────────

resource "aws_iam_role" "codebuild" {
  name = "${var.cluster_identifier}-codebuild"
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
          aws_s3_bucket.graph_staging.arn,
          "${aws_s3_bucket.graph_staging.arn}/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/codebuild/${var.cluster_identifier}-graph-pipeline*"]
      },
      {
        # Required for CodeBuild running inside a VPC.
        # Describe* and CreateNetworkInterface require Resource: "*" — AWS does not support
        # resource-level permissions for these EC2 actions (documented AWS limitation).
        Effect = "Allow"
        Action = [
          "ec2:CreateNetworkInterface",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DeleteNetworkInterface",
          "ec2:DescribeSubnets",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeDhcpOptions",
          "ec2:DescribeVpcs",
        ]
        Resource = "*"
      },
      {
        # Scoped to this VPC only — reduces blast radius vs Resource: "*"
        Effect   = "Allow"
        Action   = ["ec2:CreateNetworkInterfacePermission"]
        Resource = "*"
        Condition = {
          StringEquals = { "ec2:Vpc" = "arn:aws:ec2:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:vpc/${var.vpc_id}" }
        }
      },
      {
        # Neptune IAM auth — connect + data read/write for openCypher MERGE queries.
        # neptune-db:connect is required for basic auth; ReadDataViaQuery and
        # WriteDataViaQuery are required when Neptune FGAC is active (neptune1.4+).
        Effect = "Allow"
        Action = [
          "neptune-db:connect",
          "neptune-db:ReadDataViaQuery",
          "neptune-db:WriteDataViaQuery",
          "neptune-db:DeleteDataViaQuery",
        ]
        Resource = ["arn:aws:neptune-db:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:${aws_neptune_cluster.main.cluster_resource_id}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = ["arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:github-token-*"]
        Condition = {
          StringEquals = { "aws:ResourceTag/Project" = "hashicorp-rag-pipeline" }
        }
      },
    ]
  })
}

# ── Step Functions execution role ──────────────────────────────────────────────

resource "aws_iam_role" "step_functions" {
  name = "${var.cluster_identifier}-step-functions"
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
        Resource = [aws_codebuild_project.graph_pipeline.arn]
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
        # Resource "*" is required for Step Functions managed-rule creation.
        # Step Functions validates this permission when creating a state machine that
        # uses .sync integrations inside Map states. The exact managed-rule ARN is
        # not known at policy creation time — AWS generates it at state machine creation.
        Resource = ["*"]
      },
    ]
  })
}

# ── EventBridge Scheduler execution role ───────────────────────────────────────

resource "aws_iam_role" "scheduler" {
  name = "${var.cluster_identifier}-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
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
      Resource = [aws_sfn_state_machine.graph_pipeline.arn]
    }]
  })
}
