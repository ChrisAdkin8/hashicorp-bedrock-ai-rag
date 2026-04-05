"""Tests for deduplicate.py — SHA-256 content deduplication."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from deduplicate import MIN_BODY_CHARS, _content_hash, _normalise, deduplicate


def test_normalise_strips_attribution_prefix() -> None:
    """Attribution prefix on the first line is excluded from the normalised body."""
    text = "[provider:aws] aws_instance\n\nThis is the body content."
    result = _normalise(text)
    assert "provider:aws" not in result
    assert "body content" in result


def test_normalise_lowercases_and_collapses_whitespace() -> None:
    """Normalisation lowercases text and collapses whitespace."""
    text = "Hello   World\n\nFoo  Bar"
    result = _normalise(text)
    assert result == result.lower()
    assert "  " not in result


def test_content_hash_deterministic() -> None:
    """The same text always produces the same hash."""
    assert _content_hash("hello") == _content_hash("hello")


def test_content_hash_different_for_different_text() -> None:
    """Different text produces different hashes."""
    assert _content_hash("hello") != _content_hash("world")


def test_deduplicate_removes_duplicate_files(tmp_path: Path) -> None:
    """Duplicate files (by normalised content) are removed; originals kept."""
    body = "x" * MIN_BODY_CHARS * 2
    content_a = f"[provider:aws] aws_instance\n\n{body}"
    content_b = f"[documentation:vault] Some Title\n\n{body}"  # Same body, different prefix

    file_a = tmp_path / "a.md"
    file_b = tmp_path / "b.md"
    file_a.write_text(content_a)
    file_b.write_text(content_b)

    total, removed = deduplicate(tmp_path)
    assert total == 2
    assert removed == 1
    # The earlier file (a.md) should be kept; b.md removed
    assert file_a.exists()
    assert not file_b.exists()


def test_deduplicate_keeps_unique_files(tmp_path: Path) -> None:
    """Unique files are preserved."""
    (tmp_path / "a.md").write_text("[doc:terraform] Title\n\n" + "a" * 200)
    (tmp_path / "b.md").write_text("[doc:vault] Title\n\n" + "b" * 200)

    total, removed = deduplicate(tmp_path)
    assert removed == 0
    assert total == 2


def test_deduplicate_skips_short_files(tmp_path: Path) -> None:
    """Files shorter than MIN_BODY_CHARS are not deduplicated."""
    content = "short"
    (tmp_path / "a.md").write_text(content)
    (tmp_path / "b.md").write_text(content)

    total, removed = deduplicate(tmp_path)
    assert removed == 0
