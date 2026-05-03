"""
Tests for the registered handlers in src/tools/intelligence.py.

``test_intelligence.py`` already covers ``_thread_context``. This file
covers the three @server.tool() handlers (``ask_mailbox``,
``summarize_thread``, ``extract_from_emails``) end-to-end against the
seeded DB and the FakeOllama stub. Coverage targets:

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

from tests.conftest import FakeOllama


def _handlers(fake_server, db, ollama, *, llm_mode="local", api_key="", model="claude-x"):
    register_intelligence_tools(fake_server, db, ollama, llm_mode, api_key, model)
    return fake_server.tools


def _text(result) -> str:
    assert len(result) == 1
    return result[0].text


class TestAskMailbox:
    def test_returns_no_results_when_search_is_empty(self, fake_server, seeded_db, fake_ollama):
        # FakeOllama returns a fixed embedding that vector-matches a
        # seeded thread, so hybrid_search would never return zero rows
        # against ``seeded_db``. Force the empty-result path explicitly
        # so the no-results sentinel branch is exercised.
        seeded_db.hybrid_search = lambda **_kw: []  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db, fake_ollama)["ask_mailbox"]
        out = asyncio.run(handler(question="zxq nothing matches"))
        assert "No relevant emails" in _text(out)
        # The completion call must NOT have happened — there was nothing
        # to ground an answer in.
        assert fake_ollama.complete_calls == []

    def test_wraps_each_thread_in_untrusted_tags(self, fake_server, seeded_db, fake_ollama):
        handler = _handlers(fake_server, seeded_db, fake_ollama)["ask_mailbox"]
        asyncio.run(handler(question="invoice"))
        # The user prompt is the second element of the (system, user) tuple.
        assert fake_ollama.complete_calls
        _system, user = fake_ollama.complete_calls[0]
        assert "<untrusted_email" in user
        assert "</untrusted_email>" in user
        # User's question must be OUTSIDE the tags so the model's only
        # trusted instruction comes from the user, not the email body.
        post_tag = user.split("</untrusted_email>")[-1]
        assert "User's question" in post_tag

    def test_includes_sources_block_in_response(self, fake_server, seeded_db, fake_ollama):
        handler = _handlers(fake_server, seeded_db, fake_ollama)["ask_mailbox"]
        out = asyncio.run(handler(question="invoice"))
        text = _text(out)
        assert "Sources searched" in text

    def test_max_threads_is_clamped(self, fake_server, seeded_db, fake_ollama):
        seen: dict = {}
        original = seeded_db.hybrid_search

        def spy(**kwargs):
            seen["limit"] = kwargs.get("limit")
            return original(**kwargs)

        seeded_db.hybrid_search = spy  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db, fake_ollama)["ask_mailbox"]
        asyncio.run(handler(question="invoice", max_threads=10_000))
        assert seen["limit"] == 10  # _MAX_ASK_THREADS

    def test_db_exception_returns_error_text(self, fake_server, seeded_db, fake_ollama):
        def boom(**_kwargs):
            raise RuntimeError("simulated db failure")

        seeded_db.hybrid_search = boom  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db, fake_ollama)["ask_mailbox"]
        out = asyncio.run(handler(question="anything"))
        assert "Error" in _text(out)


class TestSummarizeThread:
    def test_known_thread_calls_llm_with_untrusted_wrapper(
        self, fake_server, seeded_db, fake_ollama
    ):
        handler = _handlers(fake_server, seeded_db, fake_ollama)["summarize_thread"]
        out = asyncio.run(handler(thread_id="t-alpha", style="brief"))
        assert "Summary (brief)" in _text(out)
        _system, user = fake_ollama.complete_calls[0]
        assert "<untrusted_email>" in user
        assert "</untrusted_email>" in user

    def test_known_opaque_id_does_not_trigger_fallback_search(
        self, fake_server, seeded_db, fake_ollama
    ):
        # A direct hit on ``get_thread`` must not call ``embed`` —
        # otherwise the fallback path would run on every successful
        # lookup, doubling latency. Embedding is only done when the
        # opaque lookup misses.
        handler = _handlers(fake_server, seeded_db, fake_ollama)["summarize_thread"]
        asyncio.run(handler(thread_id="t-alpha"))
        assert fake_ollama.embed_calls == []

    def test_unknown_id_falls_back_to_phrase_search(self, fake_server, seeded_db, fake_ollama):
        # The fallback rescues calls where the model passed a phrase
        # rather than an opaque ID. The fake ollama returns the canned
        # embedding ``[1, 0, 0, 0]`` which aligns with t-alpha's
        # vector, so hybrid_search resolves to t-alpha and the summary
        # is produced for that thread instead of "Thread not found".
        handler = _handlers(fake_server, seeded_db, fake_ollama)["summarize_thread"]
        out = asyncio.run(handler(thread_id="invoice for march"))
        text = _text(out)
        assert "Thread not found" not in text
        # Subject should appear in the summary header so the caller
        # sees which thread the fallback resolved to.
        assert "invoice for march" in text
        # The phrase was embedded once for the fallback search.
        assert fake_ollama.embed_calls == ["invoice for march"]

    def test_phrase_fallback_prefers_subject_overlap_over_vector_rank(
        self, fake_server, seeded_db, fake_ollama
    ):
        # ``fake_ollama`` returns a fixed [1, 0, 0, 0] embedding which
        # vector-aligns with t-alpha (subject "invoice for march"). For
        # the phrase "lunch plans", the keyword lane surfaces t-beta
        # (its actual subject) but the vector lane keeps pulling t-alpha
        # to the top. Without the subject-overlap tiebreaker the
        # fallback resolves to t-alpha and produces a summary about the
        # wrong thread; with the tiebreaker, t-beta wins on the two
        # subject-token overlaps.
        handler = _handlers(fake_server, seeded_db, fake_ollama)["summarize_thread"]
        out = asyncio.run(handler(thread_id="lunch plans"))
        text = _text(out)
        # Must resolve to the lunch thread, not the invoice thread.
        assert "lunch plans" in text
        assert "invoice for march" not in text

    def test_phrase_fallback_with_no_subject_overlap_returns_not_found(
        self, fake_server, seeded_db, fake_ollama
    ):
        # The fallback gate refuses to resolve when no candidate's
        # subject shares a token with the query. seeded_db contains
        # subjects "invoice for march", "lunch plans", "meeting notes
        # archive"; the query "zzznosuchsubject" overlaps with none.
        # Without this gate, vector KNN would rank t-alpha (matching
        # the canned [1,0,0,0] embedding) and the summary would land
        # on the invoice thread — silently wrong. Now we surface
        # "Thread not found" instead.
        handler = _handlers(fake_server, seeded_db, fake_ollama)["summarize_thread"]
        out = asyncio.run(handler(thread_id="zzznosuchsubject"))
        text = _text(out)
        assert "Thread not found" in text
        # And critically: no LLM was invoked, because we never
        # resolved a thread to summarize.
        assert fake_ollama.complete_calls == []

    def test_phrase_with_empty_corpus_returns_not_found(self, fake_server, empty_db, fake_ollama):
        # If the index is empty, the fallback hybrid_search returns no
        # hits — return the original sentinel rather than fabricating a
        # summary. ``empty_db`` carries the schema but no rows, so this
        # exercises the fallback's miss branch cleanly.
        handler = _handlers(fake_server, empty_db, fake_ollama)["summarize_thread"]
        out = asyncio.run(handler(thread_id="anything"))
        assert "Thread not found" in _text(out)
        # No LLM completion call when the fallback finds nothing.
        assert fake_ollama.complete_calls == []

    def test_unknown_style_falls_back_to_brief(self, fake_server, seeded_db, fake_ollama):
        handler = _handlers(fake_server, seeded_db, fake_ollama)["summarize_thread"]
        asyncio.run(handler(thread_id="t-alpha", style="completely-made-up-style"))
        _system, user = fake_ollama.complete_calls[0]
        # ``brief`` instruction should be embedded in the user prompt.
        assert "2-3 sentences" in user

    def test_db_exception_returns_error_text(self, fake_server, seeded_db, fake_ollama):
        def boom(_thread_id):
            raise RuntimeError("simulated read failure")

        seeded_db.get_thread = boom  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db, fake_ollama)["summarize_thread"]
        out = asyncio.run(handler(thread_id="t-alpha"))
        assert "Error" in _text(out)


class TestExtractFromEmails:
    def test_returns_no_match_message_when_search_is_empty(
        self, fake_server, seeded_db, fake_ollama
    ):
        # See the equivalent ask_mailbox test — fixed-embedding fakes
        # always vector-match, so the empty path is forced explicitly.
        seeded_db.hybrid_search = lambda **_kw: []  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db, fake_ollama)["extract_from_emails"]
        out = asyncio.run(handler(query="zxq nothing matches", schema={"vendor": "string"}))
        assert "No matching emails" in _text(out)

    def test_limit_is_clamped(self, fake_server, seeded_db, fake_ollama):
        seen: dict = {}
        original = seeded_db.hybrid_search

        def spy(**kwargs):
            seen["limit"] = kwargs.get("limit")
            return original(**kwargs)

        seeded_db.hybrid_search = spy  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db, fake_ollama)["extract_from_emails"]
        asyncio.run(handler(query="invoice", schema={"vendor": "string"}, limit=10_000))
        assert seen["limit"] == 50  # _MAX_EXTRACT_LIMIT

    def test_returns_no_records_when_llm_only_returns_invalid_json(self, fake_server, seeded_db):
        ollama = FakeOllama(complete_responses=["not json", "still not json", "nope"])
        handler = _handlers(fake_server, seeded_db, ollama)["extract_from_emails"]
        out = asyncio.run(handler(query="invoice OR lunch OR meeting", schema={"vendor": "string"}))
        # Every per-thread JSON parse fails → the loop logs nothing and
        # falls into the no-records-found message.
        assert "No structured data" in _text(out)

    def test_accepts_object_response_and_annotates_with_source(self, fake_server, seeded_db):
        ollama = FakeOllama(complete_responses=['{"vendor": "Acme"}'] * 3)
        handler = _handlers(fake_server, seeded_db, ollama)["extract_from_emails"]
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
        ollama = FakeOllama(
            complete_responses=[
                '[{"vendor": "Acme"}, {"vendor": "Beta"}]',
                "null",
                "null",
            ]
        )
        handler = _handlers(fake_server, seeded_db, ollama)["extract_from_emails"]
        out = asyncio.run(
            handler(
                query="invoice OR lunch OR meeting",
                schema={"vendor": "string"},
            )
        )
        text = _text(out)
        assert "Acme" in text
        assert "Beta" in text

    def test_db_exception_returns_error_text(self, fake_server, seeded_db, fake_ollama):
        def boom(**_kwargs):
            raise RuntimeError("simulated read failure")

        seeded_db.hybrid_search = boom  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db, fake_ollama)["extract_from_emails"]
        out = asyncio.run(handler(query="invoice", schema={"x": "string"}))
        assert "Error" in _text(out)


class TestLLMRouting:
    def test_local_mode_routes_to_ollama_complete(self, fake_server, seeded_db, fake_ollama):
        handler = _handlers(fake_server, seeded_db, fake_ollama, llm_mode="local")["ask_mailbox"]
        asyncio.run(handler(question="invoice"))
        # Ollama.complete must have been called even though llm_mode=local
        # — the local path is the documented default for the ``LLM_MODE``
        # env var.
        assert fake_ollama.complete_calls

    def test_cloud_mode_without_api_key_falls_back_to_ollama(
        self, fake_server, seeded_db, fake_ollama
    ):
        # cloud + empty key must not call Anthropic; instead it falls
        # through to ollama.complete. Otherwise a misconfigured deployment
        # would silently make outbound HTTPS calls with an empty key.
        handler = _handlers(fake_server, seeded_db, fake_ollama, llm_mode="cloud", api_key="")[
            "ask_mailbox"
        ]
        asyncio.run(handler(question="invoice"))
        assert fake_ollama.complete_calls
