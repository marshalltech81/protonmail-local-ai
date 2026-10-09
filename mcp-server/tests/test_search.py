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
otherwise-sync test functions, matching the other handler tests.
"""

import asyncio
import sqlite3
from contextlib import closing

import pytest
from fastmcp.exceptions import ToolError
from src.tools.outputs import MAX_FROM_NAME_MATCHES
from src.tools.search import _MAX_EVIDENCE_LIMIT, register_search_tools

from tests.conftest import RECENT_REAP_AT, RECENT_REAP_DAY, insert_reaped


def _handler(fake_server, fake_embed, db):
    register_search_tools(fake_server, db, fake_embed)
    return fake_server.tools["search_emails"]


def _text(result) -> str:
    """Extract the prose from a tool's ``CallToolResult``."""
    assert len(result.content) == 1
    return result.content[0].text


def _error(coro) -> str:
    """Run a tool call that must fail; return the ``ToolError`` message
    the client receives as an ``isError`` result."""
    with pytest.raises(ToolError) as exc:
        asyncio.run(coro)
    return str(exc.value)


class TestModeValidation:
    def test_invalid_mode_returns_validation_message(self, fake_server, fake_embed, seeded_db):
        handler = _handler(fake_server, fake_embed, seeded_db)
        assert "Invalid mode" in _error(handler(query="anything", mode="fuzzy"))
        # A rejected mode must not have issued any embed or DB work.
        assert fake_embed.embed_calls == []

    @pytest.mark.parametrize("mode", ["hybrid", "semantic", "keyword"])
    def test_all_valid_modes_are_accepted(self, fake_server, fake_embed, seeded_db, mode):
        handler = _handler(fake_server, fake_embed, seeded_db)
        # ``invoice`` appears only in t-alpha's subject/body; any mode that
        # forwards filters correctly should either return results or a
        # no-results message — not the validation message.
        out = asyncio.run(handler(query="invoice", mode=mode))
        assert "Invalid mode" not in _text(out)


class TestLimitClamping:
    def test_above_ceiling_is_clamped(self, fake_server, fake_embed, seeded_db):
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
        handler = _handler(fake_server, fake_embed, seeded_db)
        asyncio.run(handler(query="invoice", mode="hybrid", limit=10_000))
        assert seen["limit"] == 50

    def test_below_floor_is_clamped(self, fake_server, fake_embed, seeded_db):
        seen: dict[str, int] = {}
        original = seeded_db.hybrid_search

        def spy(**kwargs):
            seen["limit"] = kwargs.get("limit", -1)
            return original(**kwargs)

        seeded_db.hybrid_search = spy  # type: ignore[assignment]
        handler = _handler(fake_server, fake_embed, seeded_db)
        asyncio.run(handler(query="invoice", mode="hybrid", limit=-99))
        assert seen["limit"] == 1


class TestModeRouting:
    def test_keyword_mode_skips_embed_call(self, fake_server, fake_embed, seeded_db):
        handler = _handler(fake_server, fake_embed, seeded_db)
        asyncio.run(handler(query="invoice", mode="keyword"))
        # keyword mode must not pay for an embedding the db would ignore.
        assert fake_embed.embed_calls == []

    def test_semantic_mode_embeds_once(self, fake_server, fake_embed, seeded_db):
        handler = _handler(fake_server, fake_embed, seeded_db)
        asyncio.run(handler(query="lunch", mode="semantic"))
        assert fake_embed.embed_calls == ["lunch"]

    def test_hybrid_mode_embeds_once(self, fake_server, fake_embed, seeded_db):
        handler = _handler(fake_server, fake_embed, seeded_db)
        asyncio.run(handler(query="meeting", mode="hybrid"))
        assert fake_embed.embed_calls == ["meeting"]


class TestFilterForwarding:
    def test_keyword_mode_forwards_all_filters(self, fake_server, fake_embed, seeded_db):
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
        handler = _handler(fake_server, fake_embed, seeded_db)
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

        def __init__(self) -> None:
            self.seen_docs: list[list[str]] = []

        def rerank(self, query, documents, top_n):
            self.seen_docs.append(list(documents))
            return [(i, float(len(documents) - i)) for i in range(len(documents))]

    def test_hybrid_with_reranker_sets_with_evidence_true(
        self, fake_server, fake_embed, chunked_db
    ):
        captured: dict = {}
        original = chunked_db.hybrid_search

        def spy(**kwargs):
            captured.update(kwargs)
            return original(**kwargs)

        chunked_db.hybrid_search = spy  # type: ignore[assignment]
        reranker = self._CaptureReranker()
        register_search_tools(fake_server, chunked_db, fake_embed, reranker=reranker)
        handler = fake_server.tools["search_emails"]

        asyncio.run(handler(query="invoice", mode="hybrid"))

        assert captured.get("with_evidence") is True, (
            "search_emails with a reranker configured must request "
            "evidence chunks so the cross-encoder scores against the "
            "passage text, not the 200-char snippet"
        )

    def test_hybrid_without_reranker_keeps_with_evidence_false(
        self, fake_server, fake_embed, seeded_db
    ):
        captured: dict = {}
        original = seeded_db.hybrid_search

        def spy(**kwargs):
            captured.update(kwargs)
            return original(**kwargs)

        seeded_db.hybrid_search = spy  # type: ignore[assignment]
        handler = _handler(fake_server, fake_embed, seeded_db)

        asyncio.run(handler(query="invoice", mode="hybrid"))

        # No reranker → we skip the chunk-attach cost. The flag must
        # be explicitly False so a future caller wrapping this in a
        # batch pipeline doesn't inherit a True from a stale default.
        assert captured.get("with_evidence") is False

    def test_reranker_sees_chunk_text_not_just_snippet(self, fake_server, fake_embed, chunked_db):
        # End-to-end: with the fix, the reranker's ``documents``
        # argument carries chunk text. ``alpha-c1`` (in the chunked_db
        # fixture) has text "invoice number 12345 due march 31"; the
        # number ``12345`` appears in NEITHER the subject ("invoice
        # for march") NOR the snippet ("please find the invoice
        # attached"), so finding it in the reranker's documents proves
        # the chunk was attached and forwarded.
        reranker = self._CaptureReranker()
        register_search_tools(fake_server, chunked_db, fake_embed, reranker=reranker)
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
    def test_empty_results_returns_no_results_message(self, fake_server, fake_embed, seeded_db):
        handler = _handler(fake_server, fake_embed, seeded_db)
        out = asyncio.run(handler(query="zxqwzxqw", mode="keyword"))
        assert "No results found" in _text(out)

    def test_formatted_result_includes_key_fields(self, fake_server, fake_embed, seeded_db):
        handler = _handler(fake_server, fake_embed, seeded_db)
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

    def test_result_count_header_matches_results(self, fake_server, fake_embed, seeded_db):
        handler = _handler(fake_server, fake_embed, seeded_db)
        out = asyncio.run(handler(query="invoice OR lunch OR meeting", mode="keyword", limit=10))
        text = _text(out)
        # Header format: "Found N thread(s) for: '...'"
        first_line = text.splitlines()[0]
        assert first_line.startswith("Found ")
        assert "thread(s)" in first_line


