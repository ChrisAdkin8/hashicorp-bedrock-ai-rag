#!/usr/bin/env bash
# run_pipeline.sh — Trigger the Step Functions RAG pipeline and optionally wait.
#
# Usage:
#   scripts/run_pipeline.sh \
#     --state-machine-arn arn:aws:states:us-east-1:123456789012:stateMachine:rag-hashicorp-pipeline \
#     --region us-east-1 \
#     --kendra-index-id ABCDEFGHIJ \
#     --kendra-data-source-id KLMNOPQRST \
#     --bucket-name hashicorp-rag-docs-us-east-1-a1b2c3d4 \
#     --repo-url https://github.com/org/repo \
#     [--target all|docs|registry|discuss|blogs]
#     [--wait]
set -euo pipefail

REGION="us-east-1"
STATE_MACHINE_ARN=""
KENDRA_INDEX_ID=""
KENDRA_DS_ID=""
BUCKET_NAME=""
REPO_URL=""
TARGET="all"
WAIT=false
POLL_INTERVAL=30

usage() {
  echo "Usage: $0 --state-machine-arn ARN --kendra-index-id ID --kendra-data-source-id ID --bucket-name NAME --repo-url URL [--region REGION] [--target all|docs|registry|discuss|blogs] [--wait]"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --state-machine-arn)     STATE_MACHINE_ARN="$2"; shift 2 ;;
    --region)                REGION="$2";            shift 2 ;;
    --kendra-index-id)       KENDRA_INDEX_ID="$2";   shift 2 ;;
    --kendra-data-source-id) KENDRA_DS_ID="$2";      shift 2 ;;
    --bucket-name)           BUCKET_NAME="$2";       shift 2 ;;
    --repo-url)              REPO_URL="$2";          shift 2 ;;
    --target)                TARGET="$2";            shift 2 ;;
    --wait)                  WAIT=true;              shift 1 ;;
    -h|--help)               usage ;;
    *) echo "Unknown argument: $1"; usage ;;
  esac
done

if [[ -z "${STATE_MACHINE_ARN}" || -z "${KENDRA_INDEX_ID}" || -z "${KENDRA_DS_ID}" ]]; then
  echo "ERROR: --state-machine-arn, --kendra-index-id, and --kendra-data-source-id are required"
  usage
fi

case "${TARGET}" in
  all|docs|registry|discuss|blogs) ;;
  *) echo "ERROR: --target must be one of: all, docs, registry, discuss, blogs"; exit 1 ;;
esac

# Derive the actual SFN region from the ARN (format: arn:aws:states:<region>:<acct>:stateMachine:<name>)
SFN_REGION=$(echo "${STATE_MACHINE_ARN}" | cut -d: -f4)
if [[ -z "${SFN_REGION}" ]]; then
  SFN_REGION="${REGION}"
fi
echo "Step Functions region: ${SFN_REGION}"

INPUT_JSON=$(KENDRA_INDEX_ID="${KENDRA_INDEX_ID}" \
  KENDRA_DS_ID="${KENDRA_DS_ID}" \
  BUCKET_NAME="${BUCKET_NAME}" \
  REPO_URL="${REPO_URL}" \
  SFN_REGION="${SFN_REGION}" \
  TARGET="${TARGET}" \
  python3 -c "
import json, os
print(json.dumps({
  'kendra_index_id':       os.environ['KENDRA_INDEX_ID'],
  'kendra_data_source_id': os.environ['KENDRA_DS_ID'],
  'bucket_name':           os.environ['BUCKET_NAME'],
  'repo_url':              os.environ['REPO_URL'],
  'region':                os.environ['SFN_REGION'],
  'pipeline_target':       os.environ['TARGET'],
}))
")

echo "Starting Step Functions execution..."
EXECUTION_ARN=$(aws stepfunctions start-execution \
  --state-machine-arn "${STATE_MACHINE_ARN}" \
  --input             "${INPUT_JSON}" \
  --region            "${SFN_REGION}" \
  --query             'executionArn' \
  --output            text)

echo "Execution ARN: ${EXECUTION_ARN}"

if [[ "${WAIT}" == "true" ]]; then
  echo "Waiting for execution to complete (polling every ${POLL_INTERVAL}s)..."
  while true; do
    STATUS=$(aws stepfunctions describe-execution \
      --execution-arn "${EXECUTION_ARN}" \
      --region        "${SFN_REGION}" \
      --query         'status' \
      --output        text)
    echo "  Status: ${STATUS}"
    case "${STATUS}" in
      SUCCEEDED) echo "Execution SUCCEEDED."; break ;;
      FAILED|TIMED_OUT|ABORTED)
        echo "Execution ${STATUS}."
        echo "  Execution ARN: ${EXECUTION_ARN}"
        echo "  Logs: aws logs tail /aws/codebuild/rag-hashicorp-pipeline --follow"
        echo "  Console: https://console.aws.amazon.com/states/home?region=${SFN_REGION}#/executions/details/${EXECUTION_ARN}"
        exit 1
        ;;
      *) sleep "${POLL_INTERVAL}" ;;
    esac
  done
else
  echo "Not waiting — check execution status:"
  echo "  aws stepfunctions describe-execution --execution-arn ${EXECUTION_ARN}"
fi
