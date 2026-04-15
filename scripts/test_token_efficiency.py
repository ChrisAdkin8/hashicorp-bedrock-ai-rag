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

# Estimated raw documentation token counts represent how many tokens a human
# would have to feed the model from the full documentation pages just
# to answer the question (titles, navigation, boilerplate excluded — just content).
KENDRA_TEST_QUERIES: list[dict] = [
    {
        "topic": "S3 backend configuration",
        "query": "How do I configure an S3 backend in Terraform?",
        "raw_sources": "Terraform S3 backend docs + state locking page + workspaces page",
        "raw_tokens_estimate": 9500,
    },
    {
        "topic": "AWS provider setup",
        "query": "How do I configure the AWS provider in Terraform?",
        "raw_sources": "AWS provider docs main page + authentication page + region config",
        "raw_tokens_estimate": 11000,
    },
    {
        "topic": "Vault dynamic secrets",
        "query": "How do I generate dynamic database credentials using HashiCorp Vault?",
        "raw_sources": "Vault database secrets engine docs + PostgreSQL plugin + lease management",
        "raw_tokens_estimate": 14000,
    },
    {
        "topic": "Consul service mesh",
        "query": "How do I set up mTLS between services using Consul Connect?",
        "raw_sources": "Consul Connect overview + intentions + proxy config + TLS docs",
        "raw_tokens_estimate": 16000,
    },
    {
        "topic": "Packer AMI builds",
        "query": "How do I build an AMI with Packer using an HCL2 template?",
        "raw_sources": "Packer HCL2 docs + builders reference + AMI configuration",
        "raw_tokens_estimate": 8500,
    },
    {
        "topic": "Cross-product: Vault + AWS provider",
        "query": "How do I use Vault dynamic secrets with the Terraform AWS provider?",
        "raw_sources": "Vault AWS secrets engine + Terraform Vault provider + AWS provider auth docs",
        "raw_tokens_estimate": 22000,
    },
    {
        "topic": "Nomad job scheduling",
        "query": "How do I write a Nomad job specification to run a Docker container?",
        "raw_sources": "Nomad job spec docs + task drivers reference + Docker driver page",
        "raw_tokens_estimate": 12000,
    },
    {
        "topic": "Sentinel policy enforcement",
        "query": "How do I write a Sentinel policy to enforce Terraform resource tagging?",
        "raw_sources": "Sentinel language docs + Terraform Cloud policy sets + tfplan import reference",
        "raw_tokens_estimate": 13500,
    },
    {
        "topic": "Terraform module composition",
        "query": "How do I call a Terraform module and pass outputs between modules?",
        "raw_sources": "Terraform modules docs + module sources + output values + variable passing",
        "raw_tokens_estimate": 10000,
    },
    {
        "topic": "Cross-product: Consul + Vault",
        "query": "How do I use Vault to manage TLS certificates for Consul service mesh?",
        "raw_sources": "Consul TLS docs + Vault PKI secrets engine + Consul agent TLS config",
        "raw_tokens_estimate": 19500,
    },
]

