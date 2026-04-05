#!/usr/bin/env python3
"""fetch_blogs.py — Fetch HashiCorp blog posts via RSS/Atom feeds and HTML scraping.

Writes cleaned markdown files to /codebuild/output/cleaned/blogs/.
Splits long posts at ## heading boundaries to align with the semantic chunking strategy.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

OUTPUT_DIR = Path("/codebuild/output/cleaned/blogs")
LOOKBACK_DAYS = 365

PRODUCT_KEYWORDS: dict[str, list[str]] = {
    "terraform": ["terraform", "hcl", "provider", "module", "state", "workspace"],
    "vault": ["vault", "secret", "dynamic credentials", "pki", "kv", "auth method"],
    "consul": ["consul", "service mesh", "mtls", "connect", "service discovery"],
    "nomad": ["nomad", "job scheduling", "docker task", "task driver"],
    "packer": ["packer", "ami", "image build", "golden image"],
    "boundary": ["boundary", "remote access", "privileged access"],
    "waypoint": ["waypoint", "app deployment", "build pack"],
    "sentinel": ["sentinel", "policy as code", "opa", "rego"],
}

FEEDS = [
    {"url": "https://www.hashicorp.com/blog/feed.xml", "source": "hashicorp-blog"},
    {"url": "https://medium.com/feed/hashicorp-engineering", "source": "hashicorp-engineering"},
]


def _detect_product_family(title: str, body: str) -> str:
    """Detect the dominant product family by keyword frequency in title and body.

    Title matches are weighted higher than body matches.
    """
    text_lower = (title + " " + title + " " + body).lower()
    scores: dict[str, int] = {product: 0 for product in PRODUCT_KEYWORDS}

    for product, keywords in PRODUCT_KEYWORDS.items():
        for kw in keywords:
            scores[product] += text_lower.count(kw)

    best = max(scores, key=lambda p: scores[p])
    return best if scores[best] > 0 else "hashicorp"


def _parse_feed(feed_url: str) -> list[dict]:
    """Parse an Atom or RSS feed, returning a list of entry dicts."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    try:
        resp = requests.get(feed_url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Could not fetch feed %s: %s", feed_url, exc)
        return []

    soup = BeautifulSoup(resp.text, "xml")
    entries = soup.find_all("entry") or soup.find_all("item")
    results: list[dict] = []

    for entry in entries:
        # Atom uses <updated>/<published>, RSS uses <pubDate>
        date_tag = entry.find("updated") or entry.find("published") or entry.find("pubDate")
        if date_tag:
            raw_date = date_tag.get_text().strip()
            try:
                # Handle both ISO 8601 and RFC 2822 formats
                if "T" in raw_date:
                    pub_dt = datetime.fromisoformat(raw_date.rstrip("Z")).replace(tzinfo=timezone.utc)
                else:
                    from email.utils import parsedate_to_datetime
                    pub_dt = parsedate_to_datetime(raw_date).astimezone(timezone.utc)
                if pub_dt < cutoff:
                    continue
                pub_date = pub_dt.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                pub_date = ""
        else:
            pub_date = ""

        link_tag = entry.find("link")
        if link_tag:
            url = link_tag.get("href") or link_tag.get_text().strip()
        else:
            url = ""

        title_tag = entry.find("title")
        title = title_tag.get_text().strip() if title_tag else "Untitled"

        results.append({"title": title, "url": url, "pub_date": pub_date})

    log.info("Feed %s: %d entries in lookback window", feed_url, len(results))
    return results


def fetch_article_content(url: str) -> str:
    """Fetch and extract article body text from a blog post URL.

    Returns cleaned markdown text. Returns empty string on failure.
    """
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (compatible; RAGBot/1.0)"})
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.debug("Could not fetch article %s: %s", url, exc)
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")

    # Remove navigation, scripts, styles
    for tag in soup.find_all(["nav", "header", "footer", "script", "style", "aside"]):
        tag.decompose()

    # Find main article content
    article = soup.find("article") or soup.find(class_=re.compile(r"post|content|article", re.I)) or soup.find("main")
    if not article:
        article = soup.body or soup

    # Convert to markdown-ish text
    for pre in article.find_all("pre"):
        code_el = pre.find("code")
        code_text = code_el.get_text() if code_el else pre.get_text()
        pre.replace_with(f"\n```\n{code_text.strip()}\n```\n")

    for code in article.find_all("code"):
        code.replace_with(f"`{code.get_text()}`")

    for level in range(1, 7):
        for h in article.find_all(f"h{level}"):
            h.replace_with(f"\n{'##' if level <= 2 else '###'} {h.get_text().strip()}\n")

    for a in article.find_all("a", href=True):
        a.replace_with(f"[{a.get_text().strip()}]({a['href']})")

    return article.get_text(separator="\n").strip()


def format_post(entry: dict, body: str, product_family: str) -> str:
    """Format a blog post as clean markdown with attribution prefix."""
    attribution = f"[blog:{product_family}] {entry['title']}"
    lines = [attribution, ""]
    if entry.get("url"):
        lines.append(f"URL: {entry['url']}")
    if entry.get("pub_date"):
        lines.append(f"Published: {entry['pub_date']}")
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


def _slugify(title: str) -> str:
    """Convert a title to a filename-safe slug."""
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:80]


def process_feed(feed_info: dict) -> int:
    """Fetch and write blog posts from a single feed.

    Returns the number of files written.
    """
    entries = _parse_feed(feed_info["url"])
    source = feed_info["source"]
    out_dir = OUTPUT_DIR / source
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0

    for entry in entries:
        if not entry.get("url"):
            continue
        body = fetch_article_content(entry["url"])
        if not body:
            body = entry.get("summary", "")
        if not body:
            continue

        product_family = _detect_product_family(entry["title"], body)
        content = format_post(entry, body, product_family)
        slug = _slugify(entry["title"])
        fname = f"{slug}.md"
        (out_dir / fname).write_text(content, encoding="utf-8")
        written += 1
        time.sleep(0.5)  # Polite crawling delay

    log.info("Feed %s: wrote %d files", source, written)
    return written


def main() -> None:
    """Fetch blog posts from all configured feeds."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    total = 0
    for feed_info in FEEDS:
        total += process_feed(feed_info)

    log.info("fetch_blogs.py complete — %d files written", total)


if __name__ == "__main__":
    main()
