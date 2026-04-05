"""Tests for fetch_github_issues.py — filtering, quality scoring, formatting."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from fetch_github_issues import (
    _labels_blocked,
    _resolution_quality,
    format_issue,
)


def _make_issue(**kwargs: object) -> dict:
    """Build a minimal issue dict with defaults."""
    base: dict = {
        "number": 42,
        "title": "Test issue",
        "state": "open",
        "body": "This is the issue body with enough content.",
        "comments": 0,
        "labels": [],
        "html_url": "https://github.com/hashicorp/terraform/issues/42",
        "updated_at": "2024-01-01T00:00:00Z",
    }
    base.update(kwargs)
    return base


def test_resolution_quality_high() -> None:
    """Closed issue with 3+ comments is high quality."""
    issue = _make_issue(state="closed", comments=5)
    assert _resolution_quality(issue) == "high"


def test_resolution_quality_medium() -> None:
    """Open issue with at least 1 comment is medium quality."""
    issue = _make_issue(state="open", comments=2)
    assert _resolution_quality(issue) == "medium"


def test_resolution_quality_low() -> None:
    """Issue with no comments is low quality."""
    issue = _make_issue(state="open", comments=0)
    assert _resolution_quality(issue) == "low"


def test_labels_blocked_stale() -> None:
    """Issue with 'stale' label is blocked."""
    issue = _make_issue(labels=[{"name": "stale"}])
    assert _labels_blocked(issue) is True


def test_labels_blocked_duplicate() -> None:
    """Issue with 'duplicate' label is blocked."""
    issue = _make_issue(labels=[{"name": "duplicate"}])
    assert _labels_blocked(issue) is True


def test_labels_blocked_clear() -> None:
    """Issue with no denylist labels is not blocked."""
    issue = _make_issue(labels=[{"name": "bug"}, {"name": "help wanted"}])
    assert _labels_blocked(issue) is False


def test_format_issue_includes_attribution() -> None:
    """Formatted issue includes the attribution prefix."""
    issue = _make_issue(state="closed", comments=3)
    content = format_issue("hashicorp", "terraform", issue, [])
    assert "[issue:terraform]" in content
    assert "#42" in content
    assert "Test issue" in content


def test_format_issue_with_comments() -> None:
    """Formatted issue includes comment content."""
    issue = _make_issue()
    comments = [{"user": {"login": "alice"}, "body": "This is a comment."}]
    content = format_issue("hashicorp", "terraform", issue, comments)
    assert "alice" in content
    assert "This is a comment." in content