# Graph queries test structured dependency lookups vs reading raw .tf files
# or running terraform graph manually and parsing DOT output.
GRAPH_TEST_QUERIES: list[dict] = [
    {
        "topic": "S3 bucket dependencies",
        "query": "What does aws_s3_bucket depend on?",
        "cypher": "MATCH (a:Resource)-[:DEPENDS_ON]->(b:Resource) WHERE a.type = 'aws_s3_bucket' RETURN b.id AS dep, b.type AS type",
        "raw_sources": "terraform plan output + manual grep of .tf files",
        "raw_tokens_estimate": 4500,
    },
    {
        "topic": "IAM role resources",
        "query": "List all aws_iam_role resources",
        "cypher": "MATCH (r:Resource {type: 'aws_iam_role'}) RETURN r.id AS resource, r.name AS name",
        "raw_sources": "grep all .tf files for aws_iam_role blocks",
        "raw_tokens_estimate": 6000,
    },
    {
        "topic": "Lambda function resources",
        "query": "List all aws_lambda_function resources",
        "cypher": "MATCH (r:Resource {type: 'aws_lambda_function'}) RETURN r.id AS resource, r.name AS name",
        "raw_sources": "grep .tf files + terraform state list filtering",
        "raw_tokens_estimate": 3500,
    },
    {
        "topic": "CodeBuild project chain",
        "query": "What does aws_codebuild_project depend on?",
        "cypher": "MATCH (a:Resource)-[:DEPENDS_ON]->(b:Resource) WHERE a.type = 'aws_codebuild_project' RETURN b.id AS dep, b.type AS type",
        "raw_sources": "terraform graph DOT output + manual parsing",
        "raw_tokens_estimate": 5000,
    },
    {
        "topic": "Neptune cluster resources",
        "query": "List all aws_neptune_cluster resources and dependencies",
        "cypher": "MATCH (a:Resource)-[:DEPENDS_ON*1..2]->(b:Resource) WHERE a.type = 'aws_neptune_cluster' RETURN DISTINCT b.id AS dep, b.type AS type",
        "raw_sources": "grep .tf files for neptune blocks + state inspection",
        "raw_tokens_estimate": 4000,
    },
    {
        "topic": "Step Functions resources",
        "query": "List all aws_sfn_state_machine resources",
        "cypher": "MATCH (r:Resource {type: 'aws_sfn_state_machine'}) RETURN r.id AS resource, r.name AS name",
        "raw_sources": "grep .tf files + terraform state list",
        "raw_tokens_estimate": 3000,
    },
    {
        "topic": "EventBridge scheduler resources",
        "query": "List all aws_scheduler_schedule resources",
        "cypher": "MATCH (r:Resource {type: 'aws_scheduler_schedule'}) RETURN r.id AS resource, r.name AS name",
        "raw_sources": "grep .tf files + terraform state list",
        "raw_tokens_estimate": 3500,
    },
    {
        "topic": "Kendra index resources",
        "query": "List all aws_kendra_index resources",
        "cypher": "MATCH (r:Resource {type: 'aws_kendra_index'}) RETURN r.id AS resource, r.name AS name",
        "raw_sources": "grep .tf files for kendra blocks + terraform state list",
        "raw_tokens_estimate": 3000,
    },
    {
        "topic": "Security group resources",
        "query": "List all aws_security_group resources",
        "cypher": "MATCH (r:Resource {type: 'aws_security_group'}) RETURN r.id AS resource, r.name AS name",
        "raw_sources": "grep .tf files for security_group blocks + terraform state list",
        "raw_tokens_estimate": 3500,
    },
    {
        "topic": "Graph statistics overview",
        "query": "Count resources by type across all repos",
        "cypher": "MATCH (r:Resource) RETURN r.type AS type, count(r) AS cnt ORDER BY cnt DESC LIMIT 20",
        "raw_sources": "terraform state list | wc -l + terraform graph | dot analysis",
        "raw_tokens_estimate": 2000,
    },
]

