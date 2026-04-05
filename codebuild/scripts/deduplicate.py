#!/usr/bin/env python3
"""deduplicate.py — Remove near-duplicate files across all data sources.

Computes SHA-256 of normalised body text (whitespace/case normalised, metadata
prefix stripped). Files processed in sorted path order for determinism — the
first file encountered wins. Files shorter than 100 characters are excluded
from dedup (too short to be meaningful).
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

INPUT_DIR = Path("/codebuild/output/cleaned")
MIN_BODY_CHARS = 100


def _normalise(text: str) -> str:
    """Normalise text for dedup comparison.

    Strips the compact attribution prefix (first line), lowercases, and
    collapses whitespace.
    """
    lines = text.strip().splitlines()
    # Skip the first line (attribution prefix like "[provider:aws] aws_instance …")
    body_lines = lines[1:] if lines and lines[0].startswith("[") else lines
    body = "\n".join(body_lines)
    body = body.lower()
    body = re.sub(r"\s+", " ", body)
    return body.strip()


def _content_hash(normalised: str) -> str:
    """Return the SHA-256 hex digest of normalised content."""
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def deduplicate(input_dir: Path) -> tuple[int, int]:
    """Scan input_dir recursively and remove duplicate markdown files.

    Returns (total_files, removed_files).
    """
    seen_hashes: set[str] = set()
    md_files = sorted(input_dir.rglob("*.md"))
    total = len(md_files)
    removed = 0

    for path in md_files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("Cannot read %s: %s", path, exc)
            continue

        if len(text) < MIN_BODY_CHARS:
            continue  # Too short — skip dedup, keep file

        normalised = _normalise(text)
        if len(normalised) < MIN_BODY_CHARS:
            continue

        digest = _content_hash(normalised)
        if digest in seen_hashes:
            log.debug("Removing duplicate: %s", path)
            path.unlink()
            removed += 1
        else:
            seen_hashes.add(digest)

    return total, removed


def main() -> None:
    """Run deduplication across the cleaned output directory."""
    total, removed = deduplicate(INPUT_DIR)
    kept = total - removed
    log.info(
        "deduplicate.py complete — %d files scanned, %d duplicates removed, %d kept",
        total,
        removed,
        kept,
    )


if __name__ == "__main__":
    main()
