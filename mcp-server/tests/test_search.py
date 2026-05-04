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
