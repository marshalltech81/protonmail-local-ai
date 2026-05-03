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

        # ``is`` and ``a`` are short stop words that would otherwise
        # create false-positive overlap on any subject containing
        # them. Only ``audit`` (a real content token, not in the
        # stopword set) should count, and it appears in c1's
        # subject. (``thread`` was removed from this test when it
        # became part of the long stopword set — it's still
        # implicitly covered by ``test_long_stopwords_are_dropped``.)
        candidates = [
            _candidate("c1", "is a audit"),
            _candidate("c2", "no match"),
        ]
        assert _pick_resolution_candidate("is a audit", candidates).thread_id == "c1"

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

    def test_generic_long_tokens_do_not_outscore_meaningful_match(self):
        from src.tools.intelligence import _pick_resolution_candidate

        # Codex round-3 P2 repro: under length-only filtering, "the
        # payroll thread" gets two generic overlaps with "the lunch
        # thread" (the+thread) and one real overlap with "payroll" —
        # so the generic-overlap candidate would win. The stopword
        # set drops "the" and "thread" so only "payroll" counts and
        # the right thread wins.
        candidates = [
            _candidate("c1", "the lunch thread"),
            _candidate("c2", "payroll"),
        ]
        assert _pick_resolution_candidate("the payroll thread", candidates).thread_id == "c2"

    def test_query_of_only_stopwords_returns_none(self):
        from src.tools.intelligence import _pick_resolution_candidate

        # If every query token is a stop word, the fallback has
        # nothing meaningful to anchor on. Refuse to resolve rather
        # than return whatever the candidate list happens to start
        # with — same intent as the no-overlap gate.
        candidates = [
            _candidate("c1", "anything goes here"),
            _candidate("c2", "another subject"),
        ]
        assert _pick_resolution_candidate("summarize the thread", candidates) is None

    def test_open_enrollment_resolves_to_correct_subject(self):
        from src.tools.intelligence import _pick_resolution_candidate

        # Codex round-4 P3 repro. "open" was previously in the
        # stopword set as an action verb, leaving only "enrollment"
        # in the query — both candidates would tie at 1 overlap and
        # the first would win regardless of which subject the user
        # actually meant. With "open" preserved, the c2 subject
        # carries 2 overlaps ("open" + "enrollment") to c1's 1, and
        # the right candidate wins.
        candidates = [
            _candidate("c1", "benefits enrollment"),
            _candidate("c2", "open enrollment"),
        ]
        assert _pick_resolution_candidate("open enrollment", candidates).thread_id == "c2"

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
        # uppercase short tokens are treated as identifiers. The
        # stopword set also catches ``or`` after lowercasing, so the
        # combined rule rejects either spelling.
        assert _is_meaningful_query_token("Or") is False
        assert _is_meaningful_query_token("An") is False

    def test_long_stopwords_are_dropped(self):
        from src.tools.intelligence import _is_meaningful_query_token

        # Codex round-3 P2: long English stop words ("the", "and",
        # "for", "from") and mailbox-meta nouns ("thread", "email",
        # "message") were previously kept by the length-only rule
        # and produced false-positive overlaps. The stopword set
        # rejects them now.
        for stop in (
            "the",
            "and",
            "for",
            "from",
            "with",
            "what",
            "where",
            "when",
        ):
            assert _is_meaningful_query_token(stop) is False, stop

    def test_mailbox_meta_nouns_are_dropped(self):
        from src.tools.intelligence import _is_meaningful_query_token

        # These nouns are how the user names the data structure they
        # want, not which thread they want. They appear in nearly
        # every prompt AND many subjects, so keeping them produces
        # garbage overlaps. Drop.
        for meta in (
            "thread",
            "threads",
            "email",
            "emails",
            "message",
            "messages",
            "inbox",
            "conversation",
        ):
            assert _is_meaningful_query_token(meta) is False, meta

    def test_unambiguous_action_verbs_are_dropped(self):
        from src.tools.intelligence import _is_meaningful_query_token

        # Imperative verbs the user says to invoke a tool
        # ("summarize the X thread") tell us nothing about which
        # thread — drop them. The set is deliberately narrow:
        # context-dependent verbs that double as content tokens
        # ("open" enrollment, "show" tunes, "list" of attendees,
        # "read" required, "see" attached, "check" deposit) are
        # NOT in the drop set, because erasing them from the query
        # also erases the user's intent.
        for verb in (
            "summarize",
            "find",
            "search",
            "tell",
            "give",
            "fetch",
            "locate",
            "identify",
        ):
            assert _is_meaningful_query_token(verb) is False, verb

    def test_context_dependent_verbs_are_kept(self):
        from src.tools.intelligence import _is_meaningful_query_token

        # Codex round-4 P3 repro: "open enrollment" should resolve
        # to the "open enrollment" thread, not the "benefits
        # enrollment" thread. That requires "open" to remain a
        # query token. Same logic for the other context-dependent
        # verbs in this guard list.
        for verb in ("open", "show", "list", "look", "read", "see", "check", "pull"):
            assert _is_meaningful_query_token(verb) is True, verb

    def test_meaningful_topic_words_are_kept(self):
        from src.tools.intelligence import _is_meaningful_query_token

        # Sanity check: stopword set must not strip the actually-
        # meaningful tokens the eval queries depend on. If this
        # regresses, the stopword set is too broad.
        for topic in (
            "audit",
            "taxes",
            "regency",
            "woods",
            "parking",
            "exception",
            "accountant",
            "schneider",
            "insurance",
            "terrorism",
            "reimbursement",
            "mailing",
            "error",
        ):
            assert _is_meaningful_query_token(topic) is True, topic

    def test_stopword_check_handles_mixed_case_lowercase_only(self):
        from src.tools.intelligence import _is_meaningful_query_token

        # Mixed-case stop words ("And" at sentence start) get
        # lowercased before stopword lookup — caught.
        assert _is_meaningful_query_token("And") is False
        # All-uppercase deliberately bypasses the stopword check
        # because the user likely means an acronym / proper noun
        # (``IT`` department, ``OR`` for operating room or
        # operations research) rather than the English function
        # word that happens to look like its lowercase form.
        assert _is_meaningful_query_token("IT") is True
        assert _is_meaningful_query_token("OR") is True

    def test_uppercase_stopword_lookalikes_are_kept(self):
        from src.tools.intelligence import _is_meaningful_query_token

        # Sanity check on the case bypass: every short word that
        # also has a lowercase stopword counterpart should survive
        # in all-uppercase form. This prevents the stopword set
        # from accidentally clobbering legitimate acronyms.
        for acronym in ("IT", "OR", "AT", "TO", "BE", "DO", "AS"):
            assert _is_meaningful_query_token(acronym) is True, acronym