class TestErrorPath:
    def test_db_exception_returns_error_text(self, fake_server, fake_embed, seeded_db):
        def boom(**_kwargs):
            raise RuntimeError("simulated index error")

        seeded_db.hybrid_search = boom  # type: ignore[assignment]
        handler = _handler(fake_server, fake_embed, seeded_db)
        assert "Search error" in _error(handler(query="anything", mode="hybrid"))

    def test_secret_values_are_scrubbed_from_exception_text(
        self, fake_server, fake_embed, seeded_db
    ):
        # A provider SDK exception that quotes the operator's API key
        # (e.g. an auth-header echo in the error body) must not leak
        # to the caller. Pinning this here covers the main.py wiring
        # of secret_values into the search registrar.
        leaked_key = "sk-leakedABC123"  # pragma: allowlist secret

        def boom(**_kwargs):
            raise ConnectionError(f"upstream auth: Bearer {leaked_key}")

        seeded_db.hybrid_search = boom  # type: ignore[assignment]
        register_search_tools(fake_server, seeded_db, fake_embed, secret_values=[leaked_key])
        handler = fake_server.tools["search_emails"]
        text = _error(handler(query="anything", mode="hybrid"))
        assert leaked_key not in text
        assert "[REDACTED]" in text

    def test_provider_status_error_is_reduced_to_type_and_status(
        self, fake_server, fake_embed, seeded_db
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
        handler = _handler(fake_server, fake_embed, seeded_db)
        text = _error(handler(query=sensitive_query, mode="hybrid"))
        assert sensitive_query not in text
        assert "FakeAPIStatusError" in text
        assert "status=400" in text


def _drop_tables(db, *tables: str) -> None:
    """Drop vec tables from a fixture DB to simulate an unavailable lane."""
    import sqlite3

    import sqlite_vec

    conn = sqlite3.connect(db.path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    for table in tables:
        conn.execute(f"DROP TABLE {table}")
    conn.commit()
    conn.close()


class TestSemanticVectorLaneFailure:
    """#318: both vector helpers turn SQLite errors into empty lists, so a
    semantic search with no working vector lane answered "No results"
    instead of reporting the broken index."""

    def test_no_vector_lane_is_an_error_not_empty(self, fake_server, fake_embed, seeded_db):
        _drop_tables(seeded_db, "threads_vec", "message_chunks_vec")
        handler = _handler(fake_server, fake_embed, seeded_db)
        text = _error(handler(query="invoice", mode="semantic"))
        assert "Search error" in text
        assert "vector" in text
        # Fixed text: neither the query nor SQLite's message is quoted.
        assert "invoice" not in text
        assert "no such table" not in text

    def test_one_lane_missing_still_returns_results(self, fake_server, fake_embed, chunked_db):
        _drop_tables(chunked_db, "message_chunks_vec")
        handler = _handler(fake_server, fake_embed, chunked_db)
        out = asyncio.run(handler(query="invoice", mode="semantic"))
        assert out.structured_content["results"]

    def test_chunk_lane_alone_still_returns_results(self, fake_server, fake_embed, chunked_db):
        _drop_tables(chunked_db, "threads_vec")
        handler = _handler(fake_server, fake_embed, chunked_db)
        out = asyncio.run(handler(query="invoice", mode="semantic"))
        assert out.structured_content["results"]

    def test_valid_empty_index_is_empty_success(self, fake_server, fake_embed, empty_db):
        handler = _handler(fake_server, fake_embed, empty_db)
        out = asyncio.run(handler(query="invoice", mode="semantic"))
        assert not out.is_error
        assert out.structured_content["results"] == []
        assert "No results found" in _text(out)

    def test_hybrid_unchanged_when_vector_lanes_missing(self, fake_server, fake_embed, seeded_db):
        # Hybrid degraded-lane disclosure is #333; the keyword lane still
        # answers here, so hybrid keeps returning results.
        _drop_tables(seeded_db, "threads_vec", "message_chunks_vec")
        handler = _handler(fake_server, fake_embed, seeded_db)
        out = asyncio.run(handler(query="invoice", mode="hybrid"))
        assert out.structured_content["results"]


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
        text = _error(handler(query="invoice", mode="semantic"))
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
        text = _error(handler(query="invoice", mode="hybrid"))
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

    def test_from_name_resolves_to_top_contact_address(self, fake_server, fake_embed, seeded_db):
        # ``alice`` matches alice@example.com (2 threads) — the top
        # contact in find_contact's ranking. The handler should pass
        # that address as ``from_addr`` to hybrid_search.
        captured: dict = {}
        original = seeded_db.hybrid_search

        def spy(**kwargs):
            captured.update(kwargs)
            return original(**kwargs)

        seeded_db.hybrid_search = spy  # type: ignore[assignment]
        handler = _handler(fake_server, fake_embed, seeded_db)
        asyncio.run(handler(query="invoice", from_name="alice"))
        assert captured.get("from_addr") == "alice@example.com"

    def test_from_name_no_match_returns_honest_empty(self, fake_server, fake_embed, seeded_db):
        # When find_contact returns nothing the handler must NOT silently
        # drop the filter and run the search with no sender constraint —
        # that would surface unrelated threads. Return a clear empty
        # signal that names the unresolved query.
        handler = _handler(fake_server, fake_embed, seeded_db)
        out = asyncio.run(handler(query="anything", from_name="zzznosuchcontact"))
        text = _text(out)
        assert "No results found" in text
        assert "zzznosuchcontact" in text

    def test_explicit_from_addr_wins_over_from_name(self, fake_server, fake_embed, seeded_db):
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
        handler = _handler(fake_server, fake_embed, seeded_db)
        asyncio.run(
            handler(
                query="invoice",
                from_addr="explicit@example.com",
                from_name="alice",  # would resolve to alice@example.com
            )
        )
        assert captured.get("from_addr") == "explicit@example.com"

    def test_from_name_skipped_when_only_query_passed(self, fake_server, fake_embed, seeded_db):
        # No from_name -> no find_contact call -> no extra DB work. The
        # spy on find_contact must not see any invocation when the
        # caller doesn't pass from_name.
        called: list = []
        original = seeded_db.find_contact

        def spy(query, limit, *, senders_only=False):
            called.append((query, limit, senders_only))
            return original(query, limit, senders_only=senders_only)

        seeded_db.find_contact = spy  # type: ignore[assignment]
        handler = _handler(fake_server, fake_embed, seeded_db)
        asyncio.run(handler(query="invoice"))
        assert called == []

    def test_from_name_resolution_uses_senders_only(self, fake_server, fake_embed, seeded_db):
        # The from_name -> from_addr resolution must restrict the
        # find_contact aggregation to From-line addresses. Otherwise
        # a frequent recipient/CC contact could outrank the actual
        # sender in find_contact's results, and the resulting
        # from_addr filter would return zero or wrong matches. Spy
        # on find_contact and confirm the keyword arg is forwarded.
        captured_kwargs: dict = {}
        original = seeded_db.find_contact

        def spy(query, limit, *, senders_only=False, folders=None):
            captured_kwargs["senders_only"] = senders_only
            return original(query, limit, senders_only=senders_only, folders=folders)

        seeded_db.find_contact = spy  # type: ignore[assignment]
        handler = _handler(fake_server, fake_embed, seeded_db)
        asyncio.run(handler(query="invoice", from_name="alice"))
        assert captured_kwargs.get("senders_only") is True

    def test_from_name_lookup_error_surfaces_as_search_error(
        self, fake_server, fake_embed, seeded_db
    ):
        def boom(_query, _limit, *, senders_only=False, folders=None):
            raise RuntimeError("simulated find_contact failure")

        seeded_db.find_contact = boom  # type: ignore[assignment]
        handler = _handler(fake_server, fake_embed, seeded_db)
        assert "Search error" in _error(handler(query="anything", from_name="alice"))


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

    def test_participant_forwarded_to_hybrid(self, fake_server, fake_embed, seeded_db):
        captured = self._spy(seeded_db, "hybrid_search")
        handler = _handler(fake_server, fake_embed, seeded_db)
        asyncio.run(handler(query="invoice", participant="bob@example.com"))
        assert captured.get("participant") == "bob@example.com"

    def test_participant_forwarded_to_keyword(self, fake_server, fake_embed, seeded_db):
        captured = self._spy(seeded_db, "keyword_search")
        handler = _handler(fake_server, fake_embed, seeded_db)
        asyncio.run(handler(query="invoice", mode="keyword", participant="bob@example.com"))
        assert captured.get("participant") == "bob@example.com"

    def test_participant_forwarded_to_semantic(self, fake_server, fake_embed, seeded_db):
        captured = self._spy(seeded_db, "semantic_search")
        handler = _handler(fake_server, fake_embed, seeded_db)
        asyncio.run(handler(query="invoice", mode="semantic", participant="bob@example.com"))
        assert captured.get("participant") == "bob@example.com"

    def test_participant_forwarded_to_get_evidence(self, fake_server, fake_embed, seeded_db):
        # get_evidence audits ask_mailbox's retrieval (#537), so it takes
        # the participant filter ask_mailbox takes (#696).
        captured = self._spy(seeded_db, "hybrid_search")
        register_search_tools(fake_server, seeded_db, fake_embed)
        asyncio.run(
            fake_server.tools["get_evidence"](query="invoice", participant="bob@example.com")
        )
        assert captured.get("participant") == "bob@example.com"


class TestSearchEmailsBlankPersonFilters:
    """#705: search_emails treats a blank or padded ``participant`` /
    ``from_name`` as get_evidence does (#702): blank means absent, and
    padding is stripped, in every mode."""

    _METHODS = {
        "hybrid": "hybrid_search",
        "keyword": "keyword_search",
        "semantic": "semantic_search",
    }

    def _spy(self, db, attr):
        captured: dict = {}
        original = getattr(db, attr)

        def spy(**kwargs):
            captured.update(kwargs)
            return original(**kwargs)

        setattr(db, attr, spy)
        return captured

    @pytest.mark.parametrize("mode", ["hybrid", "keyword", "semantic"])
    @pytest.mark.parametrize("blank", ["", " ", "\t"])
    def test_blank_person_filters_are_absent(self, fake_server, fake_embed, seeded_db, mode, blank):
        called: list = []
        seeded_db.find_contact = lambda *a, **_k: called.append(a) or []  # type: ignore[assignment]
        captured = self._spy(seeded_db, self._METHODS[mode])
        handler = _handler(fake_server, fake_embed, seeded_db)
        asyncio.run(handler(query="invoice", mode=mode, participant=blank, from_name=blank))
        assert called == []
        assert captured.get("participant") is None
        assert captured.get("from_addr") is None

    def test_blank_filters_return_the_same_results_as_none(
        self, fake_server, fake_embed, seeded_db
    ):
        handler = _handler(fake_server, fake_embed, seeded_db)
        unfiltered = _text(asyncio.run(handler(query="invoice")))
        blank = _text(asyncio.run(handler(query="invoice", participant=" ", from_name=" ")))
        assert blank == unfiltered

    @pytest.mark.parametrize("mode", ["hybrid", "keyword", "semantic"])
    def test_padded_participant_is_stripped(self, fake_server, fake_embed, seeded_db, mode):
        captured = self._spy(seeded_db, self._METHODS[mode])
        handler = _handler(fake_server, fake_embed, seeded_db)
        asyncio.run(handler(query="invoice", mode=mode, participant=" @example.com "))
        assert captured.get("participant") == "@example.com"

    def test_padded_from_name_is_stripped_before_lookup(self, fake_server, fake_embed, seeded_db):
        lookups: list = []
        original = seeded_db.find_contact

        def spy(query, limit, *, senders_only=False, folders=None):
            lookups.append(query)
            return original(query, limit, senders_only=senders_only, folders=folders)

        seeded_db.find_contact = spy  # type: ignore[assignment]
        handler = _handler(fake_server, fake_embed, seeded_db)
        asyncio.run(handler(query="invoice", from_name=" alice "))
        assert lookups == ["alice"]


class TestGetEvidencePersonFilters:
    """Review round 1 (#702): get_evidence reproduces a person-scoped
    ask_mailbox retrieval, so it resolves ``from_name`` the same way
    (the public find_contact tool cannot: it has no senders_only), and
    blank person filters are absent rather than a substring of " "."""

    def _tool(self, fake_server, fake_embed, db):
        register_search_tools(fake_server, db, fake_embed)
        return fake_server.tools["get_evidence"]

    def _spy(self, db, attr):
        captured: dict = {}
        original = getattr(db, attr)

        def spy(**kwargs):
            captured.update(kwargs)
            return original(**kwargs)

        setattr(db, attr, spy)
        return captured

    def test_from_name_resolves_as_ask_mailbox_does(self, fake_server, fake_embed, seeded_db):
        lookups: list = []
        original = seeded_db.find_contact

        def spy(query, limit, *, senders_only=False, folders=None):
            lookups.append((query, limit, senders_only, folders))
            return original(query, limit, senders_only=senders_only, folders=folders)

        seeded_db.find_contact = spy  # type: ignore[assignment]
        captured = self._spy(seeded_db, "hybrid_search")
        tool = self._tool(fake_server, fake_embed, seeded_db)
        asyncio.run(tool(query="invoice", from_name="alice", folders=["INBOX"]))
        assert lookups == [("alice", MAX_FROM_NAME_MATCHES + 1, True, ["INBOX"])]
        assert captured.get("from_addr") == "alice@example.com"

    def test_explicit_from_addr_wins_over_from_name(self, fake_server, fake_embed, seeded_db):
        called: list = []
        seeded_db.find_contact = lambda *a, **_k: called.append(a) or []  # type: ignore[assignment]
        captured = self._spy(seeded_db, "hybrid_search")
        tool = self._tool(fake_server, fake_embed, seeded_db)
        asyncio.run(tool(query="invoice", from_addr="carol@example.com", from_name="alice"))
        assert called == []
        assert captured.get("from_addr") == "carol@example.com"

    def test_unmatched_from_name_returns_no_evidence(self, fake_server, fake_embed, seeded_db):
        captured = self._spy(seeded_db, "hybrid_search")
        tool = self._tool(fake_server, fake_embed, seeded_db)
        out = asyncio.run(tool(query="invoice", from_name="zzznosuchcontact"))
        text = _text(out)
        assert "no contact matched from_name" in text
        assert "zzznosuchcontact" in text
        assert out.structured_content["chunk_count"] == 0
        assert captured == {}

    def test_from_name_lookup_error_is_a_tool_error(self, fake_server, fake_embed, seeded_db):
        def boom(_query, _limit, *, senders_only=False, folders=None):
            raise RuntimeError("simulated find_contact failure")

        seeded_db.find_contact = boom  # type: ignore[assignment]
        tool = self._tool(fake_server, fake_embed, seeded_db)
        assert "Evidence error" in _error(tool(query="invoice", from_name="alice"))

    def test_from_name_rejected_with_thread_id(self, fake_server, fake_embed, chunked_db):
        tool = self._tool(fake_server, fake_embed, chunked_db)
        message = _error(tool(query="invoice", thread_id="t-alpha", from_name="alice"))
        assert "cannot be combined with thread_id" in message
        assert "from_name" in message

    def test_thread_id_description_names_every_rejected_filter(self, seeded_db):
        # Read from the ``thread_id`` description a client receives in
        # ``tools/list``, not from ``__doc__`` (#1011).
        from tests.test_tool_annotations import _server, _wire_tools

        params = _wire_tools(_server(seeded_db))["get_evidence"]["inputSchema"]["properties"]
        doc = " ".join(params["thread_id"]["description"].split())
        sentence = doc.split("Cannot be combined with", 1)[1].split(".", 1)[0]
        for name in ("folders", "from_addr", "from_name", "participant", "max_threads"):
            assert name in sentence

    def test_padded_participant_is_stripped(self, fake_server, fake_embed, seeded_db):
        # Review round 2 (#702): a padded value broke the substring match.
        captured = self._spy(seeded_db, "hybrid_search")
        tool = self._tool(fake_server, fake_embed, seeded_db)
        asyncio.run(tool(query="invoice", participant=" @example.com "))
        assert captured.get("participant") == "@example.com"

    @pytest.mark.parametrize("blank", ["", " ", "\t"])
    def test_blank_person_filters_are_absent(self, fake_server, fake_embed, seeded_db, blank):
        called: list = []
        seeded_db.find_contact = lambda *a, **_k: called.append(a) or []  # type: ignore[assignment]
        captured = self._spy(seeded_db, "hybrid_search")
        tool = self._tool(fake_server, fake_embed, seeded_db)
        asyncio.run(tool(query="invoice", participant=blank, from_name=blank))
        assert called == []
        assert captured.get("participant") is None
        assert captured.get("from_addr") is None


class TestGetEvidence:
    """``get_evidence`` returns the retrieved source passages with full
    provenance and no LLM synthesis."""

    def _handler(self, fake_server, fake_embed, db):
        register_search_tools(fake_server, db, fake_embed)
        return fake_server.tools["get_evidence"]

    def test_tool_is_registered(self, fake_server, fake_embed, seeded_db):
        register_search_tools(fake_server, seeded_db, fake_embed)
        assert "get_evidence" in fake_server.tools

    def test_mailbox_wide_returns_evidence_chunks(self, fake_server, fake_embed, chunked_db):
        handler = self._handler(fake_server, fake_embed, chunked_db)
        out = asyncio.run(handler(query="invoice"))
        text = _text(out)
        assert "Evidence for:" in text
        # alpha-c1's chunk text carries "12345" — absent from subject and
        # snippet, so finding it proves the chunk was surfaced.
        assert "12345" in text
        assert "t-alpha" in text

    def test_thread_scoped_returns_that_threads_chunks(self, fake_server, fake_embed, chunked_db):
        handler = self._handler(fake_server, fake_embed, chunked_db)
        out = asyncio.run(handler(query="invoice", thread_id="t-alpha"))
        assert "12345" in _text(out)

    @pytest.mark.parametrize(
        "filters",
        [
            {"folders": ["Archive"]},
            {"from_addr": "bob@example.test"},
            {"date_from": "2030-01-01"},
            {"date_to": "2000-01-01"},
            {"has_attachments": True},
            {"has_attachments": False},
            {"participant": "bob@example.test"},
        ],
    )
    def test_thread_scoped_rejects_retrieval_filters(
        self, fake_server, fake_embed, chunked_db, filters
    ):
        """Regression (#219): with thread_id, the filters were accepted
        and silently ignored, so the passages returned looked like they
        satisfied constraints they did not."""
        handler = self._handler(fake_server, fake_embed, chunked_db)
        message = _error(handler(query="invoice", thread_id="t-alpha", **filters))
        assert "cannot be combined with thread_id" in message
        assert next(iter(filters)) in message

    def test_thread_scoped_treats_blank_filters_as_absent(
        self, fake_server, fake_embed, chunked_db
    ):
        """Review round 1: clients often send unset optionals as blank
        strings; the mailbox-wide path already ignores them."""
        handler = self._handler(fake_server, fake_embed, chunked_db)
        out = asyncio.run(
            handler(query="invoice", thread_id="t-alpha", from_addr="", date_from=" ", folders=[])
        )
        assert "12345" in _text(out)

    def test_thread_scoped_unknown_thread(self, fake_server, fake_embed, chunked_db):
        handler = self._handler(fake_server, fake_embed, chunked_db)
        assert "Thread not found" in _error(handler(query="invoice", thread_id="no-such-thread"))

    def test_thread_scoped_reaped_thread_reports_reaped(
        self, fake_server, fake_embed, chunked_db, monkeypatch
    ):
        """PLAN Phase 4 item 4: a thread cited earlier and since reaped
        reads as reaped, not as an ID that never existed."""
        with closing(sqlite3.connect(chunked_db.path)) as conn:
            insert_reaped(
                conn,
                message_id="gone@example.com",
                thread_id="t-gone",
                reaped_at=RECENT_REAP_AT,
            )
        handler = self._handler(fake_server, fake_embed, chunked_db)
        opened: list[int] = []
        connect = chunked_db._connect

        def counted():
            opened.append(1)
            return connect()

        monkeypatch.setattr(chunked_db, "_connect", counted)
        message = _error(handler(query="invoice", thread_id="t-gone"))
        assert f"reaped from the index on {RECENT_REAP_DAY} (mirror retention)" in message
        # Review round 1: the live miss and the reap record share a snapshot.
        assert len(opened) == 1

    def test_thread_reaped_during_the_embed_reports_reaped(
        self, fake_server, fake_embed, chunked_db, monkeypatch
    ):
        """Review round 2: the thread is live at the first read, then the
        reaper commits before the evidence fetch, which finds no chunks.
        That reads as reaped, not as "No evidence found"."""
        fetch = chunked_db.get_query_evidence_chunks

        def reap_then_fetch(*args, **kwargs):
            with closing(sqlite3.connect(chunked_db.path)) as conn:
                conn.execute("DELETE FROM message_chunks WHERE thread_id = 't-alpha'")
                conn.execute("DELETE FROM threads WHERE thread_id = 't-alpha'")
                insert_reaped(
                    conn,
                    message_id="alpha@example.com",
                    thread_id="t-alpha",
                    reaped_at=RECENT_REAP_AT,
                )
            return fetch(*args, **kwargs)

        monkeypatch.setattr(chunked_db, "get_query_evidence_chunks", reap_then_fetch)
        handler = self._handler(fake_server, fake_embed, chunked_db)
        message = _error(handler(query="invoice", thread_id="t-alpha"))
        assert f"reaped from the index on {RECENT_REAP_DAY} (mirror retention)" in message

    def test_blank_query_returns_guidance(self, fake_server, fake_embed, chunked_db):
        handler = self._handler(fake_server, fake_embed, chunked_db)
        assert "Provide a query" in _error(handler(query="   "))

    def test_no_evidence_message(self, fake_server, fake_embed, empty_db):
        handler = self._handler(fake_server, fake_embed, empty_db)
        out = asyncio.run(handler(query="anything"))
        assert "No evidence found" in _text(out)

    def test_thread_scoped_no_chunks_reports_no_evidence(self, fake_server, fake_embed, chunked_db):
        # t-gamma exists but carries no chunks — the scoped path must
        # report no evidence rather than rendering an empty thread group.
        handler = self._handler(fake_server, fake_embed, chunked_db)
        out = asyncio.run(handler(query="anything", thread_id="t-gamma"))
        assert "No evidence found" in _text(out)

    def test_long_chunk_text_is_truncated(self, fake_server, fake_embed, tmp_path):
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
        register_search_tools(fake_server, db, fake_embed)
        handler = fake_server.tools["get_evidence"]
        out = asyncio.run(handler(query="anything", thread_id="t1"))
        assert "[truncated]" in _text(out)

    def test_include_scores_shows_lanes_and_distance(self, fake_server, fake_embed, chunked_db):
        handler = self._handler(fake_server, fake_embed, chunked_db)
        out = asyncio.run(handler(query="invoice", include_scores=True))
        text = _text(out)
        assert "Lanes:" in text
        assert "vector distance" in text

    def test_default_omits_scores(self, fake_server, fake_embed, chunked_db):
        handler = self._handler(fake_server, fake_embed, chunked_db)
        text = _text(asyncio.run(handler(query="invoice")))
        assert "Lanes:" not in text
        assert "vector distance" not in text

    def test_thread_scoped_include_scores_omits_lanes(self, fake_server, fake_embed, chunked_db):
        # The thread-scoped path bypasses RRF fusion, so there is no lane
        # provenance — but the per-chunk vector distance is still shown.
        handler = self._handler(fake_server, fake_embed, chunked_db)
        out = asyncio.run(handler(query="invoice", thread_id="t-alpha", include_scores=True))
        text = _text(out)
        assert "Lanes:" not in text
        assert "vector distance" in text

    def test_attachment_provenance_rendered(self, fake_server, fake_embed, attachments_db):
        handler = self._handler(fake_server, fake_embed, attachments_db)
        out = asyncio.run(handler(query="acme", thread_id="t-quote"))
        assert 'Source: attachment "acme-quote.pdf"' in _text(out)

    def test_mailbox_wide_returns_the_chunks_ask_mailbox_uses(
        self, fake_server, fake_embed, chunked_db
    ):
        """Review round 2 on #445: get_evidence is documented as the same
        chunks ask_mailbox feeds its model, so it must ask for the same
        per-thread cap rather than hybrid_search's default of three."""
        from datetime import UTC, datetime

        from src.lib.sqlite import ChunkResult, ThreadResult

        chunks = [
            ChunkResult(
                chunk_id=f"c{i}",
                message_id=f"m{i}",
                claimant_id=f"m{i}#00000000",
                thread_id="t-six",
                chunk_index=0,
                text=f"PASSAGE_MARKER_{i}",
                char_start=0,
                char_end=17,
            )
            for i in range(1, 7)
        ]

        def fake_search(**kwargs):
            per_thread = kwargs.get("evidence_per_thread", 3)
            return [
                ThreadResult(
                    thread_id="t-six",
                    subject="Six passages",
                    participants=[],
                    folder="INBOX",
                    date_first=datetime(2024, 1, 1, tzinfo=UTC),
                    date_last=datetime(2024, 1, 1, tzinfo=UTC),
                    message_ids=[],
                    snippet="",
                    has_attachments=False,
                    evidence_chunks=chunks[:per_thread],
                )
            ]

        chunked_db.hybrid_search = fake_search  # type: ignore[assignment]
        handler = self._handler(fake_server, fake_embed, chunked_db)
        text = _text(asyncio.run(handler(query="invoice")))
        assert "PASSAGE_MARKER_6" in text
        # The flat ``limit`` still caps the total.
        text = _text(asyncio.run(handler(query="invoice", limit=4)))
        assert "PASSAGE_MARKER_4" in text
        assert "PASSAGE_MARKER_5" not in text

    def test_limit_clamped_at_tool_boundary(self, fake_server, fake_embed, chunked_db):
        captured: dict = {}
        original = chunked_db.hybrid_search

        def spy(**kwargs):
            captured.update(kwargs)
            return original(**kwargs)

        chunked_db.hybrid_search = spy  # type: ignore[assignment]
        handler = self._handler(fake_server, fake_embed, chunked_db)
        asyncio.run(handler(query="invoice", limit=9999))
        assert captured["limit"] == _MAX_EVIDENCE_LIMIT

    def test_ceiling_covers_ask_mailboxs_largest_evidence_set(self):
        """#449: ask_mailbox can put up to ``_MAX_ASK_THREADS`` threads x
        ``PROMPT_EVIDENCE_CHUNKS_PER_THREAD`` chunks in its prompt; the
        audit tool must be able to return all of them. Computed from the
        shared constants so raising either one fails here first."""
        from src.lib.sqlite import PROMPT_EVIDENCE_CHUNKS_PER_THREAD
        from src.tools.intelligence import _MAX_ASK_THREADS

        assert _MAX_EVIDENCE_LIMIT >= _MAX_ASK_THREADS * PROMPT_EVIDENCE_CHUNKS_PER_THREAD

    def test_request_at_ask_mailbox_maximum_is_honoured(self, fake_server, fake_embed, chunked_db):
        """#449: a ``limit`` equal to ask_mailbox's largest evidence set
        returns every chunk rather than being clamped to the search cap."""
        from datetime import UTC, datetime

        from src.lib.sqlite import PROMPT_EVIDENCE_CHUNKS_PER_THREAD, ChunkResult, ThreadResult
        from src.tools.intelligence import _MAX_ASK_THREADS

        def fake_search(**kwargs):
            per_thread = kwargs["evidence_per_thread"]
            return [
                ThreadResult(
                    thread_id=f"t{t}",
                    subject=f"Thread {t}",
                    participants=[],
                    folder="INBOX",
                    date_first=datetime(2024, 1, 1, tzinfo=UTC),
                    date_last=datetime(2024, 1, 1, tzinfo=UTC),
                    message_ids=[],
                    snippet="",
                    has_attachments=False,
                    evidence_chunks=[
                        ChunkResult(
                            chunk_id=f"c{t}-{i}",
                            message_id=f"m{t}-{i}",
                            claimant_id=f"m{t}-{i}#00000000",
                            thread_id=f"t{t}",
                            chunk_index=i,
                            text=f"passage {t}-{i}",
                            char_start=0,
                            char_end=12,
                        )
                        for i in range(per_thread)
                    ],
                )
                for t in range(kwargs["limit"])
            ]

        chunked_db.hybrid_search = fake_search  # type: ignore[assignment]
        handler = self._handler(fake_server, fake_embed, chunked_db)
        wanted = _MAX_ASK_THREADS * PROMPT_EVIDENCE_CHUNKS_PER_THREAD
        out = asyncio.run(handler(query="invoice", limit=wanted))
        assert out.structured_content["chunk_count"] == wanted
        assert len(out.structured_content["threads"]) == _MAX_ASK_THREADS

    def test_db_error_returns_evidence_error(self, fake_server, fake_embed, chunked_db):
        def boom(**_kwargs):
            raise RuntimeError("simulated index error")

        chunked_db.hybrid_search = boom  # type: ignore[assignment]
        handler = self._handler(fake_server, fake_embed, chunked_db)
        assert "Evidence error" in _error(handler(query="invoice"))


class TestSearchAttachmentsTool:
    """``search_attachments`` locates attachments by filename, MIME, and
    extracted text and reports each one's parent thread."""

    def _handler(self, fake_server, fake_embed, db):
        register_search_tools(fake_server, db, fake_embed)
        return fake_server.tools["search_attachments"]

    def test_tool_is_registered(self, fake_server, fake_embed, seeded_db):
        register_search_tools(fake_server, seeded_db, fake_embed)
        assert "search_attachments" in fake_server.tools

    def test_query_match_renders_attachment(self, fake_server, fake_embed, attachments_db):
        handler = self._handler(fake_server, fake_embed, attachments_db)
        text = _text(asyncio.run(handler(query="budget")))
        assert "annual-budget.xlsx" in text
        assert "t-budget" in text

    def test_no_query_lists_all_attachments(self, fake_server, fake_embed, attachments_db):
        handler = self._handler(fake_server, fake_embed, attachments_db)
        text = _text(asyncio.run(handler()))
        assert "Found 3 attachment(s)" in text
        assert "acme-quote.pdf" in text

    def test_no_results_message(self, fake_server, fake_embed, attachments_db):
        handler = self._handler(fake_server, fake_embed, attachments_db)
        out = asyncio.run(handler(query="zzznosuchterm"))
        assert "No attachments found" in _text(out)

    def test_extraction_status_and_snippet_rendered(self, fake_server, fake_embed, attachments_db):
        handler = self._handler(fake_server, fake_embed, attachments_db)
        text = _text(asyncio.run(handler(query="acme")))
        assert "Text extraction: success" in text
        assert "Acme Corporation" in text

    def test_bad_date_returns_error(self, fake_server, fake_embed, attachments_db):
        handler = self._handler(fake_server, fake_embed, attachments_db)
        assert "Attachment search error" in _error(handler(date_from="not-a-date"))

    def test_limit_clamped_at_tool_boundary(self, fake_server, fake_embed, attachments_db):
        captured: dict = {}
        original = attachments_db.search_attachments_with_count

        def spy(**kwargs):
            captured.update(kwargs)
            return original(**kwargs)

        attachments_db.search_attachments_with_count = spy  # type: ignore[method-assign]
        handler = self._handler(fake_server, fake_embed, attachments_db)
        asyncio.run(handler(limit=9999))
        assert captured["limit"] == 50


class TestLoggingPrivacy:
    @pytest.mark.parametrize("tool", ["search_emails", "get_evidence", "search_attachments"])
    @pytest.mark.parametrize("field", ["date_from", "date_to"])
    def test_invalid_date_value_is_not_logged(
        self, fake_server, fake_embed, seeded_db, caplog, tool, field
    ):
        # log_tool_call withholds a non-ISO date; the validation error
        # quoting it must not put it back in the log (#238).
        import logging

        register_search_tools(fake_server, seeded_db, fake_embed)
        handler = fake_server.tools[tool]
        kwargs = {"query": "invoice", field: "private-sentinel-value"}
        with caplog.at_level(logging.DEBUG):
            text = _error(handler(**kwargs))
        assert "private-sentinel-value" in text  # the caller still learns why
        assert "private-sentinel-value" not in caplog.text
        assert field in caplog.text

    def test_search_query_never_reaches_logs(self, fake_server, fake_embed, seeded_db, caplog):
        import logging

        handler = _handler(fake_server, fake_embed, seeded_db)
        with caplog.at_level(logging.DEBUG):
            asyncio.run(handler(query="zq-private-medical-diagnosis", from_addr="dr@example.com"))

        assert "tool=search_emails" in caplog.text
        assert "zq-private-medical-diagnosis" not in caplog.text
        assert "dr@example.com" not in caplog.text


class TestInvertedDateRange:
    """#312: every search tool rejects ``date_from`` after ``date_to`` as
    an empty interval, and logs which fields were rejected."""

    @pytest.mark.parametrize(
        ("tool", "extra"),
        [
            ("search_emails", {"mode": "keyword"}),
            ("search_emails", {"mode": "semantic"}),
            ("search_emails", {"mode": "hybrid"}),
            ("get_evidence", {}),
            ("search_attachments", {}),
        ],
    )
    def test_inverted_range_is_an_error(
        self, fake_server, fake_embed, seeded_db, caplog, tool, extra
    ):
        import logging

        register_search_tools(fake_server, seeded_db, fake_embed)
        handler = fake_server.tools[tool]
        kwargs = {"query": "invoice", "date_from": "2099-03-04", "date_to": "2098-05-06", **extra}
        with caplog.at_level(logging.DEBUG):
            text = _error(handler(**kwargs))
        assert "date_from must not be after date_to" in text
        assert f"rejected invalid argument: {tool}.date_from/date_to" in caplog.text


class TestDateRangeRejectedBeforeWork:
    """Review round 1 on #416: an inverted range is rejected at each tool's
    entry, before the embed call, any retrieval, or the model. Checked
    only in the database, it still cost a (possibly remote) embedding
    request, and an embedder or vector-index failure masked the
    documented date-range error."""

    _RETRIEVAL = (
        "find_contact",
        "keyword_search",
        "semantic_search",
        "hybrid_search",
        "search_attachments",
        "query_messages",
        "get_thread",
    )

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("search_emails", {"query": "invoice", "mode": "keyword"}),
            ("search_emails", {"query": "invoice", "mode": "semantic"}),
            ("search_emails", {"query": "invoice", "mode": "hybrid"}),
            ("search_emails", {"query": "invoice", "from_name": "alice"}),
            ("get_evidence", {"query": "invoice"}),
            ("search_attachments", {"query": "invoice"}),
            ("query_messages", {}),
            ("ask_mailbox", {"question": "What was the budget?"}),
            ("extract_from_emails", {"query": "invoice", "schema": {"vendor": "string"}}),
        ],
    )
    def test_inverted_range_is_rejected_before_any_work(
        self, fake_server, seeded_db, caplog, tool, args
    ):
        import logging

        from src.tools.intelligence import register_intelligence_tools
        from src.tools.retrieval import register_retrieval_tools

        from tests.conftest import FakeEmbedClient, FakeInferenceClient

        retrieval_calls: list[str] = []
        for name in self._RETRIEVAL:

            def record(*_args, _name=name, **_kwargs):
                retrieval_calls.append(_name)
                raise AssertionError(f"{_name} ran before the date range was checked")

            setattr(seeded_db, name, record)
        embed = FakeEmbedClient()
        llm = FakeInferenceClient()
        register_search_tools(fake_server, seeded_db, embed)
        register_retrieval_tools(fake_server, seeded_db)
        register_intelligence_tools(fake_server, seeded_db, embed, llm)

        kwargs = {**args, "date_from": "2025-01-01", "date_to": "2024-01-01"}
        with caplog.at_level(logging.DEBUG):
            text = _error(fake_server.tools[tool](**kwargs))
        assert "date_from must not be after date_to" in text
        assert embed.embed_calls == []
        assert llm.complete_calls == []
        assert retrieval_calls == []
        assert "rejected invalid" in caplog.text


