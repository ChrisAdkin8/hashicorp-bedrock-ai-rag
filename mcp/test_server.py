#!/usr/bin/env python3
"""test_server.py — Smoke-test the MCP server tool functions.

Runs tool functions directly (bypassing the MCP protocol) to confirm that
credentials, environment variables, and retrieval are working correctly.

Requires environment variables:
  AWS_REGION          — AWS region
  AWS_KENDRA_INDEX_ID — Kendra index ID

Optional environment variables (for Neptune tests):
  NEPTUNE_ENDPOINT    — Neptune cluster endpoint
  NEPTUNE_PORT        — Neptune port (default 8182)
  NEPTUNE_IAM_AUTH    — Enable SigV4 auth (default "true")

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
    missing = [v for v in ("AWS_REGION", "AWS_KENDRA_INDEX_ID") if not os.environ.get(v)]
    if missing:
        log.error("Missing environment variables: %s", ", ".join(missing))
        return False
    return True


def main() -> None:
    if not _check_env():
        sys.exit(1)

    from server import (
        get_index_info,
        search_hashicorp_docs,
    )

    failures = 0
    neptune_available = bool(os.environ.get("NEPTUNE_ENDPOINT"))

    # ── Test 1: get_index_info ────────────────────────────────────────────────
    log.info("Test 1: get_index_info")
    info = get_index_info()
    if "error" in info or "auth_error" in info or "index_error" in info:
        log.error("FAIL: %s", info)
        failures += 1
    else:
        log.info("PASS: region=%s index_id=%s status=%s neptune=%s",
                 info.get("region"), info.get("kendra_index_id"),
                 info.get("index_status", "n/a"), info.get("neptune_status", "n/a"))

    # ── Test 2: basic search ──────────────────────────────────────────────────
    log.info("Test 2: search_hashicorp_docs (basic)")
    results = search_hashicorp_docs(query="How do I configure the AWS provider in Terraform?", top_k=3)
    if not results or "error" in results[0]:
        log.warning("WARN: Zero results for basic search (may indicate empty index)")
    else:
        log.info("PASS: %d results, top confidence=%s", len(results), results[0].get("confidence", "n/a"))

    # ── Test 3: filtered search ───────────────────────────────────────────────
    log.info("Test 3: search_hashicorp_docs (product_family=vault)")
    results = search_hashicorp_docs(query="dynamic secrets database Vault", top_k=5, product_family="vault")
    if not results or "error" in results[0]:
        log.warning("WARN: Zero results for vault filter (may indicate empty index)")
    else:
        wrong_family = [r for r in results if r.get("product_family") != "vault"]
        if wrong_family:
            log.error("FAIL: Results include non-vault product_family entries: %s", wrong_family)
            failures += 1
        else:
            log.info("PASS: %d results, all product_family=vault", len(results))

    # ── Test 4: no-results edge case ──────────────────────────────────────────
    log.info("Test 4: search_hashicorp_docs (nonsense query, high min_score)")
    results = search_hashicorp_docs(query="xyzzy frobnicator quux hashicorp", top_k=3, min_score=0.99)
    if results and "error" in results[0]:
        log.error("FAIL: Unexpected error on no-results query: %s", results)
        failures += 1
    else:
        log.info("PASS: %d results (expected 0 for nonsense+high threshold)", len(results))

    # ── Neptune tests (conditional) ───────────────────────────────────────────
    if neptune_available:
        from server import find_resources_by_type, get_resource_dependencies

        # Test 5: find_resources_by_type
        log.info("Test 5: find_resources_by_type (aws_iam_role)")
        results = find_resources_by_type(resource_type="aws_iam_role")
        if results and "error" in results[0]:
            log.error("FAIL: Neptune query error: %s", results[0]["error"])
            failures += 1
        else:
            log.info("PASS: %d resources found (0 is ok if graph is empty)", len(results))

        # Test 6: get_resource_dependencies
        log.info("Test 6: get_resource_dependencies (aws_iam_role, test, both)")
        results = get_resource_dependencies(
            resource_type="aws_iam_role", resource_name="test", direction="both", max_depth=1
        )
        if results and "error" in results[0]:
            log.error("FAIL: Neptune dependency query error: %s", results[0]["error"])
            failures += 1
        else:
            log.info("PASS: %d dependencies found (0 is ok if resource doesn't exist)", len(results))
    else:
        log.info("SKIP: Neptune tests (NEPTUNE_ENDPOINT not set)")

    if failures > 0:
        log.error("%d test(s) failed", failures)
        sys.exit(1)
    else:
        log.info("All smoke tests passed.")


if __name__ == "__main__":
    main()
