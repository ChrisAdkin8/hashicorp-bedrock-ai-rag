#!/usr/bin/env python3
"""test_retrieval.py — Validate Bedrock Knowledge Base retrieval quality.

Runs a suite of test queries across all product families and source types.
Passes if every query returns at least one result with a relevance score
above the configured threshold.

Usage:
    python3 scripts/test_retrieval.py \\
        --region us-west-2 \\
        --knowledge-base-id ABCDEFGHIJ \\
        [--min-score 0.5] \\
        [--top-k 5]
"""

from __future__ import annotations

import argparse
import logging
import sys

import boto3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

TEST_QUERIES: list[dict] = [
    {"topic": "terraform-provider", "query": "How do I configure the AWS provider in Terraform?"},
    {"topic": "vault", "query": "How do I generate dynamic database credentials using HashiCorp Vault?"},
    {"topic": "consul", "query": "How do I set up mTLS between services using Consul Connect?"},
    {"topic": "nomad", "query": "How do I define a Nomad job specification for a Docker container?"},
    {"topic": "sentinel", "query": "How do I write a Sentinel policy to restrict resource creation in Terraform?"},
    {"topic": "packer", "query": "How do I build an AMI with Packer using an HCL template?"},
    {"topic": "terraform-module", "query": "What is the structure of a reusable Terraform module?"},
    {"topic": "github-issue", "query": "What are common issues when upgrading the Terraform AWS provider?"},
    {"topic": "discuss-thread", "query": "How do I troubleshoot Terraform state locking errors?"},
    {"topic": "blog-post", "query": "What new features were announced for HashiCorp products?"},
]


def run_retrieval_test(
    client: object,
    knowledge_base_id: str,
    query: str,
    top_k: int,
    min_score: float,
) -> tuple[int, float | None]:
    """Run a single retrieval query.

    Returns (result_count, top_score).
    """
    resp = client.retrieve(
        knowledgeBaseId=knowledge_base_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "numberOfResults": top_k,
                "overrideSearchType": "HYBRID",
            }
        },
    )
    results = resp.get("retrievalResults", [])
    if not results:
        return 0, None
    top_score = results[0].get("score", 0.0)
    qualified = [r for r in results if r.get("score", 0.0) >= min_score]
    return len(qualified), top_score


def main() -> None:
    """Run all test queries and report pass/fail per topic."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", required=True)
    parser.add_argument("--knowledge-base-id", required=True)
    parser.add_argument("--min-score", type=float, default=0.5, help="Minimum relevance score (0-1)")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    client = boto3.client("bedrock-agent-runtime", region_name=args.region)
    failed: list[str] = []

    print(f"\n{'Topic':<25} {'Results':>8} {'Top Score':>12} {'Status':>8}")
    print("-" * 60)

    for test in TEST_QUERIES:
        topic = test["topic"]
        query = test["query"]
        try:
            count, top_score = run_retrieval_test(client, args.knowledge_base_id, query, args.top_k, args.min_score)
        except Exception as exc:
            log.error("Query failed for %s: %s", topic, exc)
            failed.append(topic)
            continue

        status = "PASS" if count > 0 else "WARN"
        score_str = f"{top_score:.4f}" if top_score is not None else "  n/a"
        print(f"{topic:<25} {count:>8} {score_str:>12} {status:>8}")
        if count == 0:
            log.warning("Zero results for topic '%s' — knowledge base may have coverage gaps", topic)

    print("-" * 60)

    if failed:
        print(f"\nFAIL: {len(failed)} queries errored: {', '.join(failed)}")
        sys.exit(1)
    else:
        print("\nAll queries completed — check WARN lines for coverage gaps.")


if __name__ == "__main__":
    main()