def test_utc_overflowing_date_bound_is_a_filter_error(fake_server, fake_embed, seeded_db, caplog):
    """A bound whose UTC conversion overflows reaches the caller as the
    date-filter error, not a raw OverflowError, before any provider work."""
    value = "0001-01-01T00:00:00+14:00"
    register_search_tools(fake_server, seeded_db, fake_embed)
    with caplog.at_level("DEBUG"):
        text = _error(fake_server.tools["search_emails"](query="invoice", date_from=value))
    assert "date_from" in text
    assert "OverflowError" not in caplog.text
    assert fake_embed.embed_calls == []


_LOCAL_DB_MARKER = "privatemarkerq7z"


class TestLocalDbErrorTextWithheld:
    """search_attachments and the search_emails from_name lookup are
    local-DB work, but a conversion error from stored rows can quote mail,
    so a failure reaches the log and the caller as its type only (#257)."""

    @pytest.mark.parametrize(
        ("tool", "method", "kwargs"),
        [
            ("search_attachments", "search_attachments_with_count", {"query": "invoice"}),
            ("search_emails", "find_contact", {"query": "invoice", "from_name": "alice"}),
        ],
    )
    def test_value_error_text_is_withheld(
        self, fake_server, fake_embed, seeded_db, monkeypatch, caplog, tool, method, kwargs
    ):
        def boom(*_args, **_kwargs):
            raise ValueError(f"bad stored value {_LOCAL_DB_MARKER}")

        monkeypatch.setattr(seeded_db, method, boom)
        register_search_tools(fake_server, seeded_db, fake_embed)
        handler = fake_server.tools[tool]
        with caplog.at_level("DEBUG"):
            text = _error(handler(**kwargs))
        assert _LOCAL_DB_MARKER not in text
        assert _LOCAL_DB_MARKER not in caplog.text
        assert "ValueError" in text


