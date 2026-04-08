locals {
  rag_bucket_name = "hashicorp-rag-docs-${data.aws_region.current.name}-${substr(sha256(data.aws_caller_identity.current.account_id), 0, 8)}"

  # The aws_kendra_data_source id format is "<data_source_id>/<index_id>".
  # Step Functions needs the Data Source ID (element 0) to trigger the sync.
  kendra_data_source_id = split("/", aws_kendra_data_source.s3.id)[0]
}
