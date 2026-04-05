#!/usr/bin/env python3
"""process_docs.py — Extract and semantically split markdown from cloned repos.

Reads repos from /codebuild/output/repos/, splits documents at ## and ###
heading boundaries, enriches with metadata attribution prefixes, and writes
cleaned output to /codebuild/output/cleaned/.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

REPOS_DIR = Path("/codebuild/output/repos")
OUTPUT_DIR = Path("/codebuild/output/cleaned")
MIN_SECTION_SIZE = 200
MAX_SECTION_CHARS = 4000

REPO_CONFIG: dict[str, dict] = {
    "terraform": {
        "docs_subdirs": ["website/docs", "website/content"],
        "source_type": "documentation",
        "product": "terraform",
        "product_family": "terraform",
    },
    "vault": {
        "docs_subdirs": ["website/docs", "website/content"],
        "source_type": "documentation",
        "product": "vault",
        "product_family": "vault",
    },
    "consul": {
        "docs_subdirs": ["website/docs", "website/content"],
        "source_type": "documentation",
        "product": "consul",
        "product_family": "consul",
    },
    "nomad": {
        "docs_subdirs": ["website/docs", "website/content"],
        "source_type": "documentation",
        "product": "nomad",
        "product_family": "nomad",
    },
    "packer": {
        "docs_subdirs": ["website/docs", "website/content"],
        "source_type": "documentation",
        "product": "packer",
        "product_family": "packer",
    },
    "boundary": {
        "docs_subdirs": ["website/docs", "website/content"],
        "source_type": "documentation",
        "product": "boundary",
        "product_family": "boundary",
    },
    "waypoint": {
        "docs_subdirs": ["website/docs", "website/content"],
        "source_type": "documentation",
        "product": "waypoint",
        "product_family": "waypoint",
    },
}

PROVIDER_CONFIG_TEMPLATE: dict = {
    "docs_subdirs": ["website/docs"],
    "source_type": "provider",
    "product_family": "terraform",
}

SENTINEL_CONFIG_TEMPLATE: dict = {
    "docs_subdirs": [".", "docs"],
    "source_type": "sentinel",
    "product_family": "terraform",
    "product": "sentinel",
}


def _detect_provider_repos() -> dict[str, dict]:
    """Detect provider repos in REPOS_DIR and generate config entries."""
    configs: dict[str, dict] = {}
    for repo_dir in REPOS_DIR.iterdir():
        name = repo_dir.name
        if name.startswith("terraform-provider-"):
            product = name.removeprefix("terraform-provider-")
            configs[name] = {**PROVIDER_CONFIG_TEMPLATE, "product": product}
    return configs


def _detect_sentinel_repos() -> dict[str, dict]:
    """Detect sentinel policy repos in REPOS_DIR and generate config entries."""
    configs: dict[str, dict] = {}
    for repo_dir in REPOS_DIR.iterdir():
        name = repo_dir.name
        if "sentinel" in name.lower():
            configs[name] = {**SENTINEL_CONFIG_TEMPLATE}
    return configs


def _split_at_headings(text: str) -> list[tuple[str, str]]:
    """Split markdown text at ## and ### heading boundaries.

    Returns a list of (heading_title, section_body) tuples. The first element
    may have an empty heading if the document starts with content before any heading.
    """
    sections: list[tuple[str, str]] = []
    pattern = re.compile(r"^(#{2,3})\s+(.+)$", re.MULTILINE)
    last_end = 0
    last_title = ""

    for match in pattern.finditer(text):
        segment = text[last_end : match.start()].strip()
        if segment:
            sections.append((last_title, segment))
        last_title = match.group(2).strip()
        last_end = match.end()

    # Append remaining content after the last heading
    tail = text[last_end:].strip()
    if tail:
        sections.append((last_title, tail))

    return sections


def _split_large_section(text: str, max_chars: int = MAX_SECTION_CHARS) -> list[str]:
    """Split an oversized section at code-fence boundaries.

    Splits at ``` boundaries when text exceeds max_chars, ensuring no code
    block is split mid-fence.
    """
    if len(text) <= max_chars:
        return [text]

    parts: list[str] = []
    current: list[str] = []
    current_len = 0
    in_fence = False

    for line in text.splitlines(keepends=True):
        if line.strip().startswith("```"):
            in_fence = not in_fence

        current.append(line)
        current_len += len(line)

        if current_len >= max_chars and not in_fence:
            parts.append("".join(current).strip())
            current = []
            current_len = 0

    if current:
        parts.append("".join(current).strip())

    return [p for p in parts if p]