@pytest.fixture
def cross_folder_db(tmp_path):
    """Thread ``t-x`` started in INBOX with a reply filed in Sent; thread
    ``t-o`` lives in Archive. Both match "ledger" in every keyword lane
    (thread FTS, body chunk, attachment filename) and the vector lanes."""
    import sqlite3

    import sqlite_vec
    from src.lib.sqlite import Database

    from tests.conftest import (
        _build_schema,
        _insert_attachment,
        _insert_chunk,
        _insert_message,
        _insert_thread,
    )

    path = tmp_path / "cross-folder.db"
    conn = sqlite3.connect(str(path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _build_schema(conn)
    for thread_id, folder, root in (("t-x", "INBOX", "x1"), ("t-o", "Archive", "o1")):
        _insert_thread(
            conn,
            thread_id=thread_id,
            subject="ledger",
            participants=["alice@example.com"],
            senders=["alice@example.com"],
            folder=folder,
            message_ids=[root],
            body_text="ledger totals",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
    _insert_message(
        conn,
        message_id="x2",
        thread_id="t-x",
        folder="Sent",
        sent_at="2024-01-02T10:00:00+00:00",
        body="ledger reply",
        in_reply_to="x1",
    )
    _insert_chunk(
        conn,
        chunk_id="o1-body",
        message_id="o1",
        thread_id="t-o",
        text="ledger archived",
        embedding=[1.0, 0.0, 0.0, 0.0],
    )
    _insert_attachment(
        conn, message_id="x2", thread_id="t-x", attachment_id="att-x", filename="ledger.pdf"
    )
    conn.close()
    return Database(str(path))


def _db_thread_ids(db, call, folder):
    """Thread IDs one search path returns for ``folders=[folder]``."""
    embedding = [1.0, 0.0, 0.0, 0.0]
    if call == "keyword":
        results = db.keyword_search("ledger", folders=[folder])
    elif call == "semantic":
        results = db.semantic_search(embedding, folders=[folder])
    elif call == "hybrid":
        results = db.hybrid_search("ledger", embedding, folders=[folder])
    elif call == "hybrid_evidence":
        # ask_mailbox / extract_from_emails call this shape.
        results = db.hybrid_search("ledger", embedding, folders=[folder], with_evidence=True)
    elif call == "like_fallback":
        results = db._like_fallback("ledger", 50, folders=[folder])
    else:
        lane = {
            "thread_fts": db._thread_keyword_search,
            "chunk_fts": db._chunk_keyword_search,
            "attachment_fts": db._attachment_keyword_search,
        }[call]
        results = lane("ledger", 50, folders=[folder])
    return {r.thread_id for r in results}


class TestFolderFilterMatchesListThreads:
    """Regression (#415): search folder filters compared ``threads.folder``
    (the folder of the thread's first message) while ``list_threads``
    reads per-message folders, so a thread listed under Sent was missing
    from a Sent-scoped search. Every path must agree on membership."""

    FOLDERS = ["INBOX", "Sent", "Archive"]

    @pytest.mark.parametrize("folder", FOLDERS)
    @pytest.mark.parametrize(
        "call",
        [
            "keyword",
            "semantic",
            "hybrid",
            "hybrid_evidence",
            "like_fallback",
            "thread_fts",
            "chunk_fts",
            "attachment_fts",
        ],
    )
    def test_db_search_paths(self, cross_folder_db, call, folder):
        listed = {t.thread_id for t in cross_folder_db.list_threads(folder=folder)}
        found = _db_thread_ids(cross_folder_db, call, folder)
        if call == "attachment_fts":
            # Only t-x carries a matching attachment.
            listed &= {"t-x"}
        assert found == listed

    @pytest.mark.parametrize("folder", FOLDERS)
    @pytest.mark.parametrize("mode", ["hybrid", "semantic", "keyword"])
    def test_search_emails(self, fake_server, fake_embed, cross_folder_db, mode, folder):
        listed = {t.thread_id for t in cross_folder_db.list_threads(folder=folder)}
        handler = _handler(fake_server, fake_embed, cross_folder_db)
        out = asyncio.run(handler(query="ledger", mode=mode, folders=[folder]))
        assert {r["thread_id"] for r in out.structured_content["results"]} == listed

    @pytest.mark.parametrize("folder", FOLDERS)
    def test_get_evidence_mailbox_wide(self, fake_server, fake_embed, cross_folder_db, folder):
        listed = {t.thread_id for t in cross_folder_db.list_threads(folder=folder)}
        register_search_tools(fake_server, cross_folder_db, fake_embed)
        out = asyncio.run(fake_server.tools["get_evidence"](query="ledger", folders=[folder]))
        assert {t["thread_id"] for t in out.structured_content["threads"]} == listed


@pytest.mark.parametrize(
    "tool",
    ["get_evidence", "ask_mailbox", "extract_from_emails", "brief_issue", "check_conclusion"],
)
def test_evidence_tool_descriptions_state_the_date_contract(tool, seeded_db):
    """Under a date range the evidence tools select threads by span and
    may show passages from outside the range (docs/architecture.md,
    Message time), so each tool's ``date_from`` description must say so
    and point the model at each passage's own ``occurred_at`` and
    ``sent_at``. Read from the parameter description a client receives
    in ``tools/list``, not from ``__doc__`` (#1011)."""
    from tests.test_tool_annotations import _server, _wire_tools

    params = _wire_tools(_server(seeded_db))[tool]["inputSchema"]["properties"]
    doc = " ".join(params["date_from"]["description"].split())
    assert (
        "span (its messages' occurred_at, else sent_at, else when first indexed, #1373) "
        "overlaps the range"
    ) in doc
    assert "occurred_at and sent_at" in doc


def _wire_descriptions(db) -> dict[str, str]:
    """Each tool's description as a client receives it. FastMCP sends
    only the docstring's first text section, so guidance placed after
    an indented block never reaches the calling model; these checks
    read the wire, not ``__doc__``."""
    from tests.test_tool_annotations import _server, _wire_tools

    return {
        name: " ".join(tool["description"].split())
        for name, tool in _wire_tools(_server(db)).items()
    }


@pytest.mark.parametrize("tool", ["ask_mailbox", "get_evidence"])
def test_evidence_descriptions_warn_passages_can_stop_before_a_resolution(tool, empty_db):
    """#974 option 1: per-thread passages are picked by similarity to
    the question, so a late message that settles the matter can be left
    out; the description says so and how to follow up."""
    doc = _wire_descriptions(empty_db)[tool]
    asked = {"ask_mailbox": "question", "get_evidence": "query"}[tool]
    assert "late resolution in a long thread" in doc
    # Review round 11: ``_matched_attachments`` matches query words
    # against MIME types as well as filenames, so a re-ask that keeps a
    # file-type word ("PDF") keeps the same attachment first.
    assert (
        "re-ask about the resolution without the attachment's filename or "
        'file-type words (for example "PDF"), since either keeps that '
        "attachment first, or read the thread's later messages with "
        "get_thread or get_message" in doc
    )
    # Review round 8 (owner), and #858: the full order of
    # ``get_evidence_chunks_for_threads``. The named attachment's first
    # passage and the best-ranked keyword-matched passage (#1246) lead;
    # then the rest of that attachment, the thread's other attachments,
    # then the body, each by similarity.
    assert (
        f"when the {asked} matches one of its attachments' filename or MIME type, "
        "that attachment's first passage, then the passage holding the rarest words "
        f"of the {asked} in that thread" in doc
    )
    assert (
        "The rest of that attachment follows, then its other attachments, then the "
        "body, each by similarity" in doc
    )
    assert "attachments can then fill every slot but the keyword one" in doc


def test_ask_mailbox_description_states_its_slot_count_and_fallback(empty_db):
    """ask_mailbox takes ``PROMPT_EVIDENCE_CHUNKS_PER_THREAD`` passages
    per thread and shows a chunkless thread's indexed text."""
    from src.lib.sqlite import PROMPT_EVIDENCE_CHUNKS_PER_THREAD

    assert PROMPT_EVIDENCE_CHUNKS_PER_THREAD == 6
    doc = _wire_descriptions(empty_db)["ask_mailbox"]
    assert "Each thread gives at most six passages" in doc
    assert "A thread with no passages shows its indexed text" in doc


def test_get_evidence_description_states_its_slots_and_chunkless_threads(empty_db):
    """Review round 8: get_evidence does not show a chunkless thread's
    indexed text (that is ask_mailbox's fallback): it lists the thread
    with an empty chunks list under max_threads and leaves it out
    otherwise. Its per-thread cap is six mailbox-wide and ``limit``
    with thread_id."""
    doc = _wire_descriptions(empty_db)["get_evidence"]
    assert "six per thread mailbox-wide, limit with thread_id" in doc
    assert (
        "A thread with no passages is listed with an empty chunks list "
        "(with max_threads) or left out; read it with get_thread." in doc
    )
    # Only the fixed note on deferred attachment passages (#1236).
    assert "indexed text" not in doc.replace("retained indexed text", "")


def test_extract_description_points_to_the_population_recipe(empty_db):
    """#976 option 1: extraction covers the top ``limit`` threads only,
    and the description says so in one sentence pointing to the recipe
    in docs/mcp-tools.md. Review rounds 1-5 grew the recipe on the wire;
    after round 6 the owner moved it to the docs only, so the
    description must not carry its steps."""
    doc = _wire_descriptions(empty_db)["extract_from_emails"]
    assert (
        "Only the top ``limit`` threads are searched, not every match "
        "(population recipe: docs/mcp-tools.md)."
    ) in doc
    assert "search_attachments" not in doc
    assert "claimant_id" not in doc
    # Review round 8 (owner): the pre-read disclosure is back on the wire.
    assert "Before a population run, tell the user how much mail will be read." in doc


def test_query_messages_description_points_to_the_multi_lane_recipe(empty_db):
    """#992 option 1: a broad question needs several exact lanes, and the
    description says so in one sentence pointing to the recipe in
    docs/mcp-tools.md, where the steps live (as #976's population recipe
    does); the wire carries the lanes, the union key and the pointer,
    not the steps."""
    doc = _wire_descriptions(empty_db)["query_messages"]
    assert (
        "For every message about a topic, run one exact lane per subject term, "
        "body word set and participant, page each to the end, and union the rows "
        "by claimant_id with the lane that found each (multi-lane recipe: "
        "docs/mcp-tools.md)."
    ) in doc
    # Review round 7 on #1100: no thread-first reading instruction on the wire.
    assert "group by thread_id" not in doc
    # Review round 1 on #1100: the union keeps message identities.
    assert "union by thread_id" not in doc
    assert doc.count("multi-lane recipe") == 1
