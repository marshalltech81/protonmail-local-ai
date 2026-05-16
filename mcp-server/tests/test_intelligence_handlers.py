"""
Tests for the registered handlers in src/tools/intelligence.py.

``test_intelligence.py`` already covers ``_thread_context``. This file
covers the three @server.tool() handlers (``ask_mailbox``,
``summarize_thread``, ``extract_from_emails``) end-to-end against the
seeded DB and the FakeLocalLLM stub. Coverage targets:

- prompt construction wraps every retrieved thread in
  ``<untrusted_email>`` tags and keeps the user task outside them
  (the prompt-injection defense the security notice depends on);
- max_threads / limit clamping forwards a sane bound to the db layer;
- the no-results path returns a sentinel rather than calling the LLM;
- exceptions on db, embed, or completion paths surface as ``Error: ...``
  rather than crashing the tool;
- ``extract_from_emails`` tolerates both single-object and array LLM
  responses and skips invalid JSON without aborting the loop.
"""

import asyncio

from src.tools.intelligence import register_intelligence_tools

from tests.conftest import FakeLocalLLM


def _handlers(fake_server, db, llm):
    register_intelligence_tools(
        fake_server,
        db,
        llm.embed_client,
        llm.inference_client,
    )
    return fake_server.tools


def _text(result) -> str:
    assert len(result) == 1
    return result[0].text


class TestAskMailbox:
    def test_returns_no_results_when_search_is_empty(self, fake_server, seeded_db, fake_llm):
        # FakeLocalLLM returns a fixed embedding that vector-matches a
        # seeded thread, so hybrid_search would never return zero rows
        # against ``seeded_db``. Force the empty-result path explicitly
        # so the no-results sentinel branch is exercised.
        seeded_db.hybrid_search = lambda **_kw: []  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db, fake_llm)["ask_mailbox"]
        out = asyncio.run(handler(question="zxq nothing matches"))
        assert "No relevant emails" in _text(out)
        # The completion call must NOT have happened — there was nothing
        # to ground an answer in.
        assert fake_llm.complete_calls == []

    def test_wraps_each_thread_in_untrusted_tags(self, fake_server, seeded_db, fake_llm):
        handler = _handlers(fake_server, seeded_db, fake_llm)["ask_mailbox"]
        asyncio.run(handler(question="invoice"))
        # The user prompt is the second element of the (system, user) tuple.
        assert fake_llm.complete_calls
        _system, user = fake_llm.complete_calls[0]
        assert "<untrusted_email" in user
        assert "</untrusted_email>" in user
        # User's question must be OUTSIDE the tags so the model's only
        # trusted instruction comes from the user, not the email body.
        post_tag = user.split("</untrusted_email>")[-1]
        assert "User's question" in post_tag

    def test_includes_sources_block_in_response(self, fake_server, seeded_db, fake_llm):
        handler = _handlers(fake_server, seeded_db, fake_llm)["ask_mailbox"]
        out = asyncio.run(handler(question="invoice"))
        text = _text(out)
        assert "Sources searched" in text

    def test_max_threads_is_clamped(self, fake_server, seeded_db, fake_llm):
        seen: dict = {}
        original = seeded_db.hybrid_search

        def spy(**kwargs):
            seen["limit"] = kwargs.get("limit")
            return original(**kwargs)

        seeded_db.hybrid_search = spy  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db, fake_llm)["ask_mailbox"]
        asyncio.run(handler(question="invoice", max_threads=10_000))
        assert seen["limit"] == 10  # _MAX_ASK_THREADS

    def test_db_exception_returns_error_text(self, fake_server, seeded_db, fake_llm):
        def boom(**_kwargs):
            raise RuntimeError("simulated db failure")

        seeded_db.hybrid_search = boom  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db, fake_llm)["ask_mailbox"]
        out = asyncio.run(handler(question="anything"))
        assert "Error" in _text(out)

    def test_secret_values_are_scrubbed_from_exception_text(self, fake_server, seeded_db, fake_llm):
        # Pin the main.py wiring of secret_values into the intelligence
        # registrar — a provider SDK exception that quotes the operator's
        # API key (e.g. an auth-header echo in the error body) must not
        # leak to the caller via the user-visible Error: response.
        leaked_key = "sk-leakedXYZ789"  # pragma: allowlist secret

        def boom(**_kwargs):
            raise RuntimeError(f"upstream auth: Bearer {leaked_key}")

        seeded_db.hybrid_search = boom  # type: ignore[assignment]
        register_intelligence_tools(
            fake_server,
            seeded_db,
            fake_llm.embed_client,
            fake_llm.inference_client,
            secret_values=[leaked_key],
        )
        handler = fake_server.tools["ask_mailbox"]
        out = asyncio.run(handler(question="anything"))
        text = _text(out)
        assert leaked_key not in text
        assert "[REDACTED]" in text


