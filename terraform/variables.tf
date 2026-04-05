variable "region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-west-2"
}

variable "knowledge_base_name" {
  description = "Bedrock Knowledge Base display name."
  type        = string
  default     = "hashicorp-knowledge-base"
}

variable "knowledge_base_id" {
  description = "Bedrock Knowledge Base ID (populated by deploy.sh after create_knowledge_base.py runs)."
  type        = string
  default     = ""
}

variable "data_source_id" {
  description = "Bedrock Data Source ID (populated by deploy.sh after create_knowledge_base.py runs)."
  type        = string
  default     = ""
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

variable "chunk_size" {
  description = "Maximum tokens per Bedrock Knowledge Base chunk."
  type        = number
  default     = 1024
}

variable "chunk_overlap_pct" {
  description = "Chunk overlap as a percentage (Bedrock uses percentage, not absolute tokens)."
  type        = number
  default     = 20
}

variable "embedding_model_arn" {
  description = "ARN of the Bedrock foundation model used for embeddings. Override for a non-default region."
  type        = string
  default     = ""
}

variable "collection_name" {
  description = "OpenSearch Serverless collection name for the vector store."
  type        = string
  default     = "hashicorp-rag-vectors"
}

variable "notification_email" {
  description = "Email address for CloudWatch alarm notifications. Leave empty to disable."
  type        = string
  default     = ""
}
