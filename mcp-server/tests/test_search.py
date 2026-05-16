"""
Tests for src/tools/search.py.

``search_emails`` is the most-called MCP tool and the layer an LLM hits
first for every retrieval question. Silent formatting drift or a broken
filter forward here degrades answer quality across the whole system
without surfacing as an exception, so the tests assert the visible
contract (mode routing, filter forwarding, clamping, formatted output,
error path) rather than internal SQL.

The project keeps the dep footprint small and does not pull in
pytest-asyncio. Async handlers are driven through ``asyncio.run`` from
otherwise-sync test functions, matching ``test_local_llm.py`` and
``test_imap.py``.
"""

import asyncio

import pytest
from src.tools.search import register_search_tools


def _handler(fake_server, fake_llm, db):
    register_search_tools(fake_server, db, fake_llm)
    return fake_server.tools["search_emails"]


def _text(result) -> str:
    """Extract the single TextContent payload from a tool response."""
    assert len(result) == 1
    return result[0].text


class TestModeValidation:
    def test_invalid_mode_returns_validation_message(self, fake_server, fake_llm, seeded_db):
        handler = _handler(fake_server, fake_llm, seeded_db)
        out = asyncio.run(handler(query="anything", mode="fuzzy"))
        assert "Invalid mode" in _text(out)
        # A rejected mode must not have issued any embed or DB work.
        assert fake_llm.embed_calls == []

    @pytest.mark.parametrize("mode", ["hybrid", "semantic", "keyword"])
    def test_all_valid_modes_are_accepted(self, fake_server, fake_llm, seeded_db, mode):
        handler = _handler(fake_server, fake_llm, seeded_db)
        # ``invoice`` appears only in t-alpha's subject/body; any mode that
        # forwards filters correctly should either return results or a
        # no-results message — not the validation message.
        out = asyncio.run(handler(query="invoice", mode=mode))
        assert "Invalid mode" not in _text(out)


class TestLimitClamping:
    def test_above_ceiling_is_clamped(self, fake_server, fake_llm, seeded_db):
        # Record the limit actually forwarded to the db layer by wrapping
        # hybrid_search. Clamping to 50 is the contract documented at
        # _MAX_SEARCH_LIMIT — anything higher would let an LLM drive a
        # pathologically large FTS + vector payload.
        seen: dict[str, int] = {}
        original = seeded_db.hybrid_search

        def spy(**kwargs):
            seen["limit"] = kwargs.get("limit", -1)
            return original(**kwargs)

        seeded_db.hybrid_search = spy  # type: ignore[assignment]
        handler = _handler(fake_server, fake_llm, seeded_db)
        asyncio.run(handler(query="invoice", mode="hybrid", limit=10_000))
        assert seen["limit"] == 50

    def test_below_floor_is_clamped(self, fake_server, fake_llm, seeded_db):
        seen: dict[str, int] = {}
        original = seeded_db.hybrid_search

        def spy(**kwargs):
            seen["limit"] = kwargs.get("limit", -1)
            return original(**kwargs)

        seeded_db.hybrid_search = spy  # type: ignore[assignment]
        handler = _handler(fake_server, fake_llm, seeded_db)
        asyncio.run(handler(query="invoice", mode="hybrid", limit=-99))
        assert seen["limit"] == 1


class TestModeRouting:
    def test_keyword_mode_skips_embed_call(self, fake_server, fake_llm, seeded_db):
        handler = _handler(fake_server, fake_llm, seeded_db)
        asyncio.run(handler(query="invoice", mode="keyword"))
        # keyword mode must not pay for an embedding the db would ignore.
        assert fake_llm.embed_calls == []

    def test_semantic_mode_embeds_once(self, fake_server, fake_llm, seeded_db):
        handler = _handler(fake_server, fake_llm, seeded_db)
        asyncio.run(handler(query="lunch", mode="semantic"))
        assert fake_llm.embed_calls == ["lunch"]

    def test_hybrid_mode_embeds_once(self, fake_server, fake_llm, seeded_db):
        handler = _handler(fake_server, fake_llm, seeded_db)
        asyncio.run(handler(query="meeting", mode="hybrid"))
        assert fake_llm.embed_calls == ["meeting"]


