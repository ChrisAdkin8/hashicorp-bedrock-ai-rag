#!/usr/bin/env python3
"""discover_modules.py — Query the Terraform Registry for verified HashiCorp modules.

Writes clone URLs to /codebuild/output/modules.txt, which is consumed by
clone_modules.sh in the pre_build phase.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

REGISTRY_URL = "https://registry.terraform.io/v1/modules"
OUTPUT_FILE = Path("/codebuild/output/modules.txt")
MAX_MODULES = 50


def fetch_verified_modules(namespace: str = "hashicorp") -> list[dict]:
    """Fetch verified modules from the Terraform Registry for the given namespace.

    Returns a list of module metadata dicts from the registry API response.
    """
    modules: list[dict] = []
    url = f"{REGISTRY_URL}?namespace={namespace}&verified=true&limit=50"

    while url and len(modules) < MAX_MODULES:
        log.info("Fetching: %s", url)
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning("Registry request failed: %s — stopping pagination", exc)
            break

        data = resp.json()
        batch = data.get("modules", [])
        modules.extend(batch)
        log.info("Fetched %d modules (total: %d)", len(batch), len(modules))

        next_url = data.get("meta", {}).get("next_url")
        url = f"https://registry.terraform.io{next_url}" if next_url else ""

    return modules[:MAX_MODULES]


def module_source_url(module: dict) -> str | None:
    """Extract the GitHub clone URL from a registry module metadata dict.

    Returns None if the source cannot be determined.
    """
    source = module.get("source", "")
    if source.startswith("github.com/"):
        return f"https://{source}.git"
    return None


def main() -> None:
    """Discover verified Terraform Registry modules and write clone URLs to file."""
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    modules = fetch_verified_modules()
    urls: list[str] = []

    for module in modules:
        url = module_source_url(module)
        if url:
            urls.append(url)
            log.debug("Module %s/%s/%s -> %s", module.get("namespace"), module.get("name"), module.get("provider"), url)
        else:
            log.debug("No GitHub source for %s — skipping", module.get("id"))

    OUTPUT_FILE.write_text("\n".join(urls) + "\n")
    log.info("Wrote %d module clone URLs to %s", len(urls), OUTPUT_FILE)


if __name__ == "__main__":
    main()