# Combined queries require answers from BOTH the Kendra index (documentation)
# AND the Neptune graph store (infrastructure structure/dependencies).
# Each entry has a natural-language query for Kendra plus a graph lookup that
# contributes structural context the docs alone cannot provide.
COMBINED_TEST_QUERIES: list[dict] = [
    {
        "topic": "IAM roles vs least-privilege guidance",
        "rag_query": (
            "What are the best practices for granting IAM roles to Lambda "
            "functions and services in AWS Terraform projects?"
        ),
        "cypher": (
            "MATCH (b:Resource)-[:DEPENDS_ON]->(a:Resource) "
            "WHERE a.type = 'aws_iam_role' "
            "RETURN b.id AS resource, b.type AS type"
        ),
        "why_combined": (
            "Kendra provides HashiCorp best-practice guidance; graph shows which "
            "IAM bindings actually exist so the answer can flag over-permissioned roles"
        ),
        "raw_sources": (
            "Terraform AWS IAM docs + Vault identity docs + grep all .tf for IAM blocks"
        ),
        "raw_tokens_estimate": 18000,
    },
    {
        "topic": "Lambda function security posture",
        "rag_query": (
            "How should Lambda execution roles be secured and scoped according to "
            "HashiCorp Vault and Terraform best practices?"
        ),
        "cypher": (
            "MATCH (a:Resource)-[:DEPENDS_ON]->(b:Resource) "
            "WHERE a.type = 'aws_lambda_function' "
            "RETURN b.id AS dep, b.type AS type"
        ),
        "why_combined": (
            "Kendra returns Vault secret-rotation and Terraform IAM docs; graph "
            "lists the actual Lambda functions and their dependencies"
        ),
        "raw_sources": (
            "Vault AWS secrets engine docs + Terraform Lambda resource docs + grep .tf files"
        ),
        "raw_tokens_estimate": 15000,
    },
    {
        "topic": "CI/CD pipeline structure and configuration",
        "rag_query": (
            "How should CodeBuild projects be configured in Terraform for a "
            "CI/CD pipeline following HashiCorp recommended patterns?"
        ),
        "cypher": (
            "MATCH (a:Resource)-[:DEPENDS_ON]->(b:Resource) "
            "WHERE a.type = 'aws_codebuild_project' "
            "RETURN b.id AS dep, b.type AS type"
        ),
        "why_combined": (
            "Kendra provides Terraform CI/CD pattern docs; graph reveals the "
            "actual CodeBuild resources and their dependency chain"
        ),
        "raw_sources": (
            "Terraform CodeBuild docs + HCP Terraform run-task docs + "
            "grep .tf files + terraform graph output"
        ),
        "raw_tokens_estimate": 17000,
    },
    {
        "topic": "Neptune deployment vs Terraform database guidance",
        "rag_query": (
            "What does HashiCorp documentation recommend for managing Neptune "
            "clusters and instances with Terraform, including engine versions?"
        ),
        "cypher": (
            "MATCH (a:Resource)-[:DEPENDS_ON*1..2]->(b:Resource) "
            "WHERE a.type = 'aws_neptune_cluster' "
            "RETURN DISTINCT b.id AS dep, b.type AS type"
        ),
        "why_combined": (
            "Kendra covers Terraform Neptune resource docs and engine guidance; "
            "graph shows the actual deployed Neptune resources for comparison"
        ),
        "raw_sources": (
            "Terraform aws_neptune_cluster docs + aws_neptune_cluster_instance docs "
            "+ grep .tf files for neptune blocks"
        ),
        "raw_tokens_estimate": 14000,
    },
    {
        "topic": "Step Functions orchestration design and implementation",
        "rag_query": (
            "How should Step Functions and EventBridge Scheduler be configured in "
            "Terraform to orchestrate a data pipeline?"
        ),
        "cypher": (
            "MATCH (a:Resource)-[:DEPENDS_ON]->(b:Resource) "
            "WHERE a.type = 'aws_sfn_state_machine' "
            "RETURN b.id AS dep, b.type AS type"
        ),
        "why_combined": (
            "Kendra provides Terraform orchestration pattern docs; graph reveals "
            "the deployed state machine resources and their dependencies"
        ),
        "raw_sources": (
            "Terraform Step Functions docs + EventBridge Scheduler docs + grep .tf "
            "files + terraform graph output"
        ),
        "raw_tokens_estimate": 16000,
    },
    {
        "topic": "State backend storage and bucket configuration",
        "rag_query": (
            "What are Terraform best practices for configuring S3 buckets as "
            "remote state backends, including versioning and locking?"
        ),
        "cypher": (
            "MATCH (a:Resource)-[:DEPENDS_ON]->(b:Resource) "
            "WHERE a.type = 'aws_s3_bucket' "
            "RETURN b.id AS dep, b.type AS type"
        ),
        "why_combined": (
            "Kendra returns Terraform state backend docs; graph shows the actual "
            "S3 buckets deployed so the answer can verify the backend setup"
        ),
        "raw_sources": (
            "Terraform S3 backend docs + state locking page + versioning docs "
            "+ grep .tf files for bucket resources"
        ),
        "raw_tokens_estimate": 15500,
    },
    {
        "topic": "Scheduler-driven Step Functions orchestration patterns",
        "rag_query": (
            "What are HashiCorp best practices for using EventBridge Scheduler to "
            "trigger Step Functions in a Terraform-managed pipeline?"
        ),
        "cypher": (
            "MATCH (r:Resource {type: 'aws_scheduler_schedule'}) "
            "RETURN r.id AS resource, r.name AS name"
        ),
        "why_combined": (
            "Kendra provides Terraform scheduler and orchestration docs; graph shows "
            "the actual scheduler resources and their trigger targets"
        ),
        "raw_sources": (
            "Terraform EventBridge Scheduler docs + Step Functions docs + "
            "grep .tf files for scheduler_schedule blocks"
        ),
        "raw_tokens_estimate": 14500,
    },
    {
        "topic": "VPC networking and security group configuration",
        "rag_query": (
            "How should VPC subnets and security groups be configured in "
            "Terraform for private services like Neptune and Lambda?"
        ),
        "cypher": (
            "MATCH (a:Resource)-[:DEPENDS_ON]->(b:Resource) "
            "WHERE b.type = 'aws_security_group' "
            "RETURN a.id AS resource, a.type AS type"
        ),
        "why_combined": (
            "Kendra returns Terraform VPC and security group docs; graph reveals "
            "the deployed security groups and what depends on them"
        ),
        "raw_sources": (
            "Terraform aws_vpc docs + aws_security_group docs + "
            "grep .tf files for security_group blocks"
        ),
        "raw_tokens_estimate": 13000,
    },
    {
        "topic": "Vault-managed secrets for AWS services",
        "rag_query": (
            "How does HashiCorp Vault integrate with AWS to dynamically "
            "generate IAM credentials using the AWS secrets engine?"
        ),
        "cypher": (
            "MATCH (r:Resource {type: 'aws_iam_role'}) "
            "RETURN r.id AS resource, r.name AS name"
        ),
        "why_combined": (
            "Kendra provides Vault AWS secrets engine documentation; graph shows "
            "which IAM roles exist to validate rotation coverage"
        ),
        "raw_sources": (
            "Vault AWS secrets engine docs + Terraform IAM docs + "
            "grep .tf files for iam_role resources"
        ),
        "raw_tokens_estimate": 16500,
    },
    {
        "topic": "Kendra index ingestion and data source configuration",
        "rag_query": (
            "How should an Amazon Kendra index be configured and populated "
            "with documents using Terraform and S3 data sources?"
        ),
        "cypher": (
            "MATCH (a:Resource)-[:DEPENDS_ON]->(b:Resource) "
            "WHERE a.type = 'aws_kendra_index' "
            "RETURN b.id AS dep, b.type AS type"
        ),
        "why_combined": (
            "Kendra returns Kendra index configuration docs; graph "
            "shows the S3 buckets and IAM roles used for document ingestion"
        ),
        "raw_sources": (
            "Amazon Kendra docs + Terraform aws_kendra_index docs + "
            "grep .tf files + scripts pipeline code"
        ),
        "raw_tokens_estimate": 19000,
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
        query, raw_tokens = test["query"], test["raw_tokens_estimate"]
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
        query, cypher, raw_tokens = test["query"], test["cypher"], test["raw_tokens_estimate"]
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
        query = test["rag_query"]
        raw_tokens = test["raw_tokens_estimate"]
        try:
            kendra_text = kendra_retrieve(client, index_id, test["rag_query"], top_k)
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