def _make_attribution(source_type: str, product: str, title: str) -> str:
    """Build a compact single-line attribution prefix for a document body."""
    return f"[{source_type}:{product}] {title}"


def _infer_doc_category(path: Path) -> str:
    """Infer a doc_category from the file path."""
    p = str(path).lower()
    if "/r/" in p or "/resources/" in p:
        return "resource-reference"
    if "/d/" in p or "/data-sources/" in p:
        return "data-source-reference"
    if "getting-started" in p:
        return "getting-started"
    if "cli" in p:
        return "cli-reference"
    if "api" in p:
        return "api-reference"
    if "internals" in p:
        return "internals"
    if "upgrade" in p:
        return "upgrade-guide"
    if "configuration" in p or "config" in p:
        return "configuration"
    if "guide" in p:
        return "guide"
    return "documentation"


def _extract_resource_type(path: Path, source_type: str) -> str | None:
    """Extract Terraform resource/data-source name from the file path."""
    if source_type != "provider":
        return None
    stem = path.stem
    # Remove common HTML extensions encoded in filename (e.g., instance.html)
    stem = stem.replace(".html", "")
    # Build the full resource name: provider prefix + stem
    # The file path pattern: website/docs/r/instance.html.markdown -> aws_instance
    parts = str(path).split("/")
    if len(parts) >= 2 and parts[-2] in ("r", "d", "resources", "data-sources"):
        return stem
    return None


def process_repo(repo_name: str, config: dict) -> int:
    """Process a single cloned repository.

    Returns the number of output files written.
    """
    repo_dir = REPOS_DIR / repo_name
    if not repo_dir.exists():
        log.warning("Repo not found: %s — skipping", repo_dir)
        return 0

    source_type = config["source_type"]
    product = config.get("product", repo_name)
    product_family = config.get("product_family", product)
    docs_subdirs = config.get("docs_subdirs", ["website/docs"])

    md_files: list[Path] = []
    for subdir in docs_subdirs:
        docs_path = repo_dir / subdir
        if docs_path.exists():
            md_files.extend(docs_path.rglob("*.md"))
            md_files.extend(docs_path.rglob("*.mdx"))
            md_files.extend(docs_path.rglob("*.markdown"))

    if not md_files:
        log.info("No markdown files found in %s — skipping", repo_name)
        return 0

    out_dir = OUTPUT_DIR / source_type / repo_name
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0

    for md_file in md_files:
        try:
            raw = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("Cannot read %s: %s", md_file, exc)
            continue

        rel_path = md_file.relative_to(repo_dir)
        doc_category = _infer_doc_category(rel_path)
        resource_type = _extract_resource_type(rel_path, source_type)
        title = md_file.stem.replace("-", " ").replace("_", " ").title()
        if resource_type:
            title = resource_type

        sections = _split_at_headings(raw)

        # Merge tiny sections into previous
        merged: list[tuple[str, str]] = []
        for heading, body in sections:
            if merged and len(body) < MIN_SECTION_SIZE:
                prev_heading, prev_body = merged[-1]
                merged[-1] = (prev_heading, prev_body + "\n\n" + body)
            else:
                merged.append((heading, body))

        if len(merged) == 1:
            # Single section — write as original path
            heading, body = merged[0]
            attribution = _make_attribution(source_type, product, title)
            content = f"{attribution}\n\n{body}"
            out_file = out_dir / rel_path.with_suffix(".md")
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_text(content, encoding="utf-8")
            written += 1
        else:
            # Multi-section document
            for idx, (heading, body) in enumerate(merged):
                section_title = heading or title
                attribution = _make_attribution(source_type, product, f"{title} — {section_title}")
                subsections = _split_large_section(body)
                for sub_idx, sub_body in enumerate(subsections):
                    content = f"{attribution}\n\n{sub_body}"
                    suffix = f"_s{idx}" if len(subsections) == 1 else f"_s{idx}_{sub_idx}"
                    out_name = rel_path.stem + suffix + ".md"
                    out_file = out_dir / rel_path.parent / out_name
                    out_file.parent.mkdir(parents=True, exist_ok=True)
                    out_file.write_text(content, encoding="utf-8")
                    written += 1

    log.info("Processed %s — wrote %d files", repo_name, written)
    return written


def main() -> None:
    """Process all detected repos and write cleaned output."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    all_configs = {**REPO_CONFIG, **_detect_provider_repos(), **_detect_sentinel_repos()}

    total = 0
    for repo_name, config in sorted(all_configs.items()):
        total += process_repo(repo_name, config)

    log.info("process_docs.py complete — %d output files total", total)


if __name__ == "__main__":
    main()
