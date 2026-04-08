variable "vpc_id" {
  description = "VPC ID in which to deploy the Neptune cluster."
  type        = string
  validation {
    condition     = can(regex("^vpc-[0-9a-f]+$", var.vpc_id))
    error_message = "Must be a valid VPC ID (e.g. vpc-0123456789abcdef0)."
  }
}

variable "subnet_ids" {
  description = "List of subnet IDs for the Neptune subnet group (minimum 2, in different AZs)."
  type        = list(string)
  validation {
    condition     = length(var.subnet_ids) >= 2
    error_message = "At least 2 subnet IDs are required for Neptune (one per AZ)."
  }
}

variable "allowed_cidr_blocks" {
  description = "CIDR blocks permitted to reach Neptune on port 8182."
  type        = list(string)
  default     = []
}

variable "cluster_identifier" {
  description = "Identifier for the Neptune cluster."
  type        = string
  default     = "hashicorp-rag-graph"
}

variable "instance_class" {
  description = "Neptune instance class."
  type        = string
  default     = "db.r6g.large"
  validation {
    condition     = can(regex("^db\\.", var.instance_class))
    error_message = "Must be a valid Neptune instance class (e.g. db.r6g.large)."
  }
}

variable "instance_count" {
  description = "Number of Neptune instances (1 = writer only, 2+ = writer + readers)."
  type        = number
  default     = 1
  validation {
    condition     = var.instance_count >= 1 && var.instance_count <= 16
    error_message = "Instance count must be between 1 and 16."
  }
}

variable "iam_auth_enabled" {
  description = "Enable IAM database authentication for Neptune."
  type        = bool
  default     = true
}

variable "deletion_protection" {
  description = "Prevent the cluster from being deleted via Terraform."
  type        = bool
  default     = true
}

variable "backup_retention_days" {
  description = "Number of days to retain automated Neptune backups."
  type        = number
  default     = 7
}

variable "tags" {
  description = "Additional resource-specific tags to merge with provider default_tags."
  type        = map(string)
  default     = {}
}

# ── Pipeline ──────────────────────────────────────────────────────────────────

variable "repo_uri" {
  description = "GitHub HTTPS URL of this repository — CodeBuild clones it to access pipeline scripts."
  type        = string
}

variable "repo_uris" {
  description = "List of GitHub HTTPS URLs of Terraform workspace repositories to plan and ingest into Neptune."
  type        = list(string)
  default     = []
}

variable "notification_email" {
  description = "Email address for CloudWatch alarm notifications. Leave empty to disable."
  type        = string
  default     = ""
}

variable "refresh_schedule" {
  description = "EventBridge Scheduler cron expression (UTC) for the graph refresh pipeline."
  type        = string
  default     = "cron(0 3 ? * SUN *)"
}

variable "scheduler_timezone" {
  description = "Timezone for the EventBridge Scheduler."
  type        = string
  default     = "Europe/London"
}

variable "create_nat_gateway" {
  description = "Create an EIP, NAT gateway, and private subnet so VPC-attached CodeBuild can reach the internet (required when subnets have no NAT route)."
  type        = bool
  default     = false
}

variable "codebuild_private_subnet_cidr" {
  description = "CIDR block for the private CodeBuild subnet created when create_nat_gateway = true. Must not overlap existing subnets."
  type        = string
  default     = "172.31.64.0/24"
}

variable "codebuild_compute_type" {
  description = "CodeBuild compute type for the graph pipeline."
  type        = string
  default     = "BUILD_GENERAL1_MEDIUM"
  validation {
    condition     = contains(["BUILD_GENERAL1_SMALL", "BUILD_GENERAL1_MEDIUM", "BUILD_GENERAL1_LARGE"], var.codebuild_compute_type)
    error_message = "Must be BUILD_GENERAL1_SMALL, BUILD_GENERAL1_MEDIUM, or BUILD_GENERAL1_LARGE."
  }
}
