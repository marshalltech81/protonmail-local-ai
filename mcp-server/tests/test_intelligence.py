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


def _candidate(thread_id: str, subject: str) -> ThreadResult:
    return ThreadResult(
        thread_id=thread_id,
        subject=subject,
        participants=[],
        folder="INBOX",
        date_first=datetime(2024, 1, 1, tzinfo=UTC),
        date_last=datetime(2024, 1, 1, tzinfo=UTC),
        message_ids=[],
        snippet="",
        has_attachments=False,
    )


class TestPickResolutionCandidate:
    """The subject-overlap tiebreaker for summarize_thread's phrase fallback.

    Each test pins a specific behavior the user-visible UX depends on. The
    function operates on hybrid_search top-N candidates, so the input list
    is already in RRF rank order — the tiebreaker only re-ranks when the
    top-ranked candidate isn't the obvious subject match.
    """

    def test_returns_none_when_no_subject_overlap(self):
        from src.tools.intelligence import _pick_resolution_candidate

        # ``zzz`` shares no tokens with either subject. The fallback
        # MUST refuse to summarize: vector KNN always returns a
        # nearest neighbor in any non-empty mailbox, so without this
        # gate a typo'd opaque ID or an unrelated phrase would
        # produce a confident summary of an irrelevant thread. The
        # caller treats None as "could not confidently resolve" and
        # surfaces ``Thread not found``.
        candidates = [
            _candidate("c1", "alpha beta"),
            _candidate("c2", "gamma delta"),
        ]
        assert _pick_resolution_candidate("zzz", candidates) is None

    def test_picks_higher_subject_overlap_over_top_rank(self):
        from src.tools.intelligence import _pick_resolution_candidate

        # ``c1`` ranks first via RRF but shares no tokens with the query;
        # ``c2`` shares two — the tiebreaker must override the rank.
        candidates = [
            _candidate("c1", "completely different"),
            _candidate("c2", "exact match here"),
        ]
        assert _pick_resolution_candidate("exact match", candidates).thread_id == "c2"

    def test_short_query_tokens_are_ignored_to_avoid_stop_words(self):
        from src.tools.intelligence import _pick_resolution_candidate

        # ``is`` and ``a`` would otherwise create a 2-token overlap on c1
        # for any subject containing those stop-words. Only ``thread``
        # should count, and it appears in c1's subject.
        candidates = [
            _candidate("c1", "is a thread"),
            _candidate("c2", "no match"),
        ]
        assert _pick_resolution_candidate("is a thread", candidates).thread_id == "c1"

    def test_match_is_case_insensitive(self):
        from src.tools.intelligence import _pick_resolution_candidate

        candidates = [
            _candidate("c1", "AUDIT and TAXES"),
            _candidate("c2", "lunch"),
        ]
        assert _pick_resolution_candidate("audit", candidates).thread_id == "c1"

    def test_empty_query_returns_none(self):
        from src.tools.intelligence import _pick_resolution_candidate

        # A whitespace-only or punctuation-only query yields zero
        # query tokens after filtering — there's nothing to overlap
        # against, so the gate refuses to resolve. Caller surfaces
        # ``Thread not found``.
        candidates = [
            _candidate("c1", "alpha"),
            _candidate("c2", "beta"),
        ]
        assert _pick_resolution_candidate("   ", candidates) is None

    def test_raises_on_empty_candidates(self):
        import pytest
        from src.tools.intelligence import _pick_resolution_candidate

        with pytest.raises(ValueError):
            _pick_resolution_candidate("anything", [])
