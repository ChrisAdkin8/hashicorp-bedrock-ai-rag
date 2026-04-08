module "hashicorp_docs_pipeline" {
  source = "./modules/hashicorp-docs-pipeline"

  region                      = var.region
  account_id                  = data.aws_caller_identity.current.account_id
  kendra_edition              = var.kendra_edition
  refresh_schedule            = var.refresh_schedule
  scheduler_timezone          = var.scheduler_timezone
  repo_uri                    = var.repo_uri
  notification_email          = var.notification_email
  create_github_oidc_provider = var.create_github_oidc_provider
  force_destroy               = var.force_destroy
  tags                        = var.tags
}

module "terraform_graph_store" {
  count  = var.create_neptune ? 1 : 0
  source = "./modules/terraform-graph-store"

  region                 = var.region
  account_id             = data.aws_caller_identity.current.account_id
  vpc_id                 = var.neptune_vpc_id
  subnet_ids             = var.neptune_subnet_ids
  allowed_cidr_blocks    = var.neptune_allowed_cidr_blocks
  cluster_identifier     = var.neptune_cluster_identifier
  instance_class         = var.neptune_instance_class
  instance_count         = var.neptune_instance_count
  iam_auth_enabled       = var.neptune_iam_auth_enabled
  deletion_protection    = var.neptune_deletion_protection
  backup_retention_days  = var.neptune_backup_retention_days
  repo_uri               = var.repo_uri
  repo_uris              = var.graph_repo_uris
  notification_email     = var.notification_email
  refresh_schedule       = var.graph_refresh_schedule
  scheduler_timezone     = var.scheduler_timezone
  codebuild_compute_type        = var.graph_codebuild_compute_type
  create_nat_gateway            = var.neptune_create_nat_gateway
  codebuild_private_subnet_cidr = var.neptune_codebuild_private_subnet_cidr
  tags                          = var.tags
}