class TestFilterForwarding:
    def test_keyword_mode_forwards_all_filters(self, fake_server, fake_llm, seeded_db):
        """Keyword mode previously dropped every filter except ``folders``.

        This test guards against a regression — the handler must pass
        from_addr, date_from, date_to, and has_attachments through so
        keyword-only searches honor the same filter contract as hybrid.
        """
        captured: dict = {}
        original = seeded_db.keyword_search

        def spy(**kwargs):
            captured.update(kwargs)
            return original(**kwargs)

        seeded_db.keyword_search = spy  # type: ignore[assignment]
        handler = _handler(fake_server, fake_llm, seeded_db)
        asyncio.run(
            handler(
                query="invoice",
                mode="keyword",
                folders=["INBOX"],
                from_addr="alice@example.com",
                date_from="2024-01-01",
                date_to="2024-12-31",
                has_attachments=True,
            )
        )
        assert captured["folders"] == ["INBOX"]
        assert captured["from_addr"] == "alice@example.com"
        assert captured["date_from"] == "2024-01-01"
        assert captured["date_to"] == "2024-12-31"
        assert captured["has_attachments"] is True


class TestRerankerEvidence:
    """``search_emails`` must request evidence chunks from
    ``hybrid_search`` whenever a reranker is configured, so the
    cross-encoder scores against the actual passage text that lifted
    the thread into ranking — not ``Subject + snippet`` (the 200-char
    body preview from the latest message). Without this, the
    optional reranker can demote the genuinely-relevant thread
    because the only signal it sees is metadata-shaped, which it
    wasn't trained against.
    """

    class _CaptureReranker:
        """Reranker stub that records the documents it scored.

        Conforms to ``RerankerBackend`` structurally (duck-typed); no
        inheritance so test layers stay free of reranker.py imports.
        """

        candidates = 10
        top_n = 5

        def __init__(self) -> None:
            self.seen_docs: list[list[str]] = []

        def rerank(self, query, documents, top_n=None):
            self.seen_docs.append(list(documents))
            return [(i, float(len(documents) - i)) for i in range(len(documents))]

    def test_hybrid_with_reranker_sets_with_evidence_true(self, fake_server, fake_llm, chunked_db):
        captured: dict = {}
        original = chunked_db.hybrid_search

        def spy(**kwargs):
            captured.update(kwargs)
            return original(**kwargs)

        chunked_db.hybrid_search = spy  # type: ignore[assignment]
        reranker = self._CaptureReranker()
        register_search_tools(fake_server, chunked_db, fake_llm, reranker=reranker)
        handler = fake_server.tools["search_emails"]

        asyncio.run(handler(query="invoice", mode="hybrid"))

        assert captured.get("with_evidence") is True, (
            "search_emails with a reranker configured must request "
            "evidence chunks so the cross-encoder scores against the "
            "passage text, not the 200-char snippet"
        )

    def test_hybrid_without_reranker_keeps_with_evidence_false(
        self, fake_server, fake_llm, seeded_db
    ):
        captured: dict = {}
        original = seeded_db.hybrid_search

        def spy(**kwargs):
            captured.update(kwargs)
            return original(**kwargs)

        seeded_db.hybrid_search = spy  # type: ignore[assignment]
        handler = _handler(fake_server, fake_llm, seeded_db)

        asyncio.run(handler(query="invoice", mode="hybrid"))

        # No reranker → we skip the chunk-attach cost. The flag must
        # be explicitly False so a future caller wrapping this in a
        # batch pipeline doesn't inherit a True from a stale default.
        assert captured.get("with_evidence") is False

    def test_reranker_sees_chunk_text_not_just_snippet(self, fake_server, fake_llm, chunked_db):
        # End-to-end: with the fix, the reranker's ``documents``
        # argument carries chunk text. ``alpha-c1`` (in the chunked_db
        # fixture) has text "invoice number 12345 due march 31"; the
        # number ``12345`` appears in NEITHER the subject ("invoice
        # for march") NOR the snippet ("please find the invoice
        # attached"), so finding it in the reranker's documents proves
        # the chunk was attached and forwarded.
        reranker = self._CaptureReranker()
        register_search_tools(fake_server, chunked_db, fake_llm, reranker=reranker)
        handler = fake_server.tools["search_emails"]

        asyncio.run(handler(query="invoice", mode="hybrid"))

        assert reranker.seen_docs, "reranker.rerank was never invoked"
        joined = "\n".join(reranker.seen_docs[0])
        assert "12345" in joined, (
            f"Reranker must score against chunk text — ``12345`` "
            f"appears in chunk ``alpha-c1`` but NEVER in subject or "
            f"snippet, so finding it in the rerank documents is the "
            f"sentinel for evidence-chunk wiring. Got documents:\n"
            f"{reranker.seen_docs[0]!r}"
        )


