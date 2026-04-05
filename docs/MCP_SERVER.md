# MCP Server — HashiCorp RAG

The `mcp/server.py` server exposes the Bedrock Knowledge Base as two tools callable from Claude Code via the [Model Context Protocol](https://modelcontextprotocol.io).

## Tools

### `search_hashicorp_docs`

Performs a hybrid (vector + keyword) search against the knowledge base.

**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | (required) | Natural-language search query |
| `top_k` | int | `5` | Maximum results to return |
| `min_score` | float | `0.0` | Minimum relevance score (0–1) |
| `product_family` | string | `""` | Filter: `terraform`, `vault`, `consul`, `nomad`, `packer`, `boundary`, `sentinel` |
| `source_type` | string | `""` | Filter: `documentation`, `provider`, `module`, `issue`, `discuss`, `blog` |

**Returns:** List of result dicts with `text`, `score`, `source_uri`, `product`, `product_family`, `source_type`.

### `get_knowledge_base_info`

Returns the active region, knowledge base ID, AWS account, and knowledge base status. Use for diagnostics.

---

## Setup

### 1. Install dependencies

```bash
task mcp:install
```

### 2. Register with Claude Code

```bash
task mcp:setup KB_ID=ABCDEFGHIJ
```

This writes to `.claude/settings.local.json`. Restart Claude Code to activate.

### 3. Smoke test

```bash
task mcp:test KB_ID=ABCDEFGHIJ
```

---

## Manual configuration

If you prefer to configure the MCP server manually, add this to `.claude/settings.local.json`:

```json
{
  "mcpServers": {
    "hashicorp-rag": {
      "command": ".venv/bin/python3",
      "args": ["/path/to/mcp/server.py"],
      "env": {
        "AWS_REGION": "us-west-2",
        "AWS_KNOWLEDGE_BASE_ID": "ABCDEFGHIJ"
      }
    }
  }
}
```

---

## Metadata inference

Bedrock's `retrieve()` API returns the S3 URI for each result but not the custom metadata from the `.metadata.json` sidecar files. The server infers `product`, `product_family`, and `source_type` from the S3 object path structure:

| Path pattern | product_family | source_type |
|---|---|---|
| `provider/terraform-provider-{product}/...` | `terraform` | `provider` |
| `documentation/{product}/...` | `{product}` | `documentation` |
| `module/...` | `terraform` | `module` |
| `sentinel/...` | `terraform` | `sentinel` |
| `issues/{org}/{repo}/...` | `{repo}` | `issue` |
| `discuss/{category}/...` | `{category}` | `discuss` |
| `blogs/{source}/...` | `hashicorp` | `blog` |

---

## Authentication

The server uses the standard AWS credential chain — no additional configuration beyond what you use for `aws` CLI commands. The credential chain is checked in this order:

1. `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` environment variables
2. `~/.aws/credentials` file
3. Instance profile (EC2/ECS/Lambda)
4. AWS SSO (`aws sso login --profile my-profile`)

The `AWS_REGION` and `AWS_KNOWLEDGE_BASE_ID` env vars are written to the `mcpServers` entry by `task mcp:setup` and passed to the server process automatically.
