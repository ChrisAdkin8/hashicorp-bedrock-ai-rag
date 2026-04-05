#!/usr/bin/env python3
"""server.py — MCP server exposing the Bedrock Knowledge Base as Claude Code tools.

Tools:
  - search_hashicorp_docs   — semantic/hybrid search with optional metadata filters
  - get_knowledge_base_info — inspect active region/knowledge-base configuration

Environment variables:
  AWS_REGION              — AWS region (default: us-west-2)
  AWS_KNOWLEDGE_BASE_ID   — Bedrock Knowledge Base ID (required)
  Standard AWS credential chain (env vars, ~/.aws/credentials, instance profile, SSO)
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import boto3
from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

mcp = FastMCP("hashicorp-rag")

AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
KNOWLEDGE_BASE_ID = os.environ.get("AWS_KNOWLEDGE_BASE_ID", "")
DEFAULT_TOP_K = 5
DEFAULT_MIN_SCORE = 0.0


def _bedrock_client() -> Any:
    """Return a boto3 bedrock-agent-runtime client."""
    return boto3.client("bedrock-agent-runtime", region_name=AWS_REGION)


def _infer_metadata(s3_uri: str) -> dict[str, str]:
    """Infer product, product_family, and source_type from an S3 object path.

    Mirrors the path structure written by process_docs.py and the fetch scripts.
    Examples:
      s3://bucket/provider/terraform-provider-aws/...  -> product_family=terraform, source_type=provider
      s3://bucket/documentation/vault/...              -> product_family=vault, source_type=documentation
      s3://bucket/issues/hashicorp/vault/...           -> source_type=issue
      s3://bucket/discuss/vault/...                    -> source_type=discuss
      s3://bucket/blogs/hashicorp-blog/...             -> source_type=blog
    """
    # Strip scheme and bucket
    path = re.sub(r"^s3://[^/]+/", "", s3_uri)
    parts = path.split("/")

    raw_source = parts[0] if parts else ""

    source_type_map = {
        "documentation": "documentation",
        "provider": "provider",
        "module": "module",
        "sentinel": "sentinel",
        "issues": "issue",
        "discuss": "discuss",
        "blogs": "blog",
    }
    source_type = source_type_map.get(raw_source, raw_source)

    product = "hashicorp"
    product_family = "hashicorp"

    if source_type == "provider" and len(parts) >= 2:
        product = parts[1].removeprefix("terraform-provider-")
        product_family = "terraform"
    elif source_type == "documentation" and len(parts) >= 2:
        product = parts[1]
        product_family = parts[1]
    elif source_type == "module":
        product = "terraform"
        product_family = "terraform"
    elif source_type == "sentinel":
        product = "sentinel"
        product_family = "terraform"
    elif source_type == "issue" and len(parts) >= 3:
        product = parts[2]
        product_family = parts[2]
    elif source_type == "discuss" and len(parts) >= 2:
        product = parts[1]
        product_family = parts[1]
    elif source_type == "blog" and len(parts) >= 2:
        product = parts[1]
        product_family = "hashicorp"

    return {"product": product, "product_family": product_family, "source_type": source_type}


@mcp.tool()
def search_hashicorp_docs(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
    product_family: str = "",
    source_type: str = "",
) -> list[dict]:
    """Search the HashiCorp documentation Knowledge Base.

    Performs a hybrid (vector + keyword) search against the Bedrock Knowledge Base
    containing HashiCorp documentation, provider references, GitHub issues,
    Discuss forum threads, and blog posts.

    Args:
        query:          Natural-language search query.
        top_k:          Maximum number of results to return (default 5).
        min_score:      Minimum relevance score 0-1 (default 0.0, returns all results).
        product_family: Optional filter — terraform, vault, consul, nomad, packer, boundary, sentinel.
        source_type:    Optional filter — documentation, provider, module, issue, discuss, blog.

    Returns:
        List of result dicts with keys: text, score, source_uri, product, product_family, source_type.
    """
    if not KNOWLEDGE_BASE_ID:
        return [{"error": "AWS_KNOWLEDGE_BASE_ID environment variable is not set."}]

    client = _bedrock_client()

    retrieval_config: dict = {
        "vectorSearchConfiguration": {
            "numberOfResults": top_k * 2 if (product_family or source_type) else top_k,
            "overrideSearchType": "HYBRID",
        }
    }

    try:
        resp = client.retrieve(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            retrievalQuery={"text": query},
            retrievalConfiguration=retrieval_config,
        )
    except Exception as exc:
        log.error("Bedrock retrieve error: %s", exc)
        return [{"error": str(exc)}]

    results = resp.get("retrievalResults", [])
    output: list[dict] = []

    for result in results:
        score = result.get("score", 0.0)
        if score < min_score:
            continue

        text = result.get("content", {}).get("text", "")
        location = result.get("location", {})
        s3_uri = location.get("s3Location", {}).get("uri", "")
        inferred = _infer_metadata(s3_uri)

        # Apply client-side metadata filters
        if product_family and inferred.get("product_family") != product_family:
            continue
        if source_type and inferred.get("source_type") != source_type:
            continue

        output.append(
            {
                "text": text,
                "score": round(score, 4),
                "source_uri": s3_uri,
                "product": inferred["product"],
                "product_family": inferred["product_family"],
                "source_type": inferred["source_type"],
            }
        )

        if len(output) >= top_k:
            break

    return output


@mcp.tool()
def get_knowledge_base_info() -> dict:
    """Return the active Knowledge Base configuration.

    Returns:
        Dict with region, knowledge_base_id, and boto3 identity information.
    """
    info: dict = {
        "region": AWS_REGION,
        "knowledge_base_id": KNOWLEDGE_BASE_ID or "(not set — AWS_KNOWLEDGE_BASE_ID missing)",
    }

    try:
        sts = boto3.client("sts", region_name=AWS_REGION)
        identity = sts.get_caller_identity()
        info["aws_account_id"] = identity.get("Account", "unknown")
        info["aws_arn"] = identity.get("Arn", "unknown")
    except Exception as exc:
        info["auth_error"] = str(exc)

    if KNOWLEDGE_BASE_ID:
        try:
            agent = boto3.client("bedrock-agent", region_name=AWS_REGION)
            kb = agent.get_knowledge_base(knowledgeBaseId=KNOWLEDGE_BASE_ID)
            info["knowledge_base_name"] = kb["knowledgeBase"].get("name", "")
            info["knowledge_base_status"] = kb["knowledgeBase"].get("status", "")
        except Exception as exc:
            info["kb_error"] = str(exc)

    return info


if __name__ == "__main__":
    mcp.run()