class TestResultFormatting:
    def test_empty_results_returns_no_results_message(self, fake_server, fake_llm, seeded_db):
        handler = _handler(fake_server, fake_llm, seeded_db)
        out = asyncio.run(handler(query="zxqwzxqw", mode="keyword"))
        assert "No results found" in _text(out)

    def test_formatted_result_includes_key_fields(self, fake_server, fake_llm, seeded_db):
        handler = _handler(fake_server, fake_llm, seeded_db)
        out = asyncio.run(handler(query="invoice", mode="keyword"))
        text = _text(out)
        assert "invoice for march" in text
        # Folder prefix — first bracketed token on the result line.
        assert "[INBOX]" in text
        # Thread id must be included so a follow-up get_thread call can
        # round-trip — dropping it would break the LLM's retrieval flow.
        assert "t-alpha" in text
        # Attachment marker must appear for threads that carry one.
        assert "📎" in text

    def test_result_count_header_matches_results(self, fake_server, fake_llm, seeded_db):
        handler = _handler(fake_server, fake_llm, seeded_db)
        out = asyncio.run(handler(query="invoice OR lunch OR meeting", mode="keyword", limit=10))
        text = _text(out)
        # Header format: "Found N thread(s) for: '...'"
        first_line = text.splitlines()[0]
        assert first_line.startswith("Found ")
        assert "thread(s)" in first_line


class TestErrorPath:
    def test_db_exception_returns_error_text(self, fake_server, fake_llm, seeded_db):
        def boom(**_kwargs):
            raise RuntimeError("simulated index error")

        seeded_db.hybrid_search = boom  # type: ignore[assignment]
        handler = _handler(fake_server, fake_llm, seeded_db)
        out = asyncio.run(handler(query="anything", mode="hybrid"))
        assert "Search error" in _text(out)

    def test_secret_values_are_scrubbed_from_exception_text(self, fake_server, fake_llm, seeded_db):
        # A provider SDK exception that quotes the operator's API key
        # (e.g. an auth-header echo in the error body) must not leak
        # to the caller. Pinning this here covers the main.py wiring
        # of secret_values into the search registrar.
        leaked_key = "sk-leakedABC123"  # pragma: allowlist secret

        def boom(**_kwargs):
            raise RuntimeError(f"upstream auth: Bearer {leaked_key}")

        seeded_db.hybrid_search = boom  # type: ignore[assignment]
        register_search_tools(fake_server, seeded_db, fake_llm, secret_values=[leaked_key])
        handler = fake_server.tools["search_emails"]
        out = asyncio.run(handler(query="anything", mode="hybrid"))
        text = _text(out)
        assert leaked_key not in text
        assert "[REDACTED]" in text

    def test_provider_status_error_is_reduced_to_type_and_status(
        self, fake_server, fake_llm, seeded_db
    ):
        # Provider SDK status errors (openai/anthropic/cohere) carry a
        # ``status_code`` attribute and stringify with the response
        # body, which can echo the user's query back. The outer except
        # must reduce these to ``type + status`` only — never the body
        # — to match the stricter handling in intelligence/rerank/the
        # indexer.
        sensitive_query = "draft about Q3 board comp negotiation"

        class FakeAPIStatusError(Exception):
            status_code = 400

            def __str__(self) -> str:
                return f"upstream 400: request body included {sensitive_query!r}"

        def boom(**_kwargs):
            raise FakeAPIStatusError()

        seeded_db.hybrid_search = boom  # type: ignore[assignment]
        handler = _handler(fake_server, fake_llm, seeded_db)
        out = asyncio.run(handler(query=sensitive_query, mode="hybrid"))
        text = _text(out)
        assert sensitive_query not in text
        assert "FakeAPIStatusError" in text
        assert "status=400" in text


