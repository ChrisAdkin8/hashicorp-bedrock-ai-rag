output "rag_bucket_name" {
  description = "S3 bucket name for processed RAG documents."
  value       = aws_s3_bucket.rag_docs.id
}

output "rag_bucket_arn" {
  description = "ARN of the RAG documents S3 bucket."
  value       = aws_s3_bucket.rag_docs.arn
}

output "state_machine_arn" {
  description = "ARN of the Step Functions state machine."
  value       = aws_sfn_state_machine.rag_pipeline.arn
}

output "codebuild_project_name" {
  description = "Name of the CodeBuild project."
  value       = aws_codebuild_project.rag_pipeline.name
}

output "codebuild_project_arn" {
  description = "ARN of the CodeBuild project."
  value       = aws_codebuild_project.rag_pipeline.arn
}

output "codebuild_role_arn" {
  description = "ARN of the CodeBuild execution IAM role."
  value       = aws_iam_role.codebuild.arn
}

output "bedrock_kb_role_arn" {
  description = "ARN of the Bedrock Knowledge Base execution IAM role. Pass to create_knowledge_base.py."
  value       = aws_iam_role.bedrock_kb.arn
}

output "opensearch_collection_arn" {
  description = "ARN of the OpenSearch Serverless collection. Pass to create_knowledge_base.py."
  value       = aws_opensearchserverless_collection.vectors.arn
}

output "opensearch_collection_endpoint" {
  description = "OpenSearch Serverless collection endpoint URL."
  value       = aws_opensearchserverless_collection.vectors.collection_endpoint
}

output "effective_embedding_model_arn" {
  description = "Resolved Bedrock embedding model ARN (region-specific)."
  value       = local.embedding_model_arn
}
