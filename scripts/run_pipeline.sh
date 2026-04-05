#!/usr/bin/env bash
# run_pipeline.sh — Trigger the Step Functions RAG pipeline and optionally wait.
#
# Usage:
#   scripts/run_pipeline.sh \
#     --state-machine-arn arn:aws:states:us-west-2:123456789012:stateMachine:rag-hashicorp-pipeline \
#     --region us-west-2 \
#     --knowledge-base-id ABCDEFGHIJ \
#     --data-source-id KLMNOPQRST \
#     --bucket-name hashicorp-rag-docs-a1b2c3d4 \
#     --repo-url https://github.com/org/repo \
#     [--wait]
set -euo pipefail

REGION="us-west-2"
STATE_MACHINE_ARN=""
KB_ID=""
DS_ID=""
BUCKET_NAME=""
REPO_URL=""
WAIT=false
POLL_INTERVAL=30

usage() {
  echo "Usage: $0 --state-machine-arn ARN --knowledge-base-id ID --data-source-id ID --bucket-name NAME --repo-url URL [--region REGION] [--wait]"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --state-machine-arn)  STATE_MACHINE_ARN="$2"; shift 2 ;;
    --region)             REGION="$2";            shift 2 ;;
    --knowledge-base-id)  KB_ID="$2";             shift 2 ;;
    --data-source-id)     DS_ID="$2";             shift 2 ;;
    --bucket-name)        BUCKET_NAME="$2";       shift 2 ;;
    --repo-url)           REPO_URL="$2";          shift 2 ;;
    --wait)               WAIT=true;              shift 1 ;;
    -h|--help)            usage ;;
    *) echo "Unknown argument: $1"; usage ;;
  esac
done

if [[ -z "${STATE_MACHINE_ARN}" || -z "${KB_ID}" || -z "${DS_ID}" ]]; then
  echo "ERROR: --state-machine-arn, --knowledge-base-id, and --data-source-id are required"
  usage
fi

INPUT_JSON=$(python3 -c "
import json
print(json.dumps({
  'knowledge_base_id': '${KB_ID}',
  'data_source_id':    '${DS_ID}',
  'bucket_name':       '${BUCKET_NAME}',
  'repo_url':          '${REPO_URL}',
  'region':            '${REGION}',
}))
")

echo "Starting Step Functions execution..."
EXECUTION_ARN=$(aws stepfunctions start-execution \
  --state-machine-arn "${STATE_MACHINE_ARN}" \
  --input             "${INPUT_JSON}" \
  --region            "${REGION}" \
  --query             'executionArn' \
  --output            text)

echo "Execution ARN: ${EXECUTION_ARN}"

if [[ "${WAIT}" == "true" ]]; then
  echo "Waiting for execution to complete (polling every ${POLL_INTERVAL}s)..."
  while true; do
    STATUS=$(aws stepfunctions describe-execution \
      --execution-arn "${EXECUTION_ARN}" \
      --region        "${REGION}" \
      --query         'status' \
      --output        text)
    echo "  Status: ${STATUS}"
    case "${STATUS}" in
      SUCCEEDED) echo "Execution SUCCEEDED."; break ;;
      FAILED|TIMED_OUT|ABORTED)
        echo "Execution ${STATUS}. Check CloudWatch Logs or Step Functions console."
        exit 1
        ;;
      *) sleep "${POLL_INTERVAL}" ;;
    esac
  done
else
  echo "Not waiting — check execution status:"
  echo "  aws stepfunctions describe-execution --execution-arn ${EXECUTION_ARN}"
fi