class TestWrongDimEmbedSurfaces:
    """Wrong-dim query vectors used to silently degrade to keyword-only
    results because sqlite-vec's MATCH error was swallowed by the broad
    ``except (sqlite3.Error, ValueError)`` in the DB layer. With the
    ``expected_embed_dim`` validation threaded through
    ``register_search_tools``, semantic and hybrid modes now surface
    the misconfiguration with an actionable error naming the embedder
    knobs the operator can fix.
    """

    def _register_with_wrong_dim_client(self, fake_server, db):
        """Wire the search tools with an embed client that returns a
        vector of the wrong dimension (3 floats against the fixture's
        4-dim schema). Mimics pointing ``EMBED_MODEL`` at a provider
        whose output dim doesn't match what the indexer wrote.

        Passes ``expected_embed_dim`` explicitly the same way
        ``main.py`` does (reading it via ``db.get_embedding_dim()`` at
        startup). Without that, the helper skips the check and the
        wrong vector reaches sqlite-vec — which is the pre-fix
        behavior we're proving has been replaced.
        """
        from tests.conftest import FakeEmbedClient

        # FakeEmbedClient exposes ``.base_url`` / ``.model`` so the
        # error message can name the misconfigured knobs.
        wrong_dim_client = FakeEmbedClient(embedding=[0.1, 0.2, 0.3])
        wrong_dim_client.base_url = "http://wrong-embed/v1"
        wrong_dim_client.model = "wrong-dim-model"
        register_search_tools(
            fake_server,
            db,
            wrong_dim_client,
            expected_embed_dim=db.get_embedding_dim(),
        )
        return fake_server.tools["search_emails"]

    def test_semantic_search_with_wrong_dim_returns_error_not_empty(self, fake_server, seeded_db):
        handler = self._register_with_wrong_dim_client(fake_server, seeded_db)
        out = asyncio.run(handler(query="invoice", mode="semantic"))
        text = _text(out)
        # Operator-visible error, not a silent "No results found".
        assert "Search error" in text
        # The error must name *something* the operator can change —
        # either the env var name or the configured value — so they
        # can find the misconfiguration without reading source.
        assert "EMBED_MODEL" in text or "wrong-dim-model" in text

    def test_hybrid_search_with_wrong_dim_returns_error_not_keyword_fallback(
        self, fake_server, seeded_db
    ):
        # The pre-fix behavior: hybrid silently fell back to keyword
        # because the vector lane failed and the RRF still produced
        # some results. The post-fix behavior surfaces the dim
        # mismatch so the operator knows the embedder is wrong.
        handler = self._register_with_wrong_dim_client(fake_server, seeded_db)
        out = asyncio.run(handler(query="invoice", mode="hybrid"))
        text = _text(out)
        assert "Search error" in text
        assert "EMBED_MODEL" in text or "wrong-dim-model" in text

    def test_keyword_search_works_when_embed_misconfigured(self, fake_server, seeded_db):
        # Keyword search must NOT be gated on dim validation — it
        # never embeds. An operator pointing at the wrong embedder
        # can still use keyword search while they debug.
        handler = self._register_with_wrong_dim_client(fake_server, seeded_db)
        out = asyncio.run(handler(query="invoice", mode="keyword"))
        text = _text(out)
        assert "Search error" not in text


