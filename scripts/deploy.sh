#!/usr/bin/env bash
# deploy.sh — End-to-end deploy orchestrator for the HashiCorp Bedrock RAG pipeline.
#
# Called by `task up`. Idempotent — safe to re-run.
#
# Steps:
#   1. Bootstrap S3 state bucket + DynamoDB lock table
#   2. terraform init + terraform apply (first pass — provisions infra)
#   3. create_knowledge_base.py → writes kb.auto.tfvars
#   4. terraform apply (second pass — wires KB/DS IDs into scheduler target)
#   5. Trigger first pipeline run (unless --skip-pipeline)
#
# Usage:
#   scripts/deploy.sh --region us-west-2 --repo-uri https://github.com/org/repo
set -euo pipefail

REGION="us-west-2"
REPO_URI=""
SKIP_PIPELINE=false
TF_DIR="terraform"
PYTHON=".venv/bin/python3"

usage() {
  echo "Usage: $0 --region REGION --repo-uri REPO_URI [--skip-pipeline]"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)        REGION="$2";        shift 2 ;;
    --repo-uri)      REPO_URI="$2";      shift 2 ;;
    --skip-pipeline) SKIP_PIPELINE=true; shift 1 ;;
    -h|--help)       usage ;;
    *) echo "Unknown argument: $1"; usage ;;
  esac
done

if [[ -z "${REPO_URI}" ]]; then
  echo "ERROR: --repo-uri is required"
  usage
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
STATE_BUCKET="${ACCOUNT_ID}-tf-state-$(echo -n "${ACCOUNT_ID}" | sha256sum | cut -c1-8)"
LOCK_TABLE="terraform-state-lock"

echo "==> Step 1: Bootstrap state bucket and DynamoDB lock table"
bash scripts/bootstrap_state.sh --region "${REGION}"

echo ""
echo "==> Step 2: terraform init + apply (first pass)"
terraform -chdir="${TF_DIR}" init \
  -backend-config="bucket=${STATE_BUCKET}" \
  -backend-config="region=${REGION}" \
  -backend-config="dynamodb_table=${LOCK_TABLE}" \
  -input=false

# Write tfvars if not already present
if [[ ! -f "${TF_DIR}/terraform.tfvars" ]]; then
  cat > "${TF_DIR}/terraform.tfvars" <<EOF
region   = "${REGION}"
repo_uri = "${REPO_URI}"
EOF
  echo "Wrote ${TF_DIR}/terraform.tfvars"
fi

terraform -chdir="${TF_DIR}" apply -auto-approve -input=false

echo ""
echo "==> Step 3: Create Bedrock Knowledge Base"
KB_ROLE_ARN=$(terraform -chdir="${TF_DIR}" output -raw bedrock_kb_role_arn)
COLLECTION_ARN=$(terraform -chdir="${TF_DIR}" output -raw opensearch_collection_arn)
COLLECTION_ENDPOINT=$(terraform -chdir="${TF_DIR}" output -raw opensearch_collection_endpoint)
RAG_BUCKET=$(terraform -chdir="${TF_DIR}" output -raw rag_bucket_name)

"${PYTHON}" scripts/create_knowledge_base.py \
  --region             "${REGION}" \
  --kb-role-arn        "${KB_ROLE_ARN}" \
  --collection-arn     "${COLLECTION_ARN}" \
  --collection-endpoint "${COLLECTION_ENDPOINT}" \
  --bucket-name        "${RAG_BUCKET}" \
  --output-id-only > "${TF_DIR}/kb.auto.tfvars"

echo "Wrote ${TF_DIR}/kb.auto.tfvars:"
cat "${TF_DIR}/kb.auto.tfvars"

echo ""
echo "==> Step 4: terraform apply (second pass — wire in KB/DS IDs)"
terraform -chdir="${TF_DIR}" apply -auto-approve -input=false

echo ""
if [[ "${SKIP_PIPELINE}" == "true" ]]; then
  echo "Skipping pipeline run (--skip-pipeline set)."
else
  echo "==> Step 5: Trigger first pipeline run"
  STATE_MACHINE_ARN=$(terraform -chdir="${TF_DIR}" output -raw state_machine_arn 2>/dev/null || echo "")
  KB_ID=$(grep knowledge_base_id "${TF_DIR}/kb.auto.tfvars" | cut -d'"' -f2)
  DS_ID=$(grep data_source_id    "${TF_DIR}/kb.auto.tfvars" | cut -d'"' -f2)

  bash scripts/run_pipeline.sh \
    --state-machine-arn "${STATE_MACHINE_ARN}" \
    --region            "${REGION}" \
    --knowledge-base-id "${KB_ID}" \
    --data-source-id    "${DS_ID}" \
    --bucket-name       "${RAG_BUCKET}" \
    --repo-url          "${REPO_URI}" \
    --wait
fi

echo ""
echo "Deploy complete."
KB_ID=$(grep knowledge_base_id "${TF_DIR}/kb.auto.tfvars" | cut -d'"' -f2)
echo "Knowledge Base ID: ${KB_ID}"
echo ""
echo "Validate retrieval:"
echo "  task pipeline:test KB_ID=${KB_ID}"
echo ""
echo "Set up MCP server for Claude Code:"
echo "  task mcp:setup KB_ID=${KB_ID}"
