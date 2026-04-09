# ── Neptune openCypher proxy — Lambda + API Gateway ──────────────────────────
# Exposes Neptune queries from outside the VPC via API Gateway (IAM auth)
# backed by a Lambda that SigV4-signs requests to Neptune.
#
# Gated by var.create_neptune_proxy (default false).

# ── Lambda packaging ─────────────────────────────────────────────────────────

data "archive_file" "neptune_proxy" {
  count       = var.create_neptune_proxy ? 1 : 0
  type        = "zip"
  source_file = "${path.module}/lambda/neptune_proxy.py"
  output_path = "${path.module}/lambda/.dist/neptune_proxy.zip"
}

# ── Lambda security group ────────────────────────────────────────────────────

resource "aws_security_group" "lambda_proxy" {
  count       = var.create_neptune_proxy ? 1 : 0
  name        = "${var.cluster_identifier}-lambda-proxy-sg"
  description = "Lambda Neptune proxy - egress to Neptune and AWS APIs"
  vpc_id      = var.vpc_id

  egress {
    description     = "Neptune openCypher"
    from_port       = 8182
    to_port         = 8182
    protocol        = "tcp"
    security_groups = [aws_security_group.neptune.id]
  }

  egress {
    description = "HTTPS (AWS APIs, STS, CloudWatch)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = var.tags
}

resource "aws_security_group_rule" "neptune_from_lambda" {
  count                    = var.create_neptune_proxy ? 1 : 0
  type                     = "ingress"
  description              = "Neptune proxy Lambda"
  from_port                = 8182
  to_port                  = 8182
  protocol                 = "tcp"
  security_group_id        = aws_security_group.neptune.id
  source_security_group_id = aws_security_group.lambda_proxy[0].id
}

# ── IAM role ─────────────────────────────────────────────────────────────────

resource "aws_iam_role" "lambda_proxy" {
  count = var.create_neptune_proxy ? 1 : 0
  name  = "${var.cluster_identifier}-neptune-proxy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "lambda_proxy" {
  count = var.create_neptune_proxy ? 1 : 0
  name  = "neptune-proxy-policy"
  role  = aws_iam_role.lambda_proxy[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "neptune-db:connect",
          "neptune-db:ReadDataViaQuery",
        ]
        Resource = ["arn:aws:neptune-db:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:${aws_neptune_cluster.main.cluster_resource_id}/*"]
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = ["arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.cluster_identifier}-neptune-proxy*"]
      },
      {
        # VPC Lambda networking — AWS requires Resource: "*" for these actions.
        Effect = "Allow"
        Action = [
          "ec2:CreateNetworkInterface",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DeleteNetworkInterface",
        ]
        Resource = "*"
      },
    ]
  })
}

# ── Lambda function ──────────────────────────────────────────────────────────

resource "aws_lambda_function" "neptune_proxy" {
  count         = var.create_neptune_proxy ? 1 : 0
  function_name = "${var.cluster_identifier}-neptune-proxy"
  role          = aws_iam_role.lambda_proxy[0].arn
  handler       = "neptune_proxy.handler"
  runtime       = "python3.12"
  timeout       = 30
  memory_size   = 256

  filename         = data.archive_file.neptune_proxy[0].output_path
  source_code_hash = data.archive_file.neptune_proxy[0].output_base64sha256

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = [aws_security_group.lambda_proxy[0].id]
  }

  environment {
    variables = {
      NEPTUNE_ENDPOINT = aws_neptune_cluster.main.endpoint
      NEPTUNE_PORT     = tostring(aws_neptune_cluster.main.port)
    }
  }

  tags = var.tags
}

# ── API Gateway HTTP API ─────────────────────────────────────────────────────

resource "aws_apigatewayv2_api" "neptune_proxy" {
  count         = var.create_neptune_proxy ? 1 : 0
  name          = "${var.cluster_identifier}-neptune-proxy"
  protocol_type = "HTTP"

  tags = var.tags
}

resource "aws_apigatewayv2_stage" "default" {
  count       = var.create_neptune_proxy ? 1 : 0
  api_id      = aws_apigatewayv2_api.neptune_proxy[0].id
  name        = "$default"
  auto_deploy = true

  tags = var.tags
}

resource "aws_apigatewayv2_integration" "lambda_proxy" {
  count                  = var.create_neptune_proxy ? 1 : 0
  api_id                 = aws_apigatewayv2_api.neptune_proxy[0].id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.neptune_proxy[0].invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "query" {
  count     = var.create_neptune_proxy ? 1 : 0
  api_id    = aws_apigatewayv2_api.neptune_proxy[0].id
  route_key = "POST /query"

  authorization_type = "AWS_IAM"
  target             = "integrations/${aws_apigatewayv2_integration.lambda_proxy[0].id}"
}

resource "aws_lambda_permission" "apigw" {
  count         = var.create_neptune_proxy ? 1 : 0
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.neptune_proxy[0].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.neptune_proxy[0].execution_arn}/*/*"
}