class TestFromNameResolution:
    """The ``from_name`` parameter resolves a name through find_contact
    and applies the resulting canonical address as a strict from_addr
    filter. Each test pins a specific contract step so a regression in
    one path doesn't corrupt the others silently.
    """

    def test_from_name_resolves_to_top_contact_address(self, fake_server, fake_llm, seeded_db):
        # ``alice`` matches alice@example.com (2 threads) — the top
        # contact in find_contact's ranking. The handler should pass
        # that address as ``from_addr`` to hybrid_search.
        captured: dict = {}
        original = seeded_db.hybrid_search

        def spy(**kwargs):
            captured.update(kwargs)
            return original(**kwargs)

        seeded_db.hybrid_search = spy  # type: ignore[assignment]
        handler = _handler(fake_server, fake_llm, seeded_db)
        asyncio.run(handler(query="invoice", from_name="alice"))
        assert captured.get("from_addr") == "alice@example.com"

    def test_from_name_no_match_returns_honest_empty(self, fake_server, fake_llm, seeded_db):
        # When find_contact returns nothing the handler must NOT silently
        # drop the filter and run the search with no sender constraint —
        # that would surface unrelated threads. Return a clear empty
        # signal that names the unresolved query.
        handler = _handler(fake_server, fake_llm, seeded_db)
        out = asyncio.run(handler(query="anything", from_name="zzznosuchcontact"))
        text = _text(out)
        assert "No results found" in text
        assert "zzznosuchcontact" in text

    def test_explicit_from_addr_wins_over_from_name(self, fake_server, fake_llm, seeded_db):
        # When the caller supplied both, ``from_addr`` is the explicit
        # constraint and ``from_name`` is just a hint — explicit wins.
        # Verify by spying on hybrid_search and confirming the explicit
        # address was forwarded unchanged.
        captured: dict = {}
        original = seeded_db.hybrid_search

        def spy(**kwargs):
            captured.update(kwargs)
            return original(**kwargs)

        seeded_db.hybrid_search = spy  # type: ignore[assignment]
        handler = _handler(fake_server, fake_llm, seeded_db)
        asyncio.run(
            handler(
                query="invoice",
                from_addr="explicit@example.com",
                from_name="alice",  # would resolve to alice@example.com
            )
        )
        assert captured.get("from_addr") == "explicit@example.com"

    def test_from_name_skipped_when_only_query_passed(self, fake_server, fake_llm, seeded_db):
        # No from_name -> no find_contact call -> no extra DB work. The
        # spy on find_contact must not see any invocation when the
        # caller doesn't pass from_name.
        called: list = []
        original = seeded_db.find_contact

        def spy(query, limit, *, senders_only=False):
            called.append((query, limit, senders_only))
            return original(query, limit, senders_only=senders_only)

        seeded_db.find_contact = spy  # type: ignore[assignment]
        handler = _handler(fake_server, fake_llm, seeded_db)
        asyncio.run(handler(query="invoice"))
        assert called == []

    def test_from_name_resolution_uses_senders_only(self, fake_server, fake_llm, seeded_db):
        # The from_name -> from_addr resolution must restrict the
        # find_contact aggregation to From-line addresses. Otherwise
        # a frequent recipient/CC contact could outrank the actual
        # sender in find_contact's results, and the resulting
        # from_addr filter would return zero or wrong matches. Spy
        # on find_contact and confirm the keyword arg is forwarded.
        captured_kwargs: dict = {}
        original = seeded_db.find_contact

        def spy(query, limit, *, senders_only=False):
            captured_kwargs["senders_only"] = senders_only
            return original(query, limit, senders_only=senders_only)

        seeded_db.find_contact = spy  # type: ignore[assignment]
        handler = _handler(fake_server, fake_llm, seeded_db)
        asyncio.run(handler(query="invoice", from_name="alice"))
        assert captured_kwargs.get("senders_only") is True

    def test_from_name_lookup_error_surfaces_as_search_error(
        self, fake_server, fake_llm, seeded_db
    ):
        def boom(_query, _limit, *, senders_only=False):
            raise RuntimeError("simulated find_contact failure")

        seeded_db.find_contact = boom  # type: ignore[assignment]
        handler = _handler(fake_server, fake_llm, seeded_db)
        out = asyncio.run(handler(query="anything", from_name="alice"))
        assert "Search error" in _text(out)


class TestParticipantParam:
    """``participant`` filters by anyone on the thread (From/To/Cc) and
    must reach the DB layer for every mode — it is post-fusion, so a
    dropped forward would silently return unfiltered results."""

    def _spy(self, db, attr):
        captured: dict = {}
        original = getattr(db, attr)

        def spy(**kwargs):
            captured.update(kwargs)
            return original(**kwargs)

        setattr(db, attr, spy)
        return captured

    def test_participant_forwarded_to_hybrid(self, fake_server, fake_llm, seeded_db):
        captured = self._spy(seeded_db, "hybrid_search")
        handler = _handler(fake_server, fake_llm, seeded_db)
        asyncio.run(handler(query="invoice", participant="bob@example.com"))
        assert captured.get("participant") == "bob@example.com"

    def test_participant_forwarded_to_keyword(self, fake_server, fake_llm, seeded_db):
        captured = self._spy(seeded_db, "keyword_search")
        handler = _handler(fake_server, fake_llm, seeded_db)
        asyncio.run(handler(query="invoice", mode="keyword", participant="bob@example.com"))
        assert captured.get("participant") == "bob@example.com"

    def test_participant_forwarded_to_semantic(self, fake_server, fake_llm, seeded_db):
        captured = self._spy(seeded_db, "semantic_search")
        handler = _handler(fake_server, fake_llm, seeded_db)
        asyncio.run(handler(query="invoice", mode="semantic", participant="bob@example.com"))
        assert captured.get("participant") == "bob@example.com"


