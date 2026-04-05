#!/usr/bin/env python3
"""fetch_github_issues.py — Fetch recent GitHub issues from priority HashiCorp repos.

Writes cleaned markdown files to /codebuild/output/cleaned/issues/.
Requires GITHUB_TOKEN env var for higher rate limits and comment access.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

OUTPUT_DIR = Path("/codebuild/output/cleaned/issues")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
LOOKBACK_DAYS = 365
MAX_COMMENTS = 10
MIN_BODY_CHARS_UNAUTH = 50
MIN_COMMENT_COUNT_UNAUTH = 1
MIN_COMMENT_COUNT_AUTH = 2
MAX_ISSUES_PER_REPO = 100

LABEL_DENYLIST = {"stale", "wontfix", "duplicate", "invalid", "spam"}

PRIORITY_REPOS = [
    ("hashicorp", "terraform"),
    ("hashicorp", "vault"),
    ("hashicorp", "consul"),
    ("hashicorp", "nomad"),
    ("hashicorp", "packer"),
    ("hashicorp", "terraform-provider-aws"),
    ("hashicorp", "terraform-provider-azurerm"),
    ("hashicorp", "terraform-provider-google"),
]


def _github_headers() -> dict[str, str]:
    """Build request headers, including auth if GITHUB_TOKEN is set."""
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _resolution_quality(issue: dict) -> str:
    """Score the resolution quality of an issue as high/medium/low."""
    if issue.get("state") == "closed" and issue.get("comments", 0) >= 3:
        return "high"
    if issue.get("comments", 0) >= 1:
        return "medium"
    return "low"


def _labels_blocked(issue: dict) -> bool:
    """Return True if any label is in the denylist."""
    for label in issue.get("labels", []):
        if label.get("name", "").lower() in LABEL_DENYLIST:
            return True
    return False


def _html_to_text(html: str) -> str:
    """Strip basic HTML tags, preserving code blocks and links."""
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", html, flags=re.DOTALL)
    text = re.sub(r"<pre>(.*?)</pre>", r"```\n\1\n```", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def fetch_issues(org: str, repo: str) -> list[dict]:
    """Fetch recent issues from a GitHub repository.

    Returns a list of issue metadata dicts filtered for quality.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"https://api.github.com/repos/{org}/{repo}/issues"
    params = {
        "state": "all",
        "since": since,
        "per_page": 100,
        "sort": "updated",
        "direction": "desc",
    }
    headers = _github_headers()
    issues: list[dict] = []

    while url and len(issues) < MAX_ISSUES_PER_REPO:
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            if resp.status_code == 403:
                log.warning("Rate limited on %s/%s — stopping", org, repo)
                break
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning("Request failed for %s/%s: %s", org, repo, exc)
            break

        batch = resp.json()
        params = {}  # Clear params after first request (pagination uses Link header)

        min_comments = MIN_COMMENT_COUNT_AUTH if GITHUB_TOKEN else MIN_COMMENT_COUNT_UNAUTH

        for issue in batch:
            # Skip pull requests
            if "pull_request" in issue:
                continue
            body = issue.get("body") or ""
            if len(body) < MIN_BODY_CHARS_UNAUTH:
                continue
            if issue.get("comments", 0) < min_comments:
                continue
            if _labels_blocked(issue):
                continue
            issues.append(issue)

        # Follow pagination
        link = resp.headers.get("Link", "")
        next_url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                next_url = part.split(";")[0].strip().strip("<>")
        url = next_url or ""

        if len(batch) < 100:
            break

    return issues[:MAX_ISSUES_PER_REPO]


def fetch_comments(org: str, repo: str, issue_number: int) -> list[dict]:
    """Fetch up to MAX_COMMENTS comments for a given issue."""
    url = f"https://api.github.com/repos/{org}/{repo}/issues/{issue_number}/comments"
    headers = _github_headers()
    try:
        resp = requests.get(url, headers=headers, params={"per_page": MAX_COMMENTS}, timeout=30)
        resp.raise_for_status()
        return resp.json()[:MAX_COMMENTS]
    except requests.RequestException as exc:
        log.debug("Could not fetch comments for %s/%s#%d: %s", org, repo, issue_number, exc)
        return []


def format_issue(org: str, repo: str, issue: dict, comments: list[dict]) -> str:
    """Format an issue and its comments into a clean markdown document."""
    number = issue["number"]
    title = issue.get("title", "")
    state = issue.get("state", "")
    body = issue.get("body") or ""
    url = issue.get("html_url", f"https://github.com/{org}/{repo}/issues/{number}")
    updated_at = issue.get("updated_at", "")[:10]

    attribution = f"[issue:{repo}] #{number} ({state}): {title}"
    lines = [attribution, "", f"URL: {url}", ""]
    lines.append(body.strip())

    if comments:
        lines.append("\n## Comments\n")
        for comment in comments:
            author = comment.get("user", {}).get("login", "unknown")
            comment_body = comment.get("body") or ""
            lines.append(f"**{author}:** {comment_body.strip()}\n")

    return "\n".join(lines)


def process_repo(org: str, repo: str) -> int:
    """Fetch and write issues for a single repository.

    Returns the number of files written.
    """
    log.info("Fetching issues from %s/%s...", org, repo)
    issues = fetch_issues(org, repo)
    if not issues:
        log.info("No qualifying issues found in %s/%s", org, repo)
        return 0

    out_dir = OUTPUT_DIR / org / repo
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0

    for issue in issues:
        number = issue["number"]
        comments: list[dict] = []
        if GITHUB_TOKEN:
            comments = fetch_comments(org, repo, number)
            time.sleep(0.1)  # Gentle pacing

        content = format_issue(org, repo, issue, comments)
        resolution_quality = _resolution_quality(issue)
        fname = f"issue_{number}_{resolution_quality}.md"
        (out_dir / fname).write_text(content, encoding="utf-8")
        written += 1

    log.info("Wrote %d issue files for %s/%s", written, org, repo)
    return written


def main() -> None:
    """Fetch issues for all priority repos and write cleaned output."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if not GITHUB_TOKEN:
        log.info("GITHUB_TOKEN not set — using unauthenticated API (60 req/hr limit)")

    total = 0
    for org, repo in PRIORITY_REPOS:
        total += process_repo(org, repo)

    log.info("fetch_github_issues.py complete — %d files written", total)


if __name__ == "__main__":
    main()
