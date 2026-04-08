# ── CodeBuild security group ──────────────────────────────────────────────────
# CodeBuild must run inside the VPC to reach Neptune (no public endpoint).

resource "aws_security_group" "codebuild" {
  name        = "${var.cluster_identifier}-codebuild-sg"
  description = "CodeBuild graph-pipeline egress - allows Neptune and HTTPS"
  vpc_id      = var.vpc_id

  egress {
    description     = "Neptune Bolt/HTTPS - scoped to Neptune SG only"
    from_port       = 8182
    to_port         = 8182
    protocol        = "tcp"
    security_groups = [aws_security_group.neptune.id]
  }

  egress {
    description = "HTTPS (GitHub clone, AWS APIs)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "HTTP (package mirrors)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Allow inbound from CodeBuild security group on port 8182
resource "aws_security_group_rule" "neptune_from_codebuild" {
  type                     = "ingress"
  description              = "Graph pipeline CodeBuild"
  from_port                = 8182
  to_port                  = 8182
  protocol                 = "tcp"
  security_group_id        = aws_security_group.neptune.id
  source_security_group_id = aws_security_group.codebuild.id
}

# ── CodeBuild project ──────────────────────────────────────────────────────────

resource "aws_codebuild_project" "graph_pipeline" {
  name         = "${var.cluster_identifier}-graph-pipeline"
  service_role = aws_iam_role.codebuild.arn

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    compute_type    = var.codebuild_compute_type
    image           = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    type            = "LINUX_CONTAINER"
    privileged_mode = false

    environment_variable {
      name  = "GRAPH_STAGING_BUCKET"
      value = aws_s3_bucket.graph_staging.id
    }

    environment_variable {
      name  = "NEPTUNE_ENDPOINT"
      value = aws_neptune_cluster.main.endpoint
    }

    environment_variable {
      name  = "NEPTUNE_PORT"
      value = aws_neptune_cluster.main.port
    }

    environment_variable {
      name  = "NEPTUNE_IAM_AUTH"
      value = tostring(var.iam_auth_enabled)
    }

    environment_variable {
      name  = "AWS_REGION"
      value = var.region
    }
  }

  source {
    type            = "GITHUB"
    location        = var.repo_uri
    git_clone_depth = 1
    buildspec       = "codebuild/buildspec_graph.yml"
  }

  vpc_config {
    vpc_id             = var.vpc_id
    subnets            = var.create_nat_gateway ? [aws_subnet.codebuild_private[0].id] : var.subnet_ids
    security_group_ids = [aws_security_group.codebuild.id]
  }

  logs_config {
    cloudwatch_logs {
      group_name = "/aws/codebuild/${var.cluster_identifier}-graph-pipeline"
    }
  }

  build_timeout = 120
}
