#!/usr/bin/env bash
# setup_mcp.sh — Register the HashiCorp RAG MCP server with Claude Code.
#
# Writes the mcpServers entry into .claude/settings.local.json so that
# Claude Code starts the MCP server automatically when opened in this directory.
#
# Usage:
#   scripts/setup_mcp.sh --knowledge-base-id ABCDEFGHIJ [--region us-west-2]
set -euo pipefail

REGION="us-west-2"
KB_ID=""
SETTINGS_FILE=".claude/settings.local.json"
PYTHON=".venv/bin/python3"

usage() {
  echo "Usage: $0 --knowledge-base-id KB_ID [--region REGION]"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --knowledge-base-id) KB_ID="$2";   shift 2 ;;
    --region)            REGION="$2";  shift 2 ;;
    -h|--help)           usage ;;
    *) echo "Unknown argument: $1"; usage ;;
  esac
done

if [[ -z "${KB_ID}" ]]; then
  echo "ERROR: --knowledge-base-id is required"
  usage
fi

SERVER_PATH="$(pwd)/mcp/server.py"
if [[ ! -f "${SERVER_PATH}" ]]; then
  echo "ERROR: MCP server not found at ${SERVER_PATH}"
  exit 1
fi

mkdir -p "$(dirname "${SETTINGS_FILE}")"

# Merge the MCP server entry using Python, writing output to settings file
"${PYTHON}" -c "
import json, os
path = '${SETTINGS_FILE}'
existing = json.loads(open(path).read()) if os.path.exists(path) else {}
existing.setdefault('mcpServers', {})
existing['mcpServers']['hashicorp-rag'] = {
    'command': '${PYTHON}',
    'args': ['${SERVER_PATH}'],
    'env': {
        'AWS_REGION': '${REGION}',
        'AWS_KNOWLEDGE_BASE_ID': '${KB_ID}'
    }
}
print(json.dumps(existing, indent=2))
" > "${SETTINGS_FILE}"

echo "Wrote MCP server config to ${SETTINGS_FILE}:"
echo "  Server:           ${SERVER_PATH}"
echo "  AWS_REGION:       ${REGION}"
echo "  KB_ID:            ${KB_ID}"
echo ""
echo "Restart Claude Code to activate the MCP server."
echo "Available tools: search_hashicorp_docs, get_knowledge_base_info"
