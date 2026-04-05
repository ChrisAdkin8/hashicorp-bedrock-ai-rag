#!/usr/bin/env python3
"""test_token_efficiency.py — Compare RAG token cost vs raw documentation.

Runs cross-product queries against the Bedrock Knowledge Base and estimates
the token savings compared to pasting full documentation pages.

Usage:
    python3 scripts/test_token_efficiency.py \\
        --region us-west-2 \\
        --knowledge-base-id ABCDEFGHIJ \\
        [--top-k 5] \\
        [--min-score 0.0]
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

# Estimated raw documentation token counts (full pages, conservative)
TEST_QUERIES: list[dict] = [
    {"query": "How do I configure an S3 backend in Terraform?",             "raw_tokens": 9500},
    {"query": "How do I set up the AWS provider in Terraform?",             "raw_tokens": 11000},
    {"query": "How do I generate dynamic secrets with HashiCorp Vault?",    "raw_tokens": 14000},
    {"query": "How do I configure Consul service mesh with mTLS?",          "raw_tokens": 16000},
    {"query": "How do I build a Packer AMI with an HCL template?",         "raw_tokens": 8500},
    {"query": "How do I use Vault dynamic secrets with the Terraform AWS provider?", "raw_tokens": 22000},
    {"query": "How do I schedule a Docker workload in Nomad?",              "raw_tokens": 12000},
    {"query": "How do I enforce Sentinel policies in Terraform Cloud?",     "raw_tokens": 13500},
    {"query": "How do I compose reusable Terraform modules?",               "raw_tokens": 10000},
    {"query": "How do I integrate Consul service discovery with Vault?",    "raw_tokens": 19500},
]


def _count_tokens(text: str) -> int:
    """Count tokens using tiktoken if available, else approximate with word count × 1.3."""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except ImportError:
        # Rough approximation: ~0.75 words per token
        return int(len(text.split()) * 1.3)


def retrieve(client: object, kb_id: str, query: str, top_k: int, min_score: float) -> str:
    """Retrieve context from the knowledge base and return concatenated chunks."""
    resp = client.retrieve(
        knowledgeBaseId=kb_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "numberOfResults": top_k,
                "overrideSearchType": "HYBRID",
            }
        },
    )
    results = resp.get("retrievalResults", [])
    qualified = [r for r in results if r.get("score", 0.0) >= min_score]
    chunks = [r["content"]["text"] for r in qualified if r.get("content", {}).get("text")]
    return "\n\n---\n\n".join(chunks)


def main() -> None:
    """Run token efficiency benchmark across all test queries."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", required=True)
    parser.add_argument("--knowledge-base-id", required=True)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--min-score", type=float, default=0.0)
    args = parser.parse_args()

    try:
        import tiktoken  # noqa: F401
        token_method = "tiktoken (cl100k_base)"
    except ImportError:
        token_method = "word-count approximation (install tiktoken for exact counts)"

    log.info("Token counting: %s", token_method)

    client = boto3.client("bedrock-agent-runtime", region_name=args.region)

    total_rag = 0
    total_raw = 0

    print(f"\n{'Query':<60} {'RAG':>6} {'Raw':>8} {'Saving':>8}")
    print("-" * 85)

    for test in TEST_QUERIES:
        query = test["query"]
        raw_tokens = test["raw_tokens"]
        try:
            context = retrieve(client, args.knowledge_base_id, query, args.top_k, args.min_score)
            rag_tokens = _count_tokens(context)
        except Exception as exc:
            log.error("Retrieval failed for '%s': %s", query[:40], exc)
            continue

        saving_pct = int((1 - rag_tokens / raw_tokens) * 100) if raw_tokens > 0 else 0
        short_query = query[:58] + ".." if len(query) > 60 else query
        print(f"{short_query:<60} {rag_tokens:>6} {raw_tokens:>8} {saving_pct:>7}%")
        total_rag += rag_tokens
        total_raw += raw_tokens

    print("-" * 85)
    overall_saving = int((1 - total_rag / total_raw) * 100) if total_raw > 0 else 0
    print(f"{'Total':<60} {total_rag:>6} {total_raw:>8} {overall_saving:>7}%")
    print(f"\nAverage RAG tokens/query: {total_rag // len(TEST_QUERIES)}")
    print(f"Average raw tokens/query: {total_raw // len(TEST_QUERIES)}")
    print(f"Overall token saving:      {overall_saving}%")


if __name__ == "__main__":
    main()
