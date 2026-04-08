# ── Shared ────────────────────────────────────────────────────────────────────

variable "region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[0-9]$", var.region))
    error_message = "Must be a valid AWS region identifier (e.g. us-east-1)."
  }
}

# ── hashicorp-kendra-rag module ───────────────────────────────────────────────

variable "kendra_edition" {
  description = "Kendra index edition. DEVELOPER_EDITION (~$810/mo) or ENTERPRISE_EDITION (~$1400/mo)."
  type        = string
  default     = "ENTERPRISE_EDITION"
}

variable "refresh_schedule" {
  description = "EventBridge Scheduler cron expression (UTC)."
  type        = string
  default     = "cron(0 2 ? * SUN *)"
}

variable "scheduler_timezone" {
  description = "Timezone for the EventBridge Scheduler."
  type        = string
  default     = "Europe/London"
}

variable "repo_uri" {
  description = "GitHub HTTPS URL of this repository — CodeBuild clones it to access pipeline scripts."
  type        = string
}

variable "notification_email" {
  description = "Email address for CloudWatch alarm notifications. Leave empty to disable."
  type        = string
  default     = ""
}

variable "create_github_oidc_provider" {
  description = "Set to true to create the GitHub Actions OIDC provider and associated IAM role."
  type        = bool
  default     = false
}

variable "force_destroy" {
  description = "Allow the RAG docs S3 bucket to be destroyed even if it contains objects. Set true only for non-production environments."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Additional resource-specific tags applied to all modules."
  type        = map(string)
  default     = {}
}

# ── neptune module ────────────────────────────────────────────────────────────

variable "create_neptune" {
  description = "Set to true to deploy the Neptune graph database module."
  type        = bool
  default     = false
}

variable "neptune_vpc_id" {
  description = "VPC ID for the Neptune cluster. Required when create_neptune = true."
  type        = string
  default     = ""
}

variable "neptune_subnet_ids" {
  description = "Subnet IDs for the Neptune subnet group. Required when create_neptune = true."
  type        = list(string)
  default     = []
}

variable "neptune_allowed_cidr_blocks" {
  description = "CIDR blocks permitted to reach Neptune on port 8182."
  type        = list(string)
  default     = []
}

variable "neptune_cluster_identifier" {
  description = "Identifier for the Neptune cluster."
  type        = string
  default     = "hashicorp-rag-graph"
}

variable "neptune_instance_class" {
  description = "Neptune instance class."
  type        = string
  default     = "db.r6g.large"
}

variable "neptune_instance_count" {
  description = "Number of Neptune instances (1 = writer only, 2+ = writer + readers)."
  type        = number
  default     = 1
}

variable "neptune_iam_auth_enabled" {
  description = "Enable IAM database authentication for Neptune."
  type        = bool
  default     = true
}

variable "neptune_deletion_protection" {
  description = "Prevent the Neptune cluster from being deleted via Terraform."
  type        = bool
  default     = true
}

variable "neptune_backup_retention_days" {
  description = "Number of days to retain automated Neptune backups."
  type        = number
  default     = 7
}

variable "neptune_create_nat_gateway" {
  description = "Create a NAT gateway so VPC-attached CodeBuild can reach the internet. Required when subnets have no existing NAT route (e.g. default VPC public subnets)."
  type        = bool
  default     = false
}

variable "neptune_codebuild_private_subnet_cidr" {
  description = "CIDR for the private CodeBuild subnet created when neptune_create_nat_gateway = true."
  type        = string
  default     = "172.31.64.0/24"
}

variable "graph_repo_uris" {
  description = "List of GitHub HTTPS URLs of Terraform workspace repositories to plan and ingest into Neptune."
  type        = list(string)
  default     = []
}

variable "graph_refresh_schedule" {
  description = "EventBridge Scheduler cron expression (UTC) for the graph refresh pipeline."
  type        = string
  default     = "cron(0 3 ? * SUN *)"
}

variable "graph_codebuild_compute_type" {
  description = "CodeBuild compute type for the graph pipeline."
  type        = string
  default     = "BUILD_GENERAL1_MEDIUM"
}
