"""Tests for src/tools/intelligence.py pure helpers.

Tool handlers themselves are covered by integration wiring; this file
targets ``_thread_context``, the pure function that selects between
``body_text`` and ``snippet`` and enforces the per-thread character
budget fed into LLM prompts.
"""

from datetime import UTC, datetime

from src.lib.sqlite import ThreadResult
from src.tools.intelligence import PER_THREAD_CHAR_BUDGET, _thread_context


def _result(body_text: str = "", snippet: str = "") -> ThreadResult:
    return ThreadResult(
        thread_id="t",
        subject="s",
        participants=[],
        folder="INBOX",
        date_first=datetime(2024, 1, 1, tzinfo=UTC),
        date_last=datetime(2024, 1, 1, tzinfo=UTC),
        message_ids=[],
        snippet=snippet,
        has_attachments=False,
        body_text=body_text,
    )


class TestThreadContext:
    def test_prefers_body_text_over_snippet(self):
        r = _result(body_text="accumulated thread body", snippet="short preview")
        assert _thread_context(r) == "accumulated thread body"

    def test_falls_back_to_snippet_when_body_text_missing(self):
        r = _result(body_text="", snippet="short preview")
        assert _thread_context(r) == "short preview"

    def test_returns_empty_string_when_both_empty(self):
        r = _result()
        assert _thread_context(r) == ""

    def test_bounded_by_per_thread_budget(self):
        r = _result(body_text="x" * (PER_THREAD_CHAR_BUDGET * 2))
        assert len(_thread_context(r)) == PER_THREAD_CHAR_BUDGET

    def test_custom_limit_honored(self):
        r = _result(body_text="x" * 500)
        assert len(_thread_context(r, limit=100)) == 100