class TestGetEvidence:
    """``get_evidence`` returns the retrieved source passages with full
    provenance and no LLM synthesis."""

    def _handler(self, fake_server, fake_llm, db):
        register_search_tools(fake_server, db, fake_llm)
        return fake_server.tools["get_evidence"]

    def test_tool_is_registered(self, fake_server, fake_llm, seeded_db):
        register_search_tools(fake_server, seeded_db, fake_llm)
        assert "get_evidence" in fake_server.tools

    def test_mailbox_wide_returns_evidence_chunks(self, fake_server, fake_llm, chunked_db):
        handler = self._handler(fake_server, fake_llm, chunked_db)
        out = asyncio.run(handler(query="invoice"))
        text = _text(out)
        assert "Evidence for:" in text
        # alpha-c1's chunk text carries "12345" — absent from subject and
        # snippet, so finding it proves the chunk was surfaced.
        assert "12345" in text
        assert "t-alpha" in text

    def test_thread_scoped_returns_that_threads_chunks(self, fake_server, fake_llm, chunked_db):
        handler = self._handler(fake_server, fake_llm, chunked_db)
        out = asyncio.run(handler(query="invoice", thread_id="t-alpha"))
        assert "12345" in _text(out)

    def test_thread_scoped_unknown_thread(self, fake_server, fake_llm, chunked_db):
        handler = self._handler(fake_server, fake_llm, chunked_db)
        out = asyncio.run(handler(query="invoice", thread_id="no-such-thread"))
        assert "Thread not found" in _text(out)

    def test_blank_query_returns_guidance(self, fake_server, fake_llm, chunked_db):
        handler = self._handler(fake_server, fake_llm, chunked_db)
        out = asyncio.run(handler(query="   "))
        assert "Provide a query" in _text(out)

    def test_no_evidence_message(self, fake_server, fake_llm, empty_db):
        handler = self._handler(fake_server, fake_llm, empty_db)
        out = asyncio.run(handler(query="anything"))
        assert "No evidence found" in _text(out)

    def test_thread_scoped_no_chunks_reports_no_evidence(self, fake_server, fake_llm, chunked_db):
        # t-gamma exists but carries no chunks — the scoped path must
        # report no evidence rather than rendering an empty thread group.
        handler = self._handler(fake_server, fake_llm, chunked_db)
        out = asyncio.run(handler(query="anything", thread_id="t-gamma"))
        assert "No evidence found" in _text(out)

    def test_long_chunk_text_is_truncated(self, fake_server, fake_llm, tmp_path):
        import sqlite3

        import sqlite_vec
        from src.lib.sqlite import Database

        from tests.conftest import _build_schema, _insert_chunk, _insert_thread

        path = tmp_path / "long-evidence.db"
        conn = sqlite3.connect(str(path))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)
        _insert_thread(
            conn,
            thread_id="t1",
            subject="long thread",
            participants=["a@example.com"],
            senders=["a@example.com"],
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        _insert_chunk(
            conn,
            chunk_id="c1",
            message_id="t1",
            thread_id="t1",
            text="x" * 5000,
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        conn.close()
        db = Database(str(path))
        try:
            register_search_tools(fake_server, db, fake_llm)
            handler = fake_server.tools["get_evidence"]
            out = asyncio.run(handler(query="anything", thread_id="t1"))
            assert "[truncated]" in _text(out)
        finally:
            db.close()

    def test_include_scores_shows_lanes_and_distance(self, fake_server, fake_llm, chunked_db):
        handler = self._handler(fake_server, fake_llm, chunked_db)
        out = asyncio.run(handler(query="invoice", include_scores=True))
        text = _text(out)
        assert "Lanes:" in text
        assert "vector distance" in text

    def test_default_omits_scores(self, fake_server, fake_llm, chunked_db):
        handler = self._handler(fake_server, fake_llm, chunked_db)
        text = _text(asyncio.run(handler(query="invoice")))
        assert "Lanes:" not in text
        assert "vector distance" not in text

    def test_thread_scoped_include_scores_omits_lanes(self, fake_server, fake_llm, chunked_db):
        # The thread-scoped path bypasses RRF fusion, so there is no lane
        # provenance — but the per-chunk vector distance is still shown.
        handler = self._handler(fake_server, fake_llm, chunked_db)
        out = asyncio.run(handler(query="invoice", thread_id="t-alpha", include_scores=True))
        text = _text(out)
        assert "Lanes:" not in text
        assert "vector distance" in text

    def test_attachment_provenance_rendered(self, fake_server, fake_llm, attachments_db):
        handler = self._handler(fake_server, fake_llm, attachments_db)
        out = asyncio.run(handler(query="acme", thread_id="t-quote"))
        assert 'Source: attachment "acme-quote.pdf"' in _text(out)

    def test_limit_clamped_at_tool_boundary(self, fake_server, fake_llm, chunked_db):
        captured: dict = {}
        original = chunked_db.hybrid_search

        def spy(**kwargs):
            captured.update(kwargs)
            return original(**kwargs)

        chunked_db.hybrid_search = spy  # type: ignore[assignment]
        handler = self._handler(fake_server, fake_llm, chunked_db)
        asyncio.run(handler(query="invoice", limit=9999))
        assert captured["limit"] == 50

    def test_db_error_returns_evidence_error(self, fake_server, fake_llm, chunked_db):
        def boom(**_kwargs):
            raise RuntimeError("simulated index error")

        chunked_db.hybrid_search = boom  # type: ignore[assignment]
        handler = self._handler(fake_server, fake_llm, chunked_db)
        out = asyncio.run(handler(query="invoice"))
        assert "Evidence error" in _text(out)


class TestSearchAttachmentsTool:
    """``search_attachments`` locates attachments by filename, MIME, and
    extracted text and reports each one's parent thread."""

    def _handler(self, fake_server, fake_llm, db):
        register_search_tools(fake_server, db, fake_llm)
        return fake_server.tools["search_attachments"]

    def test_tool_is_registered(self, fake_server, fake_llm, seeded_db):
        register_search_tools(fake_server, seeded_db, fake_llm)
        assert "search_attachments" in fake_server.tools

    def test_query_match_renders_attachment(self, fake_server, fake_llm, attachments_db):
        handler = self._handler(fake_server, fake_llm, attachments_db)
        text = _text(asyncio.run(handler(query="budget")))
        assert "annual-budget.xlsx" in text
        assert "t-budget" in text

    def test_no_query_lists_all_attachments(self, fake_server, fake_llm, attachments_db):
        handler = self._handler(fake_server, fake_llm, attachments_db)
        text = _text(asyncio.run(handler()))
        assert "Found 3 attachment(s)" in text
        assert "acme-quote.pdf" in text

    def test_no_results_message(self, fake_server, fake_llm, attachments_db):
        handler = self._handler(fake_server, fake_llm, attachments_db)
        out = asyncio.run(handler(query="zzznosuchterm"))
        assert "No attachments found" in _text(out)

    def test_extraction_status_and_snippet_rendered(self, fake_server, fake_llm, attachments_db):
        handler = self._handler(fake_server, fake_llm, attachments_db)
        text = _text(asyncio.run(handler(query="acme")))
        assert "Text extraction: success" in text
        assert "Acme Corporation" in text

    def test_bad_date_returns_error(self, fake_server, fake_llm, attachments_db):
        handler = self._handler(fake_server, fake_llm, attachments_db)
        out = asyncio.run(handler(date_from="not-a-date"))
        assert "Attachment search error" in _text(out)

    def test_limit_clamped_at_tool_boundary(self, fake_server, fake_llm, attachments_db):
        captured: dict = {}
        original = attachments_db.search_attachments

        def spy(**kwargs):
            captured.update(kwargs)
            return original(**kwargs)

        attachments_db.search_attachments = spy  # type: ignore[assignment]
        handler = self._handler(fake_server, fake_llm, attachments_db)
        asyncio.run(handler(limit=9999))
        assert captured["limit"] == 50
