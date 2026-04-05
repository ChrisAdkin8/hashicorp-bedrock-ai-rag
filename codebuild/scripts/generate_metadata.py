#!/usr/bin/env python3
"""generate_metadata.py — Generate metadata.jsonl sidecar files for S3 objects.

Reads all cleaned markdown files from /codebuild/output/cleaned/, infers
product, product_family, and source_type from their path structure, and writes
a companion .jsonl file (one JSON object per line) next to each document.

The .jsonl sidecars are uploaded alongside the markdown files and used by
Bedrock to associate metadata with each knowledge base document.

Usage:
    python3 generate_metadata.py --bucket s3://my-bucket-name
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

INPUT_DIR = Path("/codebuild/output/cleaned")

SOURCE_TYPE_MAP: dict[str, str] = {
    "documentation": "documentation",
    "provider": "provider",
    "module": "module",
    "sentinel": "sentinel",
    "issues": "issue",
    "discuss": "discuss",
    "blogs": "blog",
}

PRODUCT_FAMILY_OVERRIDES: dict[str, str] = {
    "provider": "terraform",
    "module": "terraform",
    "sentinel": "terraform",
}


def _infer_metadata(path: Path, bucket: str) -> dict:
    """Infer metadata fields from a file's path under INPUT_DIR.

    Returns a dict with keys: s3_uri, product, product_family, source_type.
    """
    rel = path.relative_to(INPUT_DIR)
    parts = rel.parts  # e.g. ("provider", "terraform-provider-aws", "website", "docs", "r", "instance.md")

    raw_source_type = parts[0] if parts else "documentation"
    source_type = SOURCE_TYPE_MAP.get(raw_source_type, raw_source_type)

    # Infer product from directory structure
    product = "hashicorp"
    if source_type == "provider" and len(parts) >= 2:
        repo_name = parts[1]
        product = repo_name.removeprefix("terraform-provider-")
    elif source_type == "documentation" and len(parts) >= 2:
        product = parts[1]
    elif source_type == "issue" and len(parts) >= 3:
        product = parts[2]  # org/repo/...
    elif source_type == "discuss" and len(parts) >= 2:
        product = parts[1]
    elif source_type == "blog" and len(parts) >= 2:
        product = parts[1]

    product_family = PRODUCT_FAMILY_OVERRIDES.get(source_type, product)

    # S3 object key is the relative path with forward slashes
    s3_key = "/".join(rel.with_suffix(".md").parts)
    s3_uri = f"s3://{bucket}/{s3_key}" if bucket else s3_key

    return {
        "metadataAttributes": {
            "product": product,
            "product_family": product_family,
            "source_type": source_type,
        }
    }


def write_sidecar(path: Path, metadata: dict) -> None:
    """Write a Bedrock-compatible metadata JSON sidecar next to the document."""
    sidecar_path = path.with_suffix(".md.metadata.json")
    sidecar_path.write_text(json.dumps(metadata, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    """Generate metadata sidecar files for all cleaned documents."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True, help="S3 bucket name (without s3:// prefix)")
    args = parser.parse_args()

    bucket = args.bucket.removeprefix("s3://")
    md_files = sorted(INPUT_DIR.rglob("*.md"))
    written = 0

    for path in md_files:
        metadata = _infer_metadata(path, bucket)
        write_sidecar(path, metadata)
        written += 1
        log.debug("Wrote sidecar for %s", path)

    log.info("generate_metadata.py complete — %d sidecar files written", written)


if __name__ == "__main__":
    main()
