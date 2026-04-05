#!/usr/bin/env python3
"""test_server.py — Smoke-test the MCP server tool functions.

Runs tool functions directly (bypassing the MCP protocol) to confirm that
credentials, environment variables, and retrieval are working correctly.

Requires environment variables:
  AWS_REGION              — AWS region
  AWS_KNOWLEDGE_BASE_ID   — Bedrock Knowledge Base ID

Usage:
    python3 mcp/test_server.py
"""

from __future__ import annotations

import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)


def _check_env() -> bool:
    """Verify required environment variables are set."""
    missing = []
    for var in ("AWS_REGION", "AWS_KNOWLEDGE_BASE_ID"):
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        log.error("Missing environment variables: %s", ", ".join(missing))
        return False
    return True


def main() -> None:
    """Run smoke tests against the live Knowledge Base."""
    if not _check_env():
        sys.exit(1)

    # Import here so missing env vars are caught first
    from server import get_knowledge_base_info, search_hashicorp_docs

    failures = 0

    # ── Test 1: get_knowledge_base_info ──────────────────────────────────────
    log.info("Test 1: get_knowledge_base_info")
    info = get_knowledge_base_info()
    if "error" in info or "auth_error" in info:
        log.error("FAIL: %s", info)
        failures += 1
    else:
        log.info("PASS: region=%s kb_id=%s status=%s", info.get("region"), info.get("knowledge_base_id"), info.get("knowledge_base_status", "n/a"))

    # ── Test 2: basic search ──────────────────────────────────────────────────
    log.info("Test 2: search_hashicorp_docs (basic)")
    results = search_hashicorp_docs(query="How do I configure the AWS provider in Terraform?", top_k=3)
    if not results or "error" in results[0]:
        log.warning("WARN: Zero results for basic search (may indicate empty knowledge base)")
    else:
        log.info("PASS: %d results, top score=%.4f", len(results), results[0].get("score", 0))

    # ── Test 3: filtered search ───────────────────────────────────────────────
    log.info("Test 3: search_hashicorp_docs (product_family=vault)")
    results = search_hashicorp_docs(
        query="dynamic secrets database Vault",
        top_k=5,
        product_family="vault",
    )
    if not results or "error" in results[0]:
        log.warning("WARN: Zero results for vault filter (may indicate empty knowledge base)")
    else:
        wrong_family = [r for r in results if r.get("product_family") != "vault"]
        if wrong_family:
            log.error("FAIL: Results include non-vault product_family entries: %s", wrong_family)
            failures += 1
        else:
            log.info("PASS: %d results, all product_family=vault", len(results))

    # ── Test 4: no-results edge case ──────────────────────────────────────────
    log.info("Test 4: search_hashicorp_docs (nonsense query)")
    results = search_hashicorp_docs(query="xyzzy frobnicator quux hashicorp", top_k=3, min_score=0.99)
    if "error" in (results[0] if results else {}):
        log.error("FAIL: Unexpected error on no-results query: %s", results)
        failures += 1
    else:
        log.info("PASS: %d results (expected 0 for nonsense+high threshold)", len(results))

    # ── Summary ───────────────────────────────────────────────────────────────
    if failures > 0:
        log.error("%d test(s) failed", failures)
        sys.exit(1)
    else:
        log.info("All smoke tests passed.")


if __name__ == "__main__":
    main()
