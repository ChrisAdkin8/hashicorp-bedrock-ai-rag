#!/usr/bin/env python3
"""MCP server exposing the HashiCorp knowledge base on AWS.

Implements five tools across two backends:
  Kendra RAG index
    - search_hashicorp_docs       — semantic search with metadata filters
    - get_index_info              — inspect active index configuration
  Neptune Graph (openCypher property graph)
    - get_resource_dependencies   — traverse Terraform resource dependencies
    - find_resources_by_type      — list resources of a given type
    - get_graph_info              — inspect graph store configuration + counts

Environment variables:
    AWS_REGION          — AWS region (defaults to boto3 session region, then us-east-1)
    AWS_KENDRA_INDEX_ID — Kendra index ID (required for Kendra tools)
    NEPTUNE_ENDPOINT    — Neptune cluster endpoint (required for direct graph access)
    NEPTUNE_PORT        — Neptune port (default 8182)
    NEPTUNE_IAM_AUTH    — Enable SigV4 auth for Neptune (default "true")
    NEPTUNE_PROXY_URL   — API Gateway URL for Neptune proxy (overrides direct access)

Authentication uses the standard AWS credential chain
(env vars, ~/.aws/credentials, instance profile, SSO).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.parse
from typing import Any

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

AWS_REGION = os.environ.get("AWS_REGION") or boto3.session.Session().region_name or "us-east-1"
KENDRA_INDEX_ID = os.environ.get("AWS_KENDRA_INDEX_ID", "")
NEPTUNE_ENDPOINT = os.environ.get("NEPTUNE_ENDPOINT", "")
NEPTUNE_PORT = int(os.environ.get("NEPTUNE_PORT", "8182"))
NEPTUNE_IAM_AUTH = os.environ.get("NEPTUNE_IAM_AUTH", "true").lower() == "true"
NEPTUNE_PROXY_URL = os.environ.get("NEPTUNE_PROXY_URL", "")

# Kendra confidence levels mapped to numeric equivalents for min_score filtering
CONFIDENCE_SCORE: dict[str, float] = {
    "VERY_HIGH":     1.00,
    "HIGH":          0.75,
    "MEDIUM":        0.50,
    "LOW":           0.25,
    "NOT_AVAILABLE": 0.00,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _kendra_client() -> Any:
    return boto3.client("kendra", region_name=AWS_REGION)


def _extract_uri_metadata(source_uri: str) -> dict[str, str]:
    """Infer product, product_family, and source_type from the S3 object path.

    URI structure (after the bucket prefix):
      provider/terraform-provider-{product}/...  -> product_family=terraform, source_type=provider
      documentation/{product}/...                -> product_family={product}, source_type=documentation
      issues/{product}/...                        -> product_family={product}, source_type=issue
      module/{name}/...                           -> product_family=terraform, source_type=module
      sentinel/{name}/...                         -> product_family=sentinel, source_type=sentinel
      blogs/{...}                                 -> source_type=blog
      discuss/{...}                               -> source_type=discuss

    Falls back to empty strings when the pattern is unrecognised.
    """
    path = source_uri
    if path.startswith("s3://"):
        parts = path.split("/", 3)  # ["s3:", "", "bucket", "rest"]
        path = parts[3] if len(parts) > 3 else ""
    elif path.startswith("https://"):
        # HTTPS S3 URL — strip host + bucket
        parts = path.split("/", 4)
        path = parts[4] if len(parts) > 4 else ""

    segments = path.split("/")
    top = segments[0] if segments else ""

    if top == "provider":
        repo = segments[1] if len(segments) > 1 else ""
        product = repo.replace("terraform-provider-", "") if repo.startswith("terraform-provider-") else repo
        return {"product": product, "product_family": "terraform", "source_type": "provider"}

    if top == "documentation":
        product = segments[1] if len(segments) > 1 else ""
        return {"product": product, "product_family": product, "source_type": "documentation"}

    if top == "issues":
        product = segments[1] if len(segments) > 1 else ""
        return {"product": product, "product_family": product, "source_type": "issue"}

    if top == "module":
        return {"product": "terraform", "product_family": "terraform", "source_type": "module"}

    if top == "sentinel":
        return {"product": "sentinel", "product_family": "sentinel", "source_type": "sentinel"}

    if top in ("blogs", "blog"):
        return {"product": "", "product_family": "", "source_type": "blog"}

    if top == "discuss":
        return {"product": "", "product_family": "", "source_type": "discuss"}

    return {"product": "", "product_family": "", "source_type": ""}


def _short_source_uri(source_uri: str) -> str:
    """Return a compact, human-readable path from a full S3 URI.

    Strips the ``s3://bucket/`` prefix, leaving only the object path.
    Falls back to the original string when the URI is not an S3 path.
    """
    if source_uri.startswith("s3://"):
        parts = source_uri.split("/", 3)  # ["s3:", "", "bucket", "rest"]
        if len(parts) > 3:
            return parts[3]
    return source_uri


def _strip_chunk_header(text: str) -> str:
    """Remove the compact metadata header prefix injected by process_docs.py.

    The header (e.g. ``[provider:aws] aws_instance -- Arguments\\n\\n``) is
    already conveyed by the source URI, so repeating it in every chunk wastes
    tokens.
    """
    return re.sub(r'^\[[\w./-]+:[\w./-]*\]\s+.*?\n\n', '', text, count=1)


def _content_fingerprint(text: str) -> str:
    """Return a short hash of normalised text for near-duplicate detection."""
    normalised = re.sub(r'\s+', ' ', text.lower()).strip()
    return hashlib.sha256(normalised.encode('utf-8')).hexdigest()[:16]


def _matches_metadata(
    source_uri: str,
    product: str | None,
    product_family: str | None,
    source_type: str | None,
) -> bool:
    """Return True if the chunk's source URI satisfies all active filters.

    Uses URI path structure rather than chunk text because Kendra returns
    arbitrary document excerpts -- only the first chunk of each file contains
    the metadata header, so text-based filtering rejects most valid results.
    """
    if not any([product, product_family, source_type]):
        return True
    meta = _extract_uri_metadata(source_uri)
    if product and meta.get("product", "").lower() != product.lower():
        return False
    if product_family and meta.get("product_family", "").lower() != product_family.lower():
        return False
    if source_type and meta.get("source_type", "").lower() != source_type.lower():
        return False
    return True


def _format_dep_section(label: str, rows: list[dict]) -> str:
    if not rows:
        return f"{label}: No matches."
    lines = [f"{label}: {len(rows)} result(s)"]
    for r in rows:
        lines.append(f"  - {r['resource_id']}    [{r['type']}]    repo={r['repository']}")
    return "\n".join(lines)


# ── Neptune helpers ───────────────────────────────────────────────────────────

def _neptune_query(query: str, parameters: dict | None = None) -> dict:
    """Execute an openCypher query against Neptune.

    Routes through the API Gateway proxy when NEPTUNE_PROXY_URL is set,
    otherwise connects directly (requires VPC connectivity).
    """
    if NEPTUNE_PROXY_URL:
        return _neptune_query_via_proxy(query, parameters)
    return _neptune_query_direct(query, parameters)


def _neptune_query_via_proxy(query: str, parameters: dict | None = None) -> dict:
    """Execute an openCypher query via the API Gateway + Lambda proxy."""
    params = parameters or {}
    payload = json.dumps({"query": query, "parameters": params})
    headers = {"Content-Type": "application/json"}

    creds = boto3.Session().get_credentials().get_frozen_credentials()
    aws_req = AWSRequest(method="POST", url=NEPTUNE_PROXY_URL, data=payload, headers=headers)
    SigV4Auth(creds, "execute-api", AWS_REGION).add_auth(aws_req)
    headers = dict(aws_req.headers)

    try:
        resp = requests.post(NEPTUNE_PROXY_URL, data=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.ConnectionError:
        return {"error": f"Cannot connect to Neptune proxy at {NEPTUNE_PROXY_URL}."}
    except requests.exceptions.HTTPError as exc:
        return {"error": f"Proxy HTTP {exc.response.status_code}: {exc.response.text[:500]}"}
    except Exception as exc:
        return {"error": str(exc)}


def _neptune_query_direct(query: str, parameters: dict | None = None) -> dict:
    """Execute an openCypher query directly against Neptune (requires VPC access)."""
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
                "Neptune is VPC-only -- ensure the MCP server can reach the cluster "
                "(SSH tunnel, VPN, or run from within the VPC)."}
    except requests.exceptions.HTTPError as exc:
        return {"error": f"Neptune HTTP {exc.response.status_code}: {exc.response.text[:500]}"}
    except Exception as exc:
        return {"error": str(exc)}


# ── MCP server ────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "hashicorp-rag",
    instructions=(
        "Two backends are exposed:\n"
        "  * Kendra RAG index -- search HashiCorp documentation, providers, "
        "GitHub issues, Discuss threads, and blog posts. Use search_hashicorp_docs "
        "for any natural-language question about Terraform, Vault, Consul, Nomad, "
        "Packer, Sentinel, or Boundary; use get_index_info to inspect the index.\n"
        "  * Neptune Graph (openCypher) -- Terraform resource dependency graph for "
        "your own workspace repos. Use get_resource_dependencies to walk what a "
        "resource depends on (or what depends on it), find_resources_by_type to "
        "list every resource of a given Terraform type, and get_graph_info to "
        "inspect the graph store."
    ),
)


@mcp.tool()
def search_hashicorp_docs(
    query: str,
    top_k: int = 3,
    min_score: float = 0.0,
    product: str | None = None,
    product_family: str | None = None,
    source_type: str | None = None,
) -> str:
    """Search the HashiCorp documentation Kendra index.

    Retrieves semantically relevant documentation, GitHub issues, Discourse
    threads, and blog posts from the HashiCorp knowledge base indexed in
    Amazon Kendra.

    Args:
        query: Natural language question or topic to search for.
        top_k: Number of results to return. Range 1-20, default 3.
        min_score: Minimum confidence score 0-1 (VERY_HIGH=1.0, HIGH=0.75,
            MEDIUM=0.5, LOW=0.25). Default 0.0 returns all results.
        product: Filter by specific product name. Examples: "aws", "vault",
            "consul", "nomad", "packer", "terraform", "boundary".
        product_family: Filter by product family. One of: "terraform",
            "vault", "consul", "nomad", "packer", "boundary", "sentinel".
        source_type: Filter by document source type. One of: "provider",
            "documentation", "module", "sentinel", "issue", "discuss", "blog".

    Returns:
        Formatted string containing the matching document chunks with their
        confidence scores and source URIs, or an error message on failure.
    """
    if not KENDRA_INDEX_ID:
        return "Configuration error: AWS_KENDRA_INDEX_ID environment variable is not set."

    top_k = max(1, min(top_k, 20))
    min_score = max(0.0, min(min_score, 1.0))

    client = _kendra_client()

    # Over-fetch when metadata filters are active so we have enough candidates
    # after post-retrieval filtering to return the requested top_k results.
    has_filters = any([product, product_family, source_type])
    fetch_k = top_k * 3 if has_filters else top_k

    params: dict = {
        "IndexId":              KENDRA_INDEX_ID,
        "QueryText":            query,
        "PageSize":             fetch_k,
        "QueryResultTypeFilter": "DOCUMENT",
    }

    # Push Kendra-native filters down for efficiency (product_family, source_type)
    kendra_filters = []
    if product_family:
        kendra_filters.append({
            "EqualsTo": {"Key": "product_family", "Value": {"StringValue": product_family}}
        })
    if source_type:
        kendra_filters.append({
            "EqualsTo": {"Key": "source_type", "Value": {"StringValue": source_type}}
        })
    if len(kendra_filters) == 1:
        params["AttributeFilter"] = kendra_filters[0]
    elif len(kendra_filters) > 1:
        params["AttributeFilter"] = {"AndAllFilters": kendra_filters}

    try:
        resp = client.query(**params)
    except Exception as exc:
        logger.exception("Kendra query error for: %s", query)
        return f"Retrieval error: {exc}"

    contexts: list[dict] = []
    for item in resp.get("ResultItems", []):
        confidence = item.get("ScoreAttributes", {}).get("ScoreConfidence", "NOT_AVAILABLE")
        score = CONFIDENCE_SCORE.get(confidence, 0.0)
        if score < min_score:
            continue

        raw_text = item.get("DocumentExcerpt", {}).get("Text", "")
        doc_uri = item.get("DocumentURI", item.get("DocumentId", ""))

        # Kendra returns custom attributes directly
        attrs = {
            a["Key"]: a["Value"].get("StringValue", "")
            for a in item.get("DocumentAttributes", [])
            if "StringValue" in a.get("Value", {})
        }

        # Apply product filter via URI metadata or Kendra attributes
        if product:
            attr_product = attrs.get("product", "")
            uri_meta = _extract_uri_metadata(doc_uri)
            if (attr_product.lower() != product.lower()
                    and uri_meta.get("product", "").lower() != product.lower()):
                continue

        # Strip the metadata header -- the source URI already identifies the doc.
        text = _strip_chunk_header(raw_text)
        contexts.append({
            "source_uri": doc_uri,
            "score": score,
            "confidence": confidence,
            "text": text,
        })

    # Deduplicate by source URI -- keep only the highest-scoring chunk per document.
    seen_uris: dict[str, int] = {}
    deduped: list[dict] = []
    for ctx in contexts:
        uri = ctx["source_uri"]
        if uri in seen_uris:
            existing_idx = seen_uris[uri]
            if ctx["score"] > deduped[existing_idx]["score"]:
                deduped[existing_idx] = ctx
        else:
            seen_uris[uri] = len(deduped)
            deduped.append(ctx)

    # Cross-document dedup: drop chunks with near-identical content from
    # different source URIs (e.g. the same example in a guide and a provider doc).
    seen_fingerprints: set[str] = set()
    unique: list[dict] = []
    for ctx in deduped:
        fp = _content_fingerprint(ctx["text"])
        if fp not in seen_fingerprints:
            seen_fingerprints.add(fp)
            unique.append(ctx)
    contexts = unique[:top_k]

    if not contexts:
        active_filters: list[str] = []
        if product:
            active_filters.append(f"product={product}")
        if product_family:
            active_filters.append(f"product_family={product_family}")
        if source_type:
            active_filters.append(f"source_type={source_type}")
        filter_note = f" with filters ({', '.join(active_filters)})" if active_filters else ""
        return f'No results found for: "{query}"{filter_note}'

    lines: list[str] = [f'Found {len(contexts)} result(s) for: "{query}"\n']
    for i, ctx in enumerate(contexts, 1):
        short_source = _short_source_uri(ctx["source_uri"])
        lines.append(f"[{i}] {short_source} ({ctx['confidence']}, {ctx['score']:.2f})")
        lines.append(ctx["text"])
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
def get_index_info() -> str:
    """Return configuration details of the active Kendra RAG index.

    Returns:
        Formatted string with region, account, index status, and a reference
        to the available metadata filters.
    """
    if not KENDRA_INDEX_ID:
        return "Error: AWS_KENDRA_INDEX_ID environment variable is not set."

    lines = [
        "Kendra RAG Index",
        "=" * 40,
        f"Region:        {AWS_REGION}",
        f"Index ID:      {KENDRA_INDEX_ID}",
    ]

    try:
        sts = boto3.client("sts", region_name=AWS_REGION)
        identity = sts.get_caller_identity()
        lines.append(f"Account:       {identity.get('Account', 'unknown')}")
        lines.append(f"Caller ARN:    {identity.get('Arn', 'unknown')}")
    except Exception as exc:
        lines.append(f"Auth error:    {exc}")

    try:
        kendra = boto3.client("kendra", region_name=AWS_REGION)
        resp = kendra.describe_index(Id=KENDRA_INDEX_ID)
        lines.append(f"Index name:    {resp.get('Name', '')}")
        lines.append(f"Index status:  {resp.get('Status', '')}")
        lines.append(f"Edition:       {resp.get('Edition', '')}")
    except Exception as exc:
        lines.append(f"Index error:   {exc}")

    lines.extend([
        "",
        "Metadata filters available in search_hashicorp_docs:",
        "  product:        aws | vault | consul | nomad | packer | terraform | boundary",
        "  product_family: terraform | vault | consul | nomad | packer | boundary | sentinel",
        "  source_type:    provider | documentation | module | sentinel | issue | discuss | blog",
        "",
        "Default retrieval settings:",
        "  top_k:     3  (range 1-20)",
        "  min_score: 0.0  (range 0.0-1.0; higher = stricter)",
    ])
    return "\n".join(lines)


# ── Neptune Graph tools ───────────────────────────────────────────────────────


@mcp.tool()
def get_resource_dependencies(
    resource_type: str,
    resource_name: str,
    direction: str = "both",
    max_depth: int = 2,
    repository: str | None = None,
) -> str:
    """Traverse the Terraform resource dependency graph in Neptune.

    Finds resources that a given resource depends on (downstream), resources
    that depend on it (upstream), or both. The graph is populated by the
    ``terraform graph`` ingestion pipeline (``task graph:populate``).

    Args:
        resource_type: Terraform resource type (e.g. "aws_lambda_function",
            "aws_s3_bucket").
        resource_name: Terraform resource name (e.g. "processor", "content").
        direction: "downstream" (what this resource depends on),
            "upstream" (what depends on it), or "both" (default).
        max_depth: Maximum traversal depth. Range 1-5, default 2.
        repository: Optional -- restrict traversal to a single repository
            (GitHub HTTPS URL or repo name).

    Returns:
        Formatted string listing the matching dependent resources, or an
        error / empty-result message.
    """
    if not NEPTUNE_ENDPOINT and not NEPTUNE_PROXY_URL:
        return "Configuration error: NEPTUNE_ENDPOINT or NEPTUNE_PROXY_URL environment variable is required."

    max_depth = min(max(1, int(max_depth)), 5)
    direction = direction.lower()
    if direction not in ("downstream", "upstream", "both"):
        return "Error: direction must be one of: downstream, upstream, both"

    address = f"{resource_type}.{resource_name}"
    sections: list[str] = []

    def _walk(walk_direction: str) -> list[dict]:
        if walk_direction == "downstream":
            cypher = (
                f"MATCH (a:Resource)-[:DEPENDS_ON*1..{max_depth}]->(b:Resource) "
                "WHERE a.id = $address "
            )
        else:
            cypher = (
                f"MATCH (b:Resource)-[:DEPENDS_ON*1..{max_depth}]->(a:Resource) "
                "WHERE a.id = $address "
            )

        if repository:
            cypher += "AND b.repo CONTAINS $repo "
        cypher += "RETURN DISTINCT b.id AS resource_id, b.type AS type, b.name AS name, b.repo AS repository"

        params: dict = {"address": address}
        if repository:
            params["repo"] = repository

        resp = _neptune_query(cypher, params)
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return resp.get("results", [])

    try:
        if direction in ("downstream", "both"):
            down = _walk("downstream")
            sections.append(_format_dep_section("Downstream (depends on)", down))
        if direction in ("upstream", "both"):
            up = _walk("upstream")
            sections.append(_format_dep_section("Upstream (depended on by)", up))
    except RuntimeError as exc:
        return str(exc)

    body = "\n\n".join(sections)
    if not body.strip() or ("No matches" in body and direction != "both"):
        return (
            f"No dependencies found for '{address}' "
            f"(direction={direction}, max_depth={max_depth}). "
            "Check that the graph has been populated (`task graph:populate`) "
            "and the address is correct."
        )
    return f"Dependency walk for '{address}' (max_depth={max_depth})\n\n{body}"


@mcp.tool()
def find_resources_by_type(
    resource_type: str,
    repository: str | None = None,
    limit: int = 50,
) -> str:
    """List Terraform resources of a given type from the Neptune graph.

    Args:
        resource_type: Terraform resource type (e.g. "aws_s3_bucket",
            "aws_iam_role").
        repository: Optional -- restrict to a specific repository
            (GitHub HTTPS URL or repo name).
        limit: Maximum rows to return. Range 1-500, default 50.

    Returns:
        Formatted string listing matching resources, or an error / empty-result
        message.
    """
    if not NEPTUNE_ENDPOINT and not NEPTUNE_PROXY_URL:
        return "Configuration error: NEPTUNE_ENDPOINT or NEPTUNE_PROXY_URL environment variable is required."

    limit = min(max(1, int(limit)), 500)

    if repository:
        cypher = (
            "MATCH (repo:Repository)-[:CONTAINS]->(r:Resource) "
            "WHERE r.type = $type AND (repo.uri = $repo OR repo.name = $repo) "
            f"RETURN r.id AS resource_id, r.type AS type, r.name AS name, repo.uri AS repository "
            f"LIMIT {limit}"
        )
        resp = _neptune_query(cypher, {"type": resource_type, "repo": repository})
    else:
        cypher = (
            "MATCH (r:Resource) WHERE r.type = $type "
            f"RETURN r.id AS resource_id, r.type AS type, r.name AS name, r.repo AS repository "
            f"LIMIT {limit}"
        )
        resp = _neptune_query(cypher, {"type": resource_type})

    if "error" in resp:
        return resp["error"]

    results = resp.get("results", [])
    if not results:
        scope = f" in repo {repository}" if repository else ""
        return (
            f"No resources of type '{resource_type}' found{scope}. "
            "Check that the graph has been populated (`task graph:populate`)."
        )

    lines = [f"Found {len(results)} resource(s) of type '{resource_type}'"]
    if repository:
        lines[0] += f" in {repository}"
    for r in results:
        lines.append(f"  - {r['resource_id']}    name={r['name']}    repo={r['repository']}")
    return "\n".join(lines)


@mcp.tool()
def get_graph_info() -> str:
    """Return configuration and basic counts for the Neptune graph store.

    Returns:
        Formatted string with region, endpoint, and the number of
        resources / dependencies / repos currently loaded -- or an error if the
        graph store is not configured.
    """
    if not NEPTUNE_ENDPOINT and not NEPTUNE_PROXY_URL:
        return "Error: NEPTUNE_ENDPOINT or NEPTUNE_PROXY_URL environment variable is not set."

    target = NEPTUNE_PROXY_URL or f"{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}"

    # Count nodes by label
    node_resp = _neptune_query(
        "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS cnt ORDER BY cnt DESC"
    )
    if "error" in node_resp:
        return f"Neptune query failed: {node_resp['error']}"

    # Count edges
    edge_resp = _neptune_query("MATCH ()-[e:DEPENDS_ON]->() RETURN count(e) AS cnt")
    edges = 0
    if "error" not in edge_resp:
        edge_results = edge_resp.get("results", [])
        if edge_results:
            edges = edge_results[0].get("cnt", 0)

    # Count repos
    repo_resp = _neptune_query(
        "MATCH (r:Repository) RETURN count(r) AS cnt"
    )
    repos = 0
    if "error" not in repo_resp:
        repo_results = repo_resp.get("results", [])
        if repo_results:
            repos = repo_results[0].get("cnt", 0)

    node_counts = node_resp.get("results", [])
    total_nodes = sum(r.get("cnt", 0) for r in node_counts)

    lines = [
        "Neptune Graph (openCypher)",
        "=" * 40,
        f"Region:     {AWS_REGION}",
        f"Endpoint:   {target}",
        f"IAM auth:   {NEPTUNE_IAM_AUTH}",
        f"Nodes:      {total_nodes} total",
    ]
    for r in node_counts:
        lines.append(f"  {r.get('label', '?')}: {r.get('cnt', 0)}")
    lines.extend([
        f"DependsOn:  {edges} edge(s)",
        f"Repos:      {repos}",
        "",
        "Tools:",
        "  get_resource_dependencies(resource_type, resource_name, direction, max_depth, repository)",
        "  find_resources_by_type(resource_type, repository, limit)",
    ])
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
