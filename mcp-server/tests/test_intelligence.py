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

    def test_short_uppercase_token_is_kept(self):
        from src.tools.intelligence import _pick_resolution_candidate

        # ``HR`` is 2 chars but all-upper — clearly a meaningful
        # identifier (department name, acronym), not a stop word.
        # Without this exception the fallback would refuse to resolve
        # "summarize the HR onboarding thread" even when "HR" is in
        # the subject.
        candidates = [
            _candidate("c1", "HR onboarding 2025"),
            _candidate("c2", "lunch plans"),
        ]
        assert _pick_resolution_candidate("HR onboarding", candidates).thread_id == "c1"

    def test_short_digit_bearing_token_is_kept(self):
        from src.tools.intelligence import _pick_resolution_candidate

        # ``Q1`` and ``W2`` are 2 chars but contain digits — keep
        # them so phrase fallbacks like "summarize the Q1 audit" or
        # "find the W2 thread" still resolve.
        candidates = [
            _candidate("c1", "Q1 audit recap"),
            _candidate("c2", "lunch plans"),
        ]
        assert _pick_resolution_candidate("Q1 audit", candidates).thread_id == "c1"

    def test_short_lowercase_stopword_is_still_dropped(self):
        from src.tools.intelligence import _pick_resolution_candidate

        # The identifier-shape exception above should NOT regress the
        # original stop-word filter. ``of`` (lowercase, no digits)
        # remains a stop word and must not produce overlap.
        candidates = [
            _candidate("c1", "list of plans"),  # contains "of"
            _candidate("c2", "no match"),
        ]
        # Query is "of" alone — only token, all-lowercase, length 2:
        # filter drops it, leaving zero query tokens, so the fallback
        # returns None (correct: refuse to resolve a stop-word query).
        assert _pick_resolution_candidate("of", candidates) is None

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


class TestIsMeaningfulQueryToken:
    """Pin the identifier-vs-stopword discriminator independently of the
    candidate-picking logic. Keeps the rule reviewable in one place so a
    future tweak (e.g. adding a known-stopword set) doesn't have to be
    inferred from integration test failures.
    """

    def test_long_token_is_kept(self):
        from src.tools.intelligence import _is_meaningful_query_token

        assert _is_meaningful_query_token("audit") is True

    def test_short_lowercase_stopword_is_dropped(self):
        from src.tools.intelligence import _is_meaningful_query_token

        for stop in ("is", "of", "an", "to", "at", "in", "on", "or"):
            assert _is_meaningful_query_token(stop) is False, stop

    def test_short_uppercase_acronym_is_kept(self):
        from src.tools.intelligence import _is_meaningful_query_token

        for acronym in ("HR", "AI", "IT", "PR", "QA"):
            assert _is_meaningful_query_token(acronym) is True, acronym

    def test_short_digit_bearing_token_is_kept(self):
        from src.tools.intelligence import _is_meaningful_query_token

        for ident in ("Q1", "W2", "5G", "K9", "h1"):
            assert _is_meaningful_query_token(ident) is True, ident

    def test_empty_string_is_dropped(self):
        from src.tools.intelligence import _is_meaningful_query_token

        # ``re.findall(r"\w+", ...)`` won't normally produce an empty
        # match, but defensively the helper should not raise on empty
        # input — the rule "kept iff identifier-shaped" cleanly says
        # no for an empty string.
        assert _is_meaningful_query_token("") is False

    def test_mixed_case_short_is_dropped(self):
        from src.tools.intelligence import _is_meaningful_query_token

        # ``Or`` at sentence start is still a stop word; only fully
        # uppercase short tokens are treated as identifiers. This is
        # a deliberate trade-off to keep the rule simple — adding a
        # stopword set would catch the edge cases more cleanly but
        # introduces a second knob to maintain.
        assert _is_meaningful_query_token("Or") is False
        assert _is_meaningful_query_token("An") is False
