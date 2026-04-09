#!/usr/bin/env python3
"""test_token_efficiency.py — Compare token cost across retrieval backends.

Runs queries against Kendra (RAG), Neptune (graph), or both, and estimates
the token savings compared to pasting full documentation or manually inspecting
Terraform state files.

Modes:
  kendra   — Kendra-only (RAG retrieval vs raw documentation)
  graph    — Neptune-only (graph traversal vs raw Terraform state)
  combined — Queries that use both Kendra + Neptune vs raw sources
  all      — Runs all three modes sequentially

Usage:
    python3 scripts/test_token_efficiency.py \\
        --region us-east-1 \\
        --kendra-index-id ABCDEFGHIJ \\
        --neptune-endpoint <endpoint> \\
        --mode all
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.parse

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError, EndpointResolutionError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logging.getLogger("botocore.credentials").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

# ── Test query definitions ────────────────────────────────────────────────────

# Estimated raw documentation token counts (full pages, conservative)
KENDRA_TEST_QUERIES: list[dict] = [
    {"query": "How do I configure an S3 backend in Terraform?",                        "raw_tokens": 9500},
    {"query": "How do I set up the AWS provider in Terraform?",                        "raw_tokens": 11000},
    {"query": "How do I generate dynamic secrets with HashiCorp Vault?",               "raw_tokens": 14000},
    {"query": "How do I configure Consul service mesh with mTLS?",                     "raw_tokens": 16000},
    {"query": "How do I build a Packer AMI with an HCL template?",                    "raw_tokens": 8500},
    {"query": "How do I use Vault dynamic secrets with the Terraform AWS provider?",   "raw_tokens": 22000},
    {"query": "How do I schedule a Docker workload in Nomad?",                         "raw_tokens": 12000},
    {"query": "How do I enforce Sentinel policies in Terraform Cloud?",                "raw_tokens": 13500},
    {"query": "How do I compose reusable Terraform modules?",                          "raw_tokens": 10000},
    {"query": "How do I integrate Consul service discovery with Vault?",               "raw_tokens": 19500},
]

# Graph queries — raw_tokens estimates the cost of inspecting raw terraform
# state/plan JSON files to extract the same information manually.
GRAPH_TEST_QUERIES: list[dict] = [
    {
        "query": "What does aws_lambda_function depend on?",
        "cypher": "MATCH (a:Resource)-[:DEPENDS_ON]->(b:Resource) WHERE a.type = 'aws_lambda_function' RETURN b.id AS dep, b.type AS type",
        "raw_tokens": 5000,
    },
    {
        "query": "List all aws_iam_role resources",
        "cypher": "MATCH (r:Resource {type: 'aws_iam_role'}) RETURN r.id AS resource, r.name AS name",
        "raw_tokens": 8000,
    },
    {
        "query": "What resources are in a repository?",
        "cypher": "MATCH (repo:Repository)-[:CONTAINS]->(r:Resource) RETURN repo.name AS repo, r.id AS resource, r.type AS type LIMIT 50",
        "raw_tokens": 12000,
    },
    {
        "query": "What depends on aws_iam_role (reverse deps)?",
        "cypher": "MATCH (b:Resource)-[:DEPENDS_ON]->(a:Resource) WHERE a.type = 'aws_iam_role' RETURN b.id AS resource, b.type AS type",
        "raw_tokens": 6000,
    },
    {
        "query": "Two-hop dependency chain from aws_s3_bucket",
        "cypher": "MATCH (a:Resource)-[:DEPENDS_ON*1..2]->(b:Resource) WHERE a.type = 'aws_s3_bucket' RETURN DISTINCT b.id AS dep, b.type AS type",
        "raw_tokens": 15000,
    },
    {
        "query": "List all aws_security_group resources",
        "cypher": "MATCH (r:Resource {type: 'aws_security_group'}) RETURN r.id AS resource, r.name AS name",
        "raw_tokens": 7000,
    },
    {
        "query": "Count resources by type across all repos",
        "cypher": "MATCH (r:Resource) RETURN r.type AS type, count(r) AS cnt ORDER BY cnt DESC LIMIT 20",
        "raw_tokens": 10000,
    },
    {
        "query": "Full dependency chain for aws_neptune_cluster",
        "cypher": "MATCH (a:Resource)-[:DEPENDS_ON*1..3]->(b:Resource) WHERE a.type = 'aws_neptune_cluster' RETURN DISTINCT b.id AS dep, b.type AS type",
        "raw_tokens": 9000,
    },
]

# Combined queries — use both Kendra context and graph traversal
COMBINED_TEST_QUERIES: list[dict] = [
    {
        "query": "What IAM permissions does aws_lambda_function need?",
        "kendra_query": "AWS Lambda IAM execution role permissions Terraform",
        "cypher": "MATCH (a:Resource)-[:DEPENDS_ON]->(b:Resource) WHERE a.type = 'aws_lambda_function' AND b.type = 'aws_iam_role' RETURN b.id AS role",
        "raw_tokens": 18000,
    },
    {
        "query": "How should aws_s3_bucket encryption be configured and what depends on it?",
        "kendra_query": "Terraform aws_s3_bucket server-side encryption configuration",
        "cypher": "MATCH (b:Resource)-[:DEPENDS_ON]->(a:Resource) WHERE a.type = 'aws_s3_bucket' RETURN b.id AS dependent, b.type AS type",
        "raw_tokens": 16000,
    },
    {
        "query": "Neptune cluster security group rules and best practices",
        "kendra_query": "Neptune cluster VPC security group configuration Terraform",
        "cypher": "MATCH (a:Resource)-[:DEPENDS_ON]->(b:Resource) WHERE a.type = 'aws_neptune_cluster' AND b.type = 'aws_security_group' RETURN b.id AS sg",
        "raw_tokens": 14000,
    },
    {
        "query": "Kendra index IAM role setup and what resources reference it",
        "kendra_query": "Amazon Kendra IAM service role permissions Terraform",
        "cypher": "MATCH (b:Resource)-[:DEPENDS_ON]->(a:Resource) WHERE a.type = 'aws_iam_role' RETURN b.id AS resource, b.type AS type LIMIT 20",
        "raw_tokens": 20000,
    },
    {
        "query": "VPC subnet configuration and resources deployed into it",
        "kendra_query": "Terraform VPC subnet configuration best practices",
        "cypher": "MATCH (a:Resource)-[:DEPENDS_ON]->(b:Resource) WHERE b.type = 'aws_subnet' RETURN a.id AS resource, a.type AS type",
        "raw_tokens": 15000,
    },
]


# ── Token counting ────────────────────────────────────────────────────────────

def _count_tokens(text: str) -> int:
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except ImportError:
        return int(len(text.split()) * 1.3)


# ── Kendra helpers ────────────────────────────────────────────────────────────

KENDRA_SUPPORTED_REGIONS = [
    "us-east-1", "us-east-2", "us-west-2",
    "eu-west-1", "eu-west-2",
    "ap-southeast-1", "ap-southeast-2", "ap-northeast-1", "ap-northeast-2",
    "ca-central-1",
]


def validate_kendra_index(client: object, index_id: str, region: str) -> None:
    """Verify the index exists and is ACTIVE; raise with actionable message if not."""
    try:
        resp = client.describe_index(Id=index_id)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "ResourceNotFoundException":
            hint = _find_index_region(index_id, exclude=region)
            msg = f"Index '{index_id}' not found in region '{region}'."
            if hint:
                msg += f" Found it in '{hint}' — rerun with --region {hint}"
            else:
                msg += " Check the index ID and region."
            log.error(msg)
            sys.exit(1)
        if code in ("AccessDeniedException", "UnauthorizedException"):
            log.error("Permission denied accessing index '%s' in '%s'. Check IAM.", index_id, region)
            sys.exit(1)
        raise
    except EndpointResolutionError:
        supported = ", ".join(KENDRA_SUPPORTED_REGIONS)
        log.error("Kendra endpoint not available in region '%s'. Supported: %s", region, supported)
        sys.exit(1)

    status = resp.get("Status", "UNKNOWN")
    if status != "ACTIVE":
        log.error("Index '%s' is not ACTIVE (status=%s). Wait until it is ready.", index_id, status)
        sys.exit(1)
    log.info("Kendra index '%s' is ACTIVE in '%s'.", index_id, region)


def _find_index_region(index_id: str, exclude: str) -> str | None:
    for region in KENDRA_SUPPORTED_REGIONS:
        if region == exclude:
            continue
        try:
            c = boto3.client("kendra", region_name=region)
            c.describe_index(Id=index_id)
            return region
        except Exception:
            pass
    return None


def kendra_retrieve(client: object, index_id: str, query: str, top_k: int) -> str:
    """Query Kendra and return concatenated excerpt text."""
    resp = client.query(IndexId=index_id, QueryText=query, PageSize=top_k)
    chunks = [
        item.get("DocumentExcerpt", {}).get("Text", "")
        for item in resp.get("ResultItems", [])
        if item.get("DocumentExcerpt", {}).get("Text")
    ]
    if not chunks:
        log.warning("No Kendra results for: %s", query)
    return "\n\n---\n\n".join(chunks)


# ── Neptune helpers ───────────────────────────────────────────────────────────

def neptune_query(endpoint: str, port: int, region: str, query: str, *, proxy_url: str = "") -> str:
    """Execute an openCypher query and return the result as text.

    When *proxy_url* is set, routes through the API Gateway + Lambda proxy
    instead of connecting directly to Neptune.
    """
    if proxy_url:
        return _neptune_query_via_proxy(proxy_url, region, query)

    url = f"https://{endpoint}:{port}/openCypher"
    body = urllib.parse.urlencode({
        "query": query,
        "parameters": json.dumps({}),
    })
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    creds = boto3.Session().get_credentials().get_frozen_credentials()
    aws_req = AWSRequest(method="POST", url=url, data=body, headers=headers)
    SigV4Auth(creds, "neptune-db", region).add_auth(aws_req)
    headers = dict(aws_req.headers)

    resp = requests.post(url, data=body, headers=headers, timeout=30)
    resp.raise_for_status()
    return json.dumps(resp.json().get("results", []), indent=2)


def _neptune_query_via_proxy(proxy_url: str, region: str, query: str) -> str:
    """Execute an openCypher query via the API Gateway + Lambda proxy."""
    payload = json.dumps({"query": query, "parameters": {}})
    headers = {"Content-Type": "application/json"}

    creds = boto3.Session().get_credentials().get_frozen_credentials()
    aws_req = AWSRequest(method="POST", url=proxy_url, data=payload, headers=headers)
    SigV4Auth(creds, "execute-api", region).add_auth(aws_req)
    headers = dict(aws_req.headers)

    resp = requests.post(proxy_url, data=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    return json.dumps(resp.json().get("results", []), indent=2)


def validate_neptune(endpoint: str, port: int, region: str, *, proxy_url: str = "", required: bool = True) -> bool:
    """Verify Neptune connectivity with a simple count query.

    Returns True if reachable.  When *required* is False (e.g. mode=all),
    logs a warning and returns False instead of aborting.
    """
    try:
        neptune_query(endpoint, port, region, "MATCH (n) RETURN count(n) AS total LIMIT 1", proxy_url=proxy_url)
        target = proxy_url or f"{endpoint}:{port}"
        log.info("Neptune at '%s' is reachable.", target)
        return True
    except requests.exceptions.ConnectionError:
        target = proxy_url or f"{endpoint}:{port}"
        msg = "Cannot connect to Neptune at %s."
        if not proxy_url:
            msg += (" Neptune is VPC-only — ensure connectivity "
                    "(SSH tunnel, VPN, proxy, or run from within VPC).")
        if required:
            log.error(msg, target)
            sys.exit(1)
        log.warning(msg + " Skipping Neptune tests.", target)
        return False
    except Exception as exc:
        if required:
            log.error("Neptune validation failed: %s", exc)
            sys.exit(1)
        log.warning("Neptune validation failed: %s — skipping Neptune tests.", exc)
        return False


# ── Test runners ──────────────────────────────────────────────────────────────

def _print_header(title: str) -> None:
    print(f"\n{'=' * 85}")
    print(f"  {title}")
    print(f"{'=' * 85}")
    print(f"\n{'Query':<60} {'Result':>6} {'Raw':>8} {'Saving':>8}")
    print("-" * 85)


def _print_summary(label: str, total_result: int, total_raw: int, n: int) -> None:
    print("-" * 85)
    if total_raw == 0:
        log.warning("No results for %s tests.", label)
        return
    saving = int((1 - total_result / total_raw) * 100)
    print(f"{'Total':<60} {total_result:>6} {total_raw:>8} {saving:>7}%")
    print(f"\nAverage result tokens/query: {total_result // max(n, 1)}")
    print(f"Average raw tokens/query:    {total_raw // max(n, 1)}")
    print(f"Overall token saving:        {saving}%")


def run_kendra_tests(client: object, index_id: str, top_k: int) -> tuple[int, int]:
    _print_header("Kendra RAG — Token Efficiency")
    total_rag, total_raw = 0, 0
    for test in KENDRA_TEST_QUERIES:
        query, raw_tokens = test["query"], test["raw_tokens"]
        try:
            context = kendra_retrieve(client, index_id, query, top_k)
            rag_tokens = _count_tokens(context)
        except Exception as exc:
            log.error("Kendra retrieval failed for '%s': %s", query[:40], exc)
            continue
        saving_pct = int((1 - rag_tokens / raw_tokens) * 100) if raw_tokens > 0 else 0
        short = query[:58] + ".." if len(query) > 60 else query
        print(f"{short:<60} {rag_tokens:>6} {raw_tokens:>8} {saving_pct:>7}%")
        total_rag += rag_tokens
        total_raw += raw_tokens
    _print_summary("Kendra", total_rag, total_raw, len(KENDRA_TEST_QUERIES))
    return total_rag, total_raw


def run_graph_tests(endpoint: str, port: int, region: str, *, proxy_url: str = "") -> tuple[int, int]:
    _print_header("Neptune Graph — Token Efficiency")
    total_graph, total_raw = 0, 0
    for test in GRAPH_TEST_QUERIES:
        query, cypher, raw_tokens = test["query"], test["cypher"], test["raw_tokens"]
        try:
            result_text = neptune_query(endpoint, port, region, cypher, proxy_url=proxy_url)
            graph_tokens = _count_tokens(result_text)
        except Exception as exc:
            log.error("Neptune query failed for '%s': %s", query[:40], exc)
            continue
        saving_pct = int((1 - graph_tokens / raw_tokens) * 100) if raw_tokens > 0 else 0
        short = query[:58] + ".." if len(query) > 60 else query
        print(f"{short:<60} {graph_tokens:>6} {raw_tokens:>8} {saving_pct:>7}%")
        total_graph += graph_tokens
        total_raw += raw_tokens
    _print_summary("Graph", total_graph, total_raw, len(GRAPH_TEST_QUERIES))
    return total_graph, total_raw


def run_combined_tests(
    client: object, index_id: str, top_k: int,
    endpoint: str, port: int, region: str,
    *, proxy_url: str = "",
) -> tuple[int, int]:
    _print_header("Combined (Kendra + Neptune) — Token Efficiency")
    total_combined, total_raw = 0, 0
    for test in COMBINED_TEST_QUERIES:
        query = test["query"]
        raw_tokens = test["raw_tokens"]
        try:
            kendra_text = kendra_retrieve(client, index_id, test["kendra_query"], top_k)
            graph_text = neptune_query(endpoint, port, region, test["cypher"], proxy_url=proxy_url)
            combined_text = kendra_text + "\n\n--- Graph Context ---\n\n" + graph_text
            combined_tokens = _count_tokens(combined_text)
        except Exception as exc:
            log.error("Combined query failed for '%s': %s", query[:40], exc)
            continue
        saving_pct = int((1 - combined_tokens / raw_tokens) * 100) if raw_tokens > 0 else 0
        short = query[:58] + ".." if len(query) > 60 else query
        print(f"{short:<60} {combined_tokens:>6} {raw_tokens:>8} {saving_pct:>7}%")
        total_combined += combined_tokens
        total_raw += raw_tokens
    _print_summary("Combined", total_combined, total_raw, len(COMBINED_TEST_QUERIES))
    return total_combined, total_raw


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", required=True)
    parser.add_argument("--kendra-index-id", default="")
    parser.add_argument("--neptune-endpoint", default="")
    parser.add_argument("--neptune-port", type=int, default=8182)
    parser.add_argument("--neptune-proxy-url", default="",
                        help="API Gateway URL for Neptune proxy (overrides direct access)")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--mode", choices=["kendra", "graph", "combined", "all"], default="all")
    args = parser.parse_args()

    needs_kendra = args.mode in ("kendra", "combined", "all")
    needs_neptune = args.mode in ("graph", "combined", "all")

    if needs_kendra and not args.kendra_index_id:
        log.error("--kendra-index-id is required for mode '%s'.", args.mode)
        sys.exit(1)
    if needs_neptune and not args.neptune_endpoint and not args.neptune_proxy_url:
        log.error("--neptune-endpoint or --neptune-proxy-url is required for mode '%s'.", args.mode)
        sys.exit(1)

    try:
        import tiktoken  # noqa: F401
        token_method = "tiktoken (cl100k_base)"
    except ImportError:
        token_method = "word-count approximation (install tiktoken for exact counts)"
    log.info("Token counting: %s", token_method)
    log.info("Mode: %s", args.mode)

    kendra_client = None
    if needs_kendra:
        if args.region not in KENDRA_SUPPORTED_REGIONS:
            log.error("Region '%s' does not support Kendra. Supported: %s",
                      args.region, ", ".join(KENDRA_SUPPORTED_REGIONS))
            sys.exit(1)
        kendra_client = boto3.client("kendra", region_name=args.region)
        validate_kendra_index(kendra_client, args.kendra_index_id, args.region)

    neptune_ok = False
    if needs_neptune:
        # In "all" mode, Neptune is optional — skip gracefully if unreachable.
        neptune_ok = validate_neptune(
            args.neptune_endpoint, args.neptune_port, args.region,
            proxy_url=args.neptune_proxy_url,
            required=(args.mode != "all"),
        )

    grand_result, grand_raw = 0, 0

    if args.mode in ("kendra", "all"):
        r, raw = run_kendra_tests(kendra_client, args.kendra_index_id, args.top_k)
        grand_result += r
        grand_raw += raw

    if args.mode in ("graph", "all") and (args.mode == "graph" or neptune_ok):
        r, raw = run_graph_tests(args.neptune_endpoint, args.neptune_port, args.region,
                                 proxy_url=args.neptune_proxy_url)
        grand_result += r
        grand_raw += raw

    if args.mode in ("combined", "all") and (args.mode == "combined" or neptune_ok):
        r, raw = run_combined_tests(
            kendra_client, args.kendra_index_id, args.top_k,
            args.neptune_endpoint, args.neptune_port, args.region,
            proxy_url=args.neptune_proxy_url,
        )
        grand_result += r
        grand_raw += raw

    if args.mode == "all" and grand_raw > 0:
        print(f"\n{'=' * 85}")
        print("  Grand Total — All Modes")
        print(f"{'=' * 85}")
        overall_saving = int((1 - grand_result / grand_raw) * 100)
        print(f"\nTotal retrieval tokens: {grand_result:,}")
        print(f"Total raw tokens:      {grand_raw:,}")
        print(f"Overall token saving:  {overall_saving}%")

    if grand_raw == 0:
        log.error("No queries returned results. Check that the indexes have been populated.")
        sys.exit(1)


if __name__ == "__main__":
    main()
