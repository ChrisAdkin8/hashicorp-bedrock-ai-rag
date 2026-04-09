resource "aws_kendra_index" "main" {
  name     = "hashicorp-rag-index"
  edition  = var.kendra_edition
  role_arn = aws_iam_role.kendra.arn

  lifecycle {
    prevent_destroy = false
  }

  document_metadata_configuration_updates {
    name = "product"
    type = "STRING_VALUE"
    search {
      displayable = true
      facetable   = true
      searchable  = true
      sortable    = false
    }
    relevance { importance = 1 }
  }

  document_metadata_configuration_updates {
    name = "product_family"
    type = "STRING_VALUE"
    search {
      displayable = true
      facetable   = true
      searchable  = true
      sortable    = false
    }
    relevance { importance = 1 }
  }

  document_metadata_configuration_updates {
    name = "source_type"
    type = "STRING_VALUE"
    search {
      displayable = true
      facetable   = true
      searchable  = true
      sortable    = false
    }
    relevance { importance = 1 }
  }
}

resource "aws_kendra_data_source" "s3" {
  index_id = aws_kendra_index.main.id
  name     = "hashicorp-docs-s3"
  type     = "S3"
  role_arn = aws_iam_role.kendra.arn

  configuration {
    s3_configuration {
      bucket_name        = aws_s3_bucket.rag_docs.id
      inclusion_patterns = ["**/*.md"]
    }
  }
}
