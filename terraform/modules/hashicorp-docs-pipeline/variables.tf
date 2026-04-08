variable "kendra_edition" {
  description = "Kendra index edition. DEVELOPER_EDITION (~$810/mo) or ENTERPRISE_EDITION (~$1400/mo)."
  type        = string
  default     = "ENTERPRISE_EDITION"
  validation {
    condition     = contains(["DEVELOPER_EDITION", "ENTERPRISE_EDITION"], var.kendra_edition)
    error_message = "Must be DEVELOPER_EDITION or ENTERPRISE_EDITION."
  }
}

variable "force_destroy" {
  description = "Allow the RAG docs S3 bucket to be destroyed even if it contains objects. Set true only for non-production environments."
  type        = bool
  default     = false
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

variable "tags" {
  description = "Additional resource-specific tags to merge with provider default_tags."
  type        = map(string)
  default     = {}
}