class TestSummarizeThread:
    def test_known_thread_calls_llm_with_untrusted_wrapper(self, fake_server, seeded_db, fake_llm):
        handler = _handlers(fake_server, seeded_db, fake_llm)["summarize_thread"]
        out = asyncio.run(handler(thread_id="t-alpha", style="brief"))
        assert "Summary (brief)" in _text(out)
        _system, user = fake_llm.complete_calls[0]
        assert "<untrusted_email>" in user
        assert "</untrusted_email>" in user

    def test_known_opaque_id_does_not_trigger_fallback_search(
        self, fake_server, seeded_db, fake_llm
    ):
        # A direct hit on ``get_thread`` must not call ``embed`` —
        # otherwise the fallback path would run on every successful
        # lookup, doubling latency. Embedding is only done when the
        # opaque lookup misses.
        handler = _handlers(fake_server, seeded_db, fake_llm)["summarize_thread"]
        asyncio.run(handler(thread_id="t-alpha"))
        assert fake_llm.embed_calls == []

    def test_unknown_id_falls_back_to_phrase_search(self, fake_server, seeded_db, fake_llm):
        # The fallback rescues calls where the model passed a phrase
        # rather than an opaque ID. The fake llm returns the canned
        # embedding ``[1, 0, 0, 0]`` which aligns with t-alpha's
        # vector, so hybrid_search resolves to t-alpha and the summary
        # is produced for that thread instead of "Thread not found".
        handler = _handlers(fake_server, seeded_db, fake_llm)["summarize_thread"]
        out = asyncio.run(handler(thread_id="invoice for march"))
        text = _text(out)
        assert "Thread not found" not in text
        # Subject should appear in the summary header so the caller
        # sees which thread the fallback resolved to.
        assert "invoice for march" in text
        # The phrase was embedded once for the fallback search.
        assert fake_llm.embed_calls == ["invoice for march"]

    def test_phrase_fallback_prefers_subject_overlap_over_vector_rank(
        self, fake_server, seeded_db, fake_llm
    ):
        # ``fake_llm`` returns a fixed [1, 0, 0, 0] embedding which
        # vector-aligns with t-alpha (subject "invoice for march"). For
        # the phrase "lunch plans", the keyword lane surfaces t-beta
        # (its actual subject) but the vector lane keeps pulling t-alpha
        # to the top. Without the subject-overlap tiebreaker the
        # fallback resolves to t-alpha and produces a summary about the
        # wrong thread; with the tiebreaker, t-beta wins on the two
        # subject-token overlaps.
        handler = _handlers(fake_server, seeded_db, fake_llm)["summarize_thread"]
        out = asyncio.run(handler(thread_id="lunch plans"))
        text = _text(out)
        # Must resolve to the lunch thread, not the invoice thread.
        assert "lunch plans" in text
        assert "invoice for march" not in text

    def test_phrase_fallback_with_no_subject_overlap_returns_not_found(
        self, fake_server, seeded_db, fake_llm
    ):
        # The fallback gate refuses to resolve when no candidate's
        # subject shares a token with the query. seeded_db contains
        # subjects "invoice for march", "lunch plans", "meeting notes
        # archive"; the query "zzznosuchsubject" overlaps with none.
        # Without this gate, vector KNN would rank t-alpha (matching
        # the canned [1,0,0,0] embedding) and the summary would land
        # on the invoice thread — silently wrong. Now we surface
        # "Thread not found" instead.
        handler = _handlers(fake_server, seeded_db, fake_llm)["summarize_thread"]
        out = asyncio.run(handler(thread_id="zzznosuchsubject"))
        text = _text(out)
        assert "Thread not found" in text
        # And critically: no LLM was invoked, because we never
        # resolved a thread to summarize.
        assert fake_llm.complete_calls == []

    def test_phrase_with_empty_corpus_returns_not_found(self, fake_server, empty_db, fake_llm):
        # If the index is empty, the fallback hybrid_search returns no
        # hits — return the original sentinel rather than fabricating a
        # summary. ``empty_db`` carries the schema but no rows, so this
        # exercises the fallback's miss branch cleanly.
        handler = _handlers(fake_server, empty_db, fake_llm)["summarize_thread"]
        out = asyncio.run(handler(thread_id="anything"))
        assert "Thread not found" in _text(out)
        # No LLM completion call when the fallback finds nothing.
        assert fake_llm.complete_calls == []

    def test_unknown_style_falls_back_to_brief(self, fake_server, seeded_db, fake_llm):
        handler = _handlers(fake_server, seeded_db, fake_llm)["summarize_thread"]
        asyncio.run(handler(thread_id="t-alpha", style="completely-made-up-style"))
        _system, user = fake_llm.complete_calls[0]
        # ``brief`` instruction should be embedded in the user prompt.
        assert "2-3 sentences" in user

    def test_db_exception_returns_error_text(self, fake_server, seeded_db, fake_llm):
        def boom(_thread_id):
            raise RuntimeError("simulated read failure")

        seeded_db.get_thread = boom  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db, fake_llm)["summarize_thread"]
        out = asyncio.run(handler(thread_id="t-alpha"))
        assert "Error" in _text(out)

    def test_recent_chunks_supplement_body_text(self, fake_server, chunked_db, fake_llm):
        """Codex P1: the recent-chunk tail is *appended* to ``body_text``,
        not substituted for it. ``body_text`` is front-preserved, so a
        ``"detailed"`` summary must see BOTH the start of the thread
        (body) AND its latest activity (chunk tail). ``chunked_db.t-alpha``
        has a chunk whose text is NOT in its ``body_text`` — both must
        reach the prompt."""
        handler = _handlers(fake_server, chunked_db, fake_llm)["summarize_thread"]
        asyncio.run(handler(thread_id="t-alpha", style="detailed"))
        assert fake_llm.complete_calls
        _system, user = fake_llm.complete_calls[0]
        # Earlier body context is preserved...
        assert "please find the invoice attached for march" in user
        # ...and the recent-chunk tail (text only in message_chunks) is
        # appended under its section header in chunk-rendered form.
        assert "invoice number 12345" in user
        assert "--- recent messages ---" in user
        assert "[chunk " in user

    def test_chunkless_thread_still_uses_body_text(self, fake_server, chunked_db, fake_llm):
        """Regression guard: a thread with NO chunks (t-gamma) must
        continue to fall back to body_text so the summarize path works
        for empty-body or extraction-failure threads."""
        handler = _handlers(fake_server, chunked_db, fake_llm)["summarize_thread"]
        asyncio.run(handler(thread_id="t-gamma"))
        assert fake_llm.complete_calls
        _system, user = fake_llm.complete_calls[0]
        # The thread's body_text reaches the prompt because there are
        # no chunks to override it.
        assert "notes from the planning meeting" in user


