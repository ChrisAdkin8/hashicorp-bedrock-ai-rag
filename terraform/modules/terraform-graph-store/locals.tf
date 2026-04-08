locals {
  staging_bucket_name = "hashicorp-graph-staging-${data.aws_region.current.name}-${substr(sha256(data.aws_caller_identity.current.account_id), 0, 8)}"
}
