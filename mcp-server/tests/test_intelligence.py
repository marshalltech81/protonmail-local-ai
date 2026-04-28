"""Tests for src/tools/intelligence.py pure helpers.

Tool handlers themselves are covered by integration wiring; this file
targets ``_thread_context``, the pure function that selects between
``body_text`` and ``snippet`` and enforces the per-thread character
budget fed into LLM prompts.
"""

from datetime import UTC, datetime

from src.lib.sqlite import ChunkResult, ThreadResult
from src.tools.intelligence import PER_THREAD_CHAR_BUDGET, _thread_context


def _result(
    body_text: str = "",
    snippet: str = "",
    evidence_chunks: list[ChunkResult] | None = None,
) -> ThreadResult:
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
        evidence_chunks=evidence_chunks if evidence_chunks is not None else [],
    )


def _chunk(text: str, index: int = 0, char_start: int = 0) -> ChunkResult:
    return ChunkResult(
        chunk_id=f"c{index}",
        message_id="m1",
        thread_id="t",
        chunk_index=index,
        text=text,
        char_start=char_start,
        char_end=char_start + len(text),
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


class TestThreadContextWithChunks:
    def test_chunks_render_with_provenance_header(self):
        r = _result(
            body_text="full thread body that should not be returned",
            evidence_chunks=[_chunk("the precise passage", index=0, char_start=42)],
        )
        out = _thread_context(r)
        assert "[chunk 0 chars 42-61]" in out
        assert "the precise passage" in out
        # Body text is suppressed when chunks are present.
        assert "full thread body" not in out

    def test_multiple_chunks_concatenated_with_separator(self):
        r = _result(
            evidence_chunks=[
                _chunk("first matched passage", index=0, char_start=0),
                _chunk("second matched passage", index=1, char_start=200),
            ],
        )
        out = _thread_context(r)
        assert "first matched passage" in out
        assert "second matched passage" in out
        assert "[chunk 0" in out and "[chunk 1" in out

    def test_chunks_truncated_to_per_thread_budget(self):
        big = "y" * (PER_THREAD_CHAR_BUDGET * 2)
        r = _result(evidence_chunks=[_chunk(big, index=0)])
        out = _thread_context(r, limit=200)
        # The header + truncated chunk fits inside the requested budget.
        assert len(out) <= 200

    def test_no_evidence_chunks_falls_back_to_body_text(self):
        r = _result(body_text="legacy thread body")
        assert _thread_context(r) == "legacy thread body"
