# ── Security Group ─────────────────────────────────────────────────────────────

resource "aws_security_group" "neptune" {
  name        = "${var.cluster_identifier}-sg"
  description = "Neptune cluster access on port 8182"
  vpc_id      = var.vpc_id

  ingress {
    description = "Neptune Bolt/HTTPS"
    from_port   = 8182
    to_port     = 8182
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidr_blocks
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ── Subnet Group ───────────────────────────────────────────────────────────────

resource "aws_neptune_subnet_group" "main" {
  name        = "${var.cluster_identifier}-subnet-group"
  subnet_ids  = var.subnet_ids
  description = "Subnet group for Neptune cluster ${var.cluster_identifier}"
}

# ── Cluster Parameter Group ────────────────────────────────────────────────────

resource "aws_neptune_cluster_parameter_group" "main" {
  name        = "${var.cluster_identifier}-params"
  family      = "neptune1.4"
  description = "Parameter group for ${var.cluster_identifier}"

  parameter {
    name  = "neptune_enable_audit_log"
    value = "1"
  }
}

# ── Cluster ────────────────────────────────────────────────────────────────────

resource "aws_neptune_cluster" "main" {
  cluster_identifier                   = var.cluster_identifier
  engine                               = "neptune"
  neptune_subnet_group_name            = aws_neptune_subnet_group.main.name
  neptune_cluster_parameter_group_name = aws_neptune_cluster_parameter_group.main.name
  vpc_security_group_ids               = [aws_security_group.neptune.id]

  iam_database_authentication_enabled = var.iam_auth_enabled
  deletion_protection                 = var.deletion_protection
  backup_retention_period             = var.backup_retention_days
  skip_final_snapshot                 = !var.deletion_protection

  apply_immediately = true
}

# ── Cluster Instances ──────────────────────────────────────────────────────────

resource "aws_neptune_cluster_instance" "main" {
  count              = var.instance_count
  identifier         = "${var.cluster_identifier}-${count.index}"
  cluster_identifier = aws_neptune_cluster.main.id
  instance_class     = var.instance_class
  engine             = "neptune"

  apply_immediately = true
}
