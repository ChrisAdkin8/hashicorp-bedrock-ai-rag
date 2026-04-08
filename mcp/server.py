#!/usr/bin/env python3
"""server.py — MCP server exposing Kendra and Neptune as Claude Code tools.

Tools:
  - search_hashicorp_docs       — keyword/semantic search with optional metadata filters
  - get_resource_dependencies   — traverse Terraform resource dependency graph in Neptune
  - find_resources_by_type      — list Terraform resources of a given type in Neptune
  - get_index_info              — inspect active region/index/Neptune configuration

Environment variables:
  AWS_REGION          — AWS region (defaults to boto3 session region, then us-east-1)
  AWS_KENDRA_INDEX_ID — Kendra index ID (required for Kendra tools)
  NEPTUNE_ENDPOINT    — Neptune cluster endpoint (required for graph tools)
  NEPTUNE_PORT        — Neptune port (default 8182)
  NEPTUNE_IAM_AUTH    — Enable SigV4 auth for Neptune (default "true")
  Standard AWS credential chain (env vars, ~/.aws/credentials, instance profile, SSO)
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
from typing import Any

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

mcp = FastMCP("hashicorp-rag")

AWS_REGION = os.environ.get("AWS_REGION") or boto3.session.Session().region_name or "us-east-1"
KENDRA_INDEX_ID = os.environ.get("AWS_KENDRA_INDEX_ID", "")
NEPTUNE_ENDPOINT = os.environ.get("NEPTUNE_ENDPOINT", "")
NEPTUNE_PORT = int(os.environ.get("NEPTUNE_PORT", "8182"))
NEPTUNE_IAM_AUTH = os.environ.get("NEPTUNE_IAM_AUTH", "true").lower() == "true"

# Kendra confidence levels mapped to numeric equivalents for min_score filtering
CONFIDENCE_SCORE: dict[str, float] = {
    "VERY_HIGH":     1.00,
    "HIGH":          0.75,
    "MEDIUM":        0.50,
    "LOW":           0.25,
    "NOT_AVAILABLE": 0.00,
}


def _kendra_client() -> Any:
    return boto3.client("kendra", region_name=AWS_REGION)


def _neptune_query(query: str, parameters: dict | None = None) -> dict:
    """Execute an openCypher query against Neptune via SigV4-signed HTTP POST.

    Creates fresh credentials on each call to handle temporary credential
    expiry in the long-running MCP server process.
    """
    if not NEPTUNE_ENDPOINT:
        return {"error": "NEPTUNE_ENDPOINT environment variable is not set."}

    url = f"https://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/openCypher"
    params = parameters or {}
    body = urllib.parse.urlencode({
        "query": query,
        "parameters": json.dumps(params),
    })
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    if NEPTUNE_IAM_AUTH:
        creds = boto3.Session().get_credentials().get_frozen_credentials()
        aws_req = AWSRequest(method="POST", url=url, data=body, headers=headers)
        SigV4Auth(creds, "neptune-db", AWS_REGION).add_auth(aws_req)
        headers = dict(aws_req.headers)

    try:
        resp = requests.post(url, data=body, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.ConnectionError:
        return {"error": f"Cannot connect to Neptune at {NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}. "
                "Neptune is VPC-only — ensure the MCP server can reach the cluster "
                "(SSH tunnel, VPN, or run from within the VPC)."}
    except requests.exceptions.HTTPError as exc:
        return {"error": f"Neptune HTTP {exc.response.status_code}: {exc.response.text[:500]}"}
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
def search_hashicorp_docs(
    query: str,
    top_k: int = 5,
    min_score: float = 0.0,
    product_family: str = "",
    source_type: str = "",
) -> list[dict]:
    """Search the HashiCorp documentation Kendra index.

    Performs a keyword + semantic search against the Kendra index containing
    HashiCorp documentation, provider references, GitHub issues, Discuss forum
    threads, and blog posts.

    Args:
        query:          Natural-language search query.
        top_k:          Maximum number of results to return (default 5).
        min_score:      Minimum confidence score 0-1 (VERY_HIGH=1.0, HIGH=0.75,
                        MEDIUM=0.5, LOW=0.25). Default 0.0 returns all results.
        product_family: Optional filter — terraform, vault, consul, nomad, packer,
                        boundary, sentinel.
        source_type:    Optional filter — documentation, provider, module, issue,
                        discuss, blog.

    Returns:
        List of result dicts with keys: text, score, confidence, source_uri,
        product, product_family, source_type.
    """
    if not KENDRA_INDEX_ID:
        return [{"error": "AWS_KENDRA_INDEX_ID environment variable is not set."}]

    client = _kendra_client()

    params: dict = {
        "IndexId":              KENDRA_INDEX_ID,
        "QueryText":            query,
        "PageSize":             top_k * 2 if (product_family or source_type) else top_k,
        "QueryResultTypeFilter": "DOCUMENT",
    }

    # Push metadata filters down to Kendra for efficiency
    filters = []
    if product_family:
        filters.append({
            "EqualsTo": {"Key": "product_family", "Value": {"StringValue": product_family}}
        })
    if source_type:
        filters.append({
            "EqualsTo": {"Key": "source_type", "Value": {"StringValue": source_type}}
        })
    if len(filters) == 1:
        params["AttributeFilter"] = filters[0]
    elif len(filters) > 1:
        params["AttributeFilter"] = {"AndAllFilters": filters}

    try:
        resp = client.query(**params)
    except Exception as exc:
        log.error("Kendra query error: %s", exc)
        return [{"error": str(exc)}]

    output: list[dict] = []
    for item in resp.get("ResultItems", []):
        confidence = item.get("ScoreAttributes", {}).get("ScoreConfidence", "NOT_AVAILABLE")
        score = CONFIDENCE_SCORE.get(confidence, 0.0)
        if score < min_score:
            continue

        text = item.get("DocumentExcerpt", {}).get("Text", "")
        doc_uri = item.get("DocumentURI", item.get("DocumentId", ""))

        # Kendra returns custom attributes directly — no path inference needed
        attrs = {
            a["Key"]: a["Value"].get("StringValue", "")
            for a in item.get("DocumentAttributes", [])
            if "StringValue" in a.get("Value", {})
        }

        output.append({
            "text":           text,
            "score":          score,
            "confidence":     confidence,
            "source_uri":     doc_uri,
            "product":        attrs.get("product", "hashicorp"),
            "product_family": attrs.get("product_family", "hashicorp"),
            "source_type":    attrs.get("source_type", ""),
        })

        if len(output) >= top_k:
            break

    return output


@mcp.tool()
def get_resource_dependencies(
    resource_type: str,
    resource_name: str,
    direction: str = "both",
    max_depth: int = 2,
) -> list[dict]:
    """Traverse the Terraform resource dependency graph in Neptune.

    Finds resources that a given resource depends on (downstream), resources
    that depend on it (upstream), or both.

    Args:
        resource_type: Terraform resource type (e.g. "aws_lambda_function").
        resource_name: Terraform resource name (e.g. "processor").
        direction:     "downstream" (what this depends on), "upstream" (what
                       depends on this), or "both" (default).
        max_depth:     Maximum traversal depth (default 2, max 5).

    Returns:
        List of dicts with keys: resource_id, type, name, direction, repository.
    """
    if not NEPTUNE_ENDPOINT:
        return [{"error": "NEPTUNE_ENDPOINT environment variable is not set."}]

    max_depth = min(max(1, max_depth), 5)
    address = f"{resource_type}.{resource_name}"
    results: list[dict] = []

    if direction in ("downstream", "both"):
        query = (
            f"MATCH (a:Resource)-[:DEPENDS_ON*1..{max_depth}]->(b:Resource) "
            "WHERE a.id = $address "
            "RETURN DISTINCT b.id AS resource_id, b.type AS type, b.name AS name, b.repo AS repository"
        )
        resp = _neptune_query(query, {"address": address})
        if "error" in resp:
            return [resp]
        for row in resp.get("results", []):
            row["direction"] = "downstream"
            results.append(row)

    if direction in ("upstream", "both"):
        query = (
            f"MATCH (b:Resource)-[:DEPENDS_ON*1..{max_depth}]->(a:Resource) "
            "WHERE a.id = $address "
            "RETURN DISTINCT b.id AS resource_id, b.type AS type, b.name AS name, b.repo AS repository"
        )
        resp = _neptune_query(query, {"address": address})
        if "error" in resp:
            return [resp]
        for row in resp.get("results", []):
            row["direction"] = "upstream"
            results.append(row)

    if not results:
        return [{"info": f"No dependencies found for '{address}'. "
                 "Check that the graph has been populated (task graph:populate) "
                 "and the resource address is correct."}]

    return results


@mcp.tool()
def find_resources_by_type(
    resource_type: str,
    repository: str = "",
) -> list[dict]:
    """List all Terraform resources of a given type from the Neptune graph.

    Args:
        resource_type: Terraform resource type (e.g. "aws_s3_bucket",
                       "aws_iam_role").
        repository:    Optional — filter to resources in a specific repository
                       (GitHub HTTPS URL or repo name).

    Returns:
        List of dicts with keys: resource_id, type, name, repository.
    """
    if not NEPTUNE_ENDPOINT:
        return [{"error": "NEPTUNE_ENDPOINT environment variable is not set."}]

    if repository:
        query = (
            "MATCH (repo:Repository)-[:CONTAINS]->(r:Resource) "
            "WHERE r.type = $type AND (repo.uri = $repo OR repo.name = $repo) "
            "RETURN r.id AS resource_id, r.type AS type, r.name AS name, repo.uri AS repository"
        )
        resp = _neptune_query(query, {"type": resource_type, "repo": repository})
    else:
        query = (
            "MATCH (r:Resource) WHERE r.type = $type "
            "RETURN r.id AS resource_id, r.type AS type, r.name AS name, r.repo AS repository"
        )
        resp = _neptune_query(query, {"type": resource_type})

    if "error" in resp:
        return [resp]

    results = resp.get("results", [])
    if not results:
        return [{"info": f"No resources of type '{resource_type}' found. "
                 "Check that the graph has been populated (task graph:populate)."}]
    return results


@mcp.tool()
def get_index_info() -> dict:
    """Return the active Kendra index and Neptune graph configuration.

    Returns:
        Dict with region, Kendra index status, Neptune connectivity, and
        caller identity.
    """
    info: dict = {
        "region":           AWS_REGION,
        "kendra_index_id":  KENDRA_INDEX_ID or "(not set — AWS_KENDRA_INDEX_ID missing)",
    }

    try:
        sts = boto3.client("sts", region_name=AWS_REGION)
        identity = sts.get_caller_identity()
        info["aws_account_id"] = identity.get("Account", "unknown")
        info["aws_arn"] = identity.get("Arn", "unknown")
    except Exception as exc:
        info["auth_error"] = str(exc)

    if KENDRA_INDEX_ID:
        try:
            kendra = boto3.client("kendra", region_name=AWS_REGION)
            resp = kendra.describe_index(Id=KENDRA_INDEX_ID)
            info["index_name"]   = resp.get("Name", "")
            info["index_status"] = resp.get("Status", "")
            info["edition"]      = resp.get("Edition", "")
        except Exception as exc:
            info["index_error"] = str(exc)

    # Neptune status
    if NEPTUNE_ENDPOINT:
        info["neptune_endpoint"] = NEPTUNE_ENDPOINT
        info["neptune_port"] = NEPTUNE_PORT
        info["neptune_iam_auth"] = NEPTUNE_IAM_AUTH
        resp = _neptune_query(
            "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS cnt ORDER BY cnt DESC"
        )
        if "error" in resp:
            info["neptune_error"] = resp["error"]
        else:
            info["neptune_status"] = "connected"
            info["neptune_node_counts"] = resp.get("results", [])
    else:
        info["neptune_status"] = "(not configured — NEPTUNE_ENDPOINT missing)"

    return info


if __name__ == "__main__":
    mcp.run()
