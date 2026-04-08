#!/usr/bin/env bash
# setup_mcp.sh — Register the HashiCorp RAG MCP server with Claude Code.
#
# Writes the mcpServers entry into .claude/settings.local.json so that
# Claude Code starts the MCP server automatically when opened in this directory.
#
# Usage:
#   scripts/setup_mcp.sh --kendra-index-id ABCDEFGHIJ [--region us-east-1] \
#     [--neptune-endpoint <endpoint>] [--neptune-port 8182]
set -euo pipefail

REGION="us-east-1"
KENDRA_INDEX_ID=""
NEPTUNE_ENDPOINT=""
NEPTUNE_PORT="8182"
SETTINGS_FILE=".claude/settings.local.json"
PYTHON=".venv/bin/python3"

usage() {
  echo "Usage: $0 --kendra-index-id ID [--region REGION] [--neptune-endpoint EP] [--neptune-port PORT]"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --kendra-index-id)   KENDRA_INDEX_ID="$2";   shift 2 ;;
    --region)            REGION="$2";             shift 2 ;;
    --neptune-endpoint)  NEPTUNE_ENDPOINT="$2";   shift 2 ;;
    --neptune-port)      NEPTUNE_PORT="$2";       shift 2 ;;
    -h|--help)           usage ;;
    *) echo "Unknown argument: $1"; usage ;;
  esac
done

if [[ -z "${KENDRA_INDEX_ID}" ]]; then
  echo "ERROR: --kendra-index-id is required"
  usage
fi

SERVER_PATH="$(pwd)/mcp/server.py"
if [[ ! -f "${SERVER_PATH}" ]]; then
  echo "ERROR: MCP server not found at ${SERVER_PATH}"
  exit 1
fi

mkdir -p "$(dirname "${SETTINGS_FILE}")"

# Build the env dict — include Neptune vars only when endpoint is provided
"${PYTHON}" -c "
import json, os
path = '${SETTINGS_FILE}'
existing = json.loads(open(path).read()) if os.path.exists(path) else {}
existing.setdefault('mcpServers', {})
env = {
    'AWS_REGION': '${REGION}',
    'AWS_KENDRA_INDEX_ID': '${KENDRA_INDEX_ID}',
}
neptune_endpoint = '${NEPTUNE_ENDPOINT}'
if neptune_endpoint:
    env['NEPTUNE_ENDPOINT'] = neptune_endpoint
    env['NEPTUNE_PORT'] = '${NEPTUNE_PORT}'
    env['NEPTUNE_IAM_AUTH'] = 'true'
existing['mcpServers']['hashicorp-rag'] = {
    'command': '${PYTHON}',
    'args': ['${SERVER_PATH}'],
    'env': env,
}
print(json.dumps(existing, indent=2))
" > "${SETTINGS_FILE}"

echo "Wrote MCP server config to ${SETTINGS_FILE}:"
echo "  Server:             ${SERVER_PATH}"
echo "  AWS_REGION:         ${REGION}"
echo "  KENDRA_INDEX_ID:    ${KENDRA_INDEX_ID}"
if [[ -n "${NEPTUNE_ENDPOINT}" ]]; then
  echo "  NEPTUNE_ENDPOINT:   ${NEPTUNE_ENDPOINT}"
  echo "  NEPTUNE_PORT:       ${NEPTUNE_PORT}"
fi
echo ""
echo "Restart Claude Code to activate the MCP server."
echo "Available tools: search_hashicorp_docs, get_resource_dependencies, find_resources_by_type, get_index_info"
