#!/usr/bin/env python3
"""fetch_discuss.py — Fetch HashiCorp Discuss forum threads via the Discourse API.

Writes cleaned markdown files to /codebuild/output/cleaned/discuss/.
Threads with accepted answers are reordered to front-load the resolution.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

BASE_URL = "https://discuss.hashicorp.com"
OUTPUT_DIR = Path("/codebuild/output/cleaned/discuss")
LOOKBACK_DAYS = 365
MAX_REPLIES = 5
MIN_REPLIES = 1

CATEGORIES = [
    "terraform-core",
    "terraform-providers",
    "vault",
    "consul",
    "nomad",
    "packer",
    "boundary",
    "waypoint",
    "sentinel",
]


def _html_to_markdown(html: str) -> str:
    """Convert Discourse HTML post body to clean markdown.

    Preserves code blocks, tables, blockquotes, links, and headings.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Convert code blocks
    for pre in soup.find_all("pre"):
        code_el = pre.find("code")
        code_text = code_el.get_text() if code_el else pre.get_text()
        pre.replace_with(f"\n```\n{code_text.strip()}\n```\n")

    # Convert inline code
    for code in soup.find_all("code"):
        code.replace_with(f"`{code.get_text()}`")

    # Convert headings
    for level in range(1, 7):
        for h in soup.find_all(f"h{level}"):
            h.replace_with(f"\n{'#' * level} {h.get_text().strip()}\n")

    # Convert links
    for a in soup.find_all("a", href=True):
        a.replace_with(f"[{a.get_text().strip()}]({a['href']})")

    # Convert blockquotes
    for bq in soup.find_all("blockquote"):
        lines = bq.get_text().strip().splitlines()
        quoted = "\n".join(f"> {line}" for line in lines)
        bq.replace_with(f"\n{quoted}\n")

    return soup.get_text(separator="\n").strip()


def fetch_category_topics(category_slug: str) -> list[dict]:
    """Fetch topic summaries for a Discourse category.

    Returns topics with at least MIN_REPLIES replies from within LOOKBACK_DAYS.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    url = f"{BASE_URL}/c/{category_slug}.json"
    topics: list[dict] = []

    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Could not fetch category %s: %s", category_slug, exc)
        return []

    data = resp.json()
    for topic in data.get("topic_list", {}).get("topics", []):
        reply_count = topic.get("posts_count", 0) - 1  # subtract original post
        if reply_count < MIN_REPLIES:
            continue
        last_posted = topic.get("last_posted_at", "")
        if last_posted:
            try:
                posted_dt = datetime.fromisoformat(last_posted.rstrip("Z")).replace(tzinfo=timezone.utc)
                if posted_dt < cutoff:
                    continue
            except ValueError:
                pass
        topics.append(topic)

    log.info("Category %s: %d qualifying topics", category_slug, len(topics))
    return topics


def fetch_topic_posts(topic_id: int) -> list[dict]:
    """Fetch posts (question + replies) for a topic."""
    url = f"{BASE_URL}/t/{topic_id}.json"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json().get("post_stream", {}).get("posts", [])
    except requests.RequestException as exc:
        log.debug("Could not fetch topic %d: %s", topic_id, exc)
        return []


def format_thread(category: str, topic: dict, posts: list[dict]) -> str:
    """Format a Discourse thread as clean markdown.

    Reorders accepted answers immediately after the question.
    """
    title = topic.get("title", "")
    topic_id = topic.get("id")
    slug = topic.get("slug", str(topic_id))
    thread_url = f"{BASE_URL}/t/{slug}/{topic_id}"
    last_posted = topic.get("last_posted_at", "")[:10]

    attribution = f"[discuss:{category}] {title}"
    lines = [attribution, "", f"URL: {thread_url}", ""]

    if not posts:
        return "\n".join(lines)

    question = posts[0]
    replies = posts[1 : MAX_REPLIES + 1]

    # Detect accepted answer (post marked as solution/accepted)
    accepted: list[dict] = []
    other_replies: list[dict] = []
    for post in replies:
        if post.get("accepted_answer") or post.get("post_type") == 3:
            accepted.append(post)
        else:
            other_replies.append(post)

    lines.append("## Question\n")
    lines.append(_html_to_markdown(question.get("cooked", "")) + "\n")

    if accepted:
        lines.append("## Accepted Answer\n")
        for post in accepted:
            author = post.get("username", "unknown")
            lines.append(f"**{author}:** {_html_to_markdown(post.get('cooked', ''))}\n")

    if other_replies:
        lines.append("## Replies\n")
        for post in other_replies:
            author = post.get("username", "unknown")
            lines.append(f"**{author}:** {_html_to_markdown(post.get('cooked', ''))}\n")

    return "\n".join(lines)


def process_category(category: str) -> int:
    """Fetch and write threads for a single Discourse category.

    Returns the number of files written.
    """
    topics = fetch_category_topics(category)
    if not topics:
        return 0

    out_dir = OUTPUT_DIR / category
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0

    for topic in topics:
        topic_id = topic.get("id")
        posts = fetch_topic_posts(topic_id)
        has_accepted = any(p.get("accepted_answer") or p.get("post_type") == 3 for p in posts[1:])
        content = format_thread(category, topic, posts)
        slug = topic.get("slug", str(topic_id))
        fname = f"{slug}_{topic_id}.md"
        (out_dir / fname).write_text(content, encoding="utf-8")
        written += 1
        time.sleep(0.2)  # Gentle pacing for Discourse API

    log.info("Category %s: wrote %d files", category, written)
    return written


def main() -> None:
    """Fetch Discuss threads for all configured categories."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    total = 0
    for category in CATEGORIES:
        total += process_category(category)

    log.info("fetch_discuss.py complete — %d files written", total)


if __name__ == "__main__":
    main()
