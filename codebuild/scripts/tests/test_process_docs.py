"""Tests for process_docs.py — semantic section splitting and metadata enrichment."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from process_docs import (
    MIN_SECTION_SIZE,
    _detect_provider_repos,
    _html_to_text,
    _infer_doc_category,
    _make_attribution,
    _split_at_headings,
    _split_large_section,
)


def test_split_at_headings_basic() -> None:
    """Section split produces one tuple per heading."""
    md = "Intro text.\n\n## Section One\n\nBody one.\n\n## Section Two\n\nBody two."
    sections = _split_at_headings(md)
    assert len(sections) == 3  # intro + two sections
    assert sections[1][0] == "Section One"
    assert "Body one." in sections[1][1]


def test_split_at_headings_no_headings() -> None:
    """Document without headings yields one section."""
    md = "Just some text with no headings."
    sections = _split_at_headings(md)
    assert len(sections) == 1
    assert sections[0][0] == ""


def test_split_at_headings_h3() -> None:
    """H3 headings are also split boundaries."""
    md = "### Sub-section\n\nContent here."
    sections = _split_at_headings(md)
    assert sections[0][0] == "Sub-section"


def test_split_large_section_short() -> None:
    """Short sections are returned unchanged."""
    text = "Short text."
    result = _split_large_section(text, max_chars=500)
    assert result == [text]


def test_split_large_section_respects_code_fences() -> None:
    """Large sections split at code-fence boundaries, not mid-fence."""
    # Create a body where the split threshold falls inside a code block
    preamble = "x" * 200
    code_block = "```\n" + "y" * 500 + "\n```"
    text = preamble + "\n" + code_block
    result = _split_large_section(text, max_chars=300)
    # All parts should have balanced backtick fences
    for part in result:
        assert part.count("```") % 2 == 0


def test_make_attribution() -> None:
    """Attribution prefix has the expected format."""
    attr = _make_attribution("provider", "aws", "aws_instance")
    assert attr == "[provider:aws] aws_instance"


def test_infer_doc_category_resource() -> None:
    """Resource reference paths are classified correctly."""
    assert _infer_doc_category(Path("website/docs/r/instance.html.md")) == "resource-reference"


def test_infer_doc_category_data_source() -> None:
    """Data source paths are classified correctly."""
    assert _infer_doc_category(Path("website/docs/d/ami.html.md")) == "data-source-reference"


def test_infer_doc_category_guide() -> None:
    """Guide paths fall back to guide."""
    assert _infer_doc_category(Path("website/docs/guides/getting-started.md")) == "guide"