class TestExtractFromEmails:
    def test_returns_no_match_message_when_search_is_empty(self, fake_server, seeded_db, fake_llm):
        # See the equivalent ask_mailbox test — fixed-embedding fakes
        # always vector-match, so the empty path is forced explicitly.
        seeded_db.hybrid_search = lambda **_kw: []  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db, fake_llm)["extract_from_emails"]
        out = asyncio.run(handler(query="zxq nothing matches", schema={"vendor": "string"}))
        assert "No matching emails" in _text(out)

    def test_limit_is_clamped(self, fake_server, seeded_db, fake_llm):
        seen: dict = {}
        original = seeded_db.hybrid_search

        def spy(**kwargs):
            seen["limit"] = kwargs.get("limit")
            return original(**kwargs)

        seeded_db.hybrid_search = spy  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db, fake_llm)["extract_from_emails"]
        asyncio.run(handler(query="invoice", schema={"vendor": "string"}, limit=10_000))
        assert seen["limit"] == 50  # _MAX_EXTRACT_LIMIT

    def test_returns_no_records_when_llm_only_returns_invalid_json(self, fake_server, seeded_db):
        llm = FakeLocalLLM(complete_responses=["not json", "still not json", "nope"])
        handler = _handlers(fake_server, seeded_db, llm)["extract_from_emails"]
        out = asyncio.run(handler(query="invoice OR lunch OR meeting", schema={"vendor": "string"}))
        # Every per-thread JSON parse fails → the loop logs nothing and
        # falls into the no-records-found message.
        assert "No structured data" in _text(out)

    def test_accepts_object_response_and_annotates_with_source(self, fake_server, seeded_db):
        llm = FakeLocalLLM(complete_responses=['{"vendor": "Acme"}'] * 3)
        handler = _handlers(fake_server, seeded_db, llm)["extract_from_emails"]
        out = asyncio.run(
            handler(
                query="invoice OR lunch OR meeting",
                schema={"vendor": "string"},
            )
        )
        text = _text(out)
        assert "Acme" in text
        # Annotation fields ensure the LLM's extracted record is
        # traceable back to a thread — dropping these would make
        # extraction outputs non-auditable.
        assert "_source_thread" in text
        assert "_date" in text

    def test_accepts_array_response_per_thread(self, fake_server, seeded_db):
        # Same prompt, but the model returned an array — the previous
        # behavior aborted with TypeError. Now each item should be
        # accepted and annotated independently.
        llm = FakeLocalLLM(
            complete_responses=[
                '[{"vendor": "Acme"}, {"vendor": "Beta"}]',
                "null",
                "null",
            ]
        )
        handler = _handlers(fake_server, seeded_db, llm)["extract_from_emails"]
        out = asyncio.run(
            handler(
                query="invoice OR lunch OR meeting",
                schema={"vendor": "string"},
            )
        )
        text = _text(out)
        assert "Acme" in text
        assert "Beta" in text

    def test_db_exception_returns_error_text(self, fake_server, seeded_db, fake_llm):
        def boom(**_kwargs):
            raise RuntimeError("simulated read failure")

        seeded_db.hybrid_search = boom  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db, fake_llm)["extract_from_emails"]
        out = asyncio.run(handler(query="invoice", schema={"x": "string"}))
        assert "Error" in _text(out)


class TestInferenceDispatch:
    def test_inference_client_is_invoked_for_ask_mailbox(self, fake_server, seeded_db, fake_llm):
        # Intelligence tools delegate to the inference client without
        # branching by mode — the client itself encapsulates the
        # protocol/SDK choice. Validation that a misconfigured mode
        # surfaces at startup (no fallback) lives in test_main.py.
        handler = _handlers(fake_server, seeded_db, fake_llm)["ask_mailbox"]
        asyncio.run(handler(question="invoice"))
        assert fake_llm.complete_calls
