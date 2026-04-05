#!/usr/bin/env bash
# setup_claude_bedrock.sh — Configure Claude Code to use Amazon Bedrock as its backend.
#
# Sets CLAUDE_CODE_USE_BEDROCK=1 and related env vars in the current shell.
# Use --persist to append them to ~/.bashrc (idempotent).
#
# Usage:
#   source scripts/setup_claude_bedrock.sh [--region REGION] [--model MODEL] [--persist]
set -euo pipefail

REGION="us-west-2"
MODEL="claude-sonnet-4-20250514"
PERSIST=false
MARKER="# claude-code-bedrock-config"

usage() {
  echo "Usage: source $0 [--region REGION] [--model MODEL] [--persist]"
  echo ""
  echo "Options:"
  echo "  --region REGION   Bedrock region (default: us-west-2)"
  echo "  --model  MODEL    Model ID (default: claude-sonnet-4-20250514)"
  echo "  --persist         Append exports to ~/.bashrc (idempotent)"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)  REGION="$2";  shift 2 ;;
    --model)   MODEL="$2";   shift 2 ;;
    --persist) PERSIST=true; shift 1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1"; usage; exit 1 ;;
  esac
done

# ── Verify AWS credentials ─────────────────────────────────────────────────────

echo "Verifying AWS credentials..."
CALLER_ID=$(aws sts get-caller-identity --output json 2>/dev/null) || {
  echo "ERROR: AWS credentials not configured. Run 'aws configure' or set AWS_* env vars."
  exit 1
}
ACCOUNT_ID=$(echo "${CALLER_ID}" | python3 -c "import sys,json; print(json.load(sys.stdin)['Account'])")
echo "OK: Authenticated as account ${ACCOUNT_ID}"

# ── Check Bedrock model access ────────────────────────────────────────────────

echo "Checking Bedrock model access..."
aws bedrock list-foundation-models \
  --region "${REGION}" \
  --by-provider anthropic \
  --query 'modelSummaries[0].modelId' \
  --output text &>/dev/null || {
  echo "WARN: Could not enumerate Bedrock models. Ensure Bedrock is accessible in ${REGION}."
}
echo "OK: Bedrock reachable in ${REGION}"

# ── Set environment variables ─────────────────────────────────────────────────

export CLAUDE_CODE_USE_BEDROCK=1
export ANTHROPIC_BEDROCK_REGION="${REGION}"
export AWS_REGION="${REGION}"
export ANTHROPIC_MODEL="${MODEL}"

echo ""
echo "Environment variables set:"
echo "  CLAUDE_CODE_USE_BEDROCK=1"
echo "  ANTHROPIC_BEDROCK_REGION=${REGION}"
echo "  AWS_REGION=${REGION}"
echo "  ANTHROPIC_MODEL=${MODEL}"

# ── Optionally persist to ~/.bashrc ───────────────────────────────────────────

if [[ "${PERSIST}" == "true" ]]; then
  if grep -q "${MARKER}" "${HOME}/.bashrc" 2>/dev/null; then
    echo ""
    echo "Config already present in ~/.bashrc — skipping (remove the '${MARKER}' block to re-add)."
  else
    cat >> "${HOME}/.bashrc" <<EOF

${MARKER}
export CLAUDE_CODE_USE_BEDROCK=1
export ANTHROPIC_BEDROCK_REGION="${REGION}"
export AWS_REGION="${REGION}"
export ANTHROPIC_MODEL="${MODEL}"
EOF
    echo ""
    echo "Appended exports to ~/.bashrc — new shells will inherit these settings."
  fi
fi

# ── Verify claude CLI ─────────────────────────────────────────────────────────

if command -v claude &>/dev/null; then
  echo ""
  echo "OK: claude CLI found — start Claude Code with 'claude'"
else
  echo ""
  echo "WARN: 'claude' CLI not found. Install Claude Code from https://claude.ai/code"
fi

echo ""
echo "Setup complete. To revert to the Anthropic API:"
echo "  unset CLAUDE_CODE_USE_BEDROCK ANTHROPIC_BEDROCK_REGION AWS_REGION ANTHROPIC_MODEL"
