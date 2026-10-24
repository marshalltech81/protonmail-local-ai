"""``search_attachments(sender=...)``: the carrying message's From (#1056).

``from_addr`` selects whole threads, so an attachment another person's
message carried into a selected thread is listed as if the address sent
it. ``sender`` keeps only attachments whose carrying message's From
matches, compiled through the shared predicate module (#1084) so the
match rule is ``query_messages``' ``sender`` leaf, in every lane and
before each lane's limit. All data is synthetic.
"""

import asyncio
import logging

import pytest
from fastmcp import Client
from src.lib.security import _LOGGABLE_TOOL_PARAMS
from src.lib.sqlite import _FILTERED_OVERSAMPLE, Database
from src.tools.search import register_search_tools

from tests.conftest import _insert_attachment, _insert_extraction, _insert_message, claimant_of
from tests.test_sqlite import _open_built_db_conn

VENDOR = "Vendor Person <vendor@example.com>"
COLLEAGUE = "Colleague Name <colleague@other.test>"
MARKER = "zq-synthetic-carrier-marker@example.invalid"


def _add(
    conn,
    message_id: str,
    thread_id: str,
    sent_at: str,
    from_: str,
    *,
    filename: str | None = None,
    text: str = "alpha bravo",
    content_type: str = "application/pdf",
    sender_ambiguous: int | None = 0,
    status: str = "success",
) -> None:
    """One message carrying one attachment whose extracted text is
    ``text`` (also its attachment chunk, for the text lane)."""
    _insert_message(
        conn,
        message_id=message_id,
        thread_id=thread_id,
        sent_at=sent_at,
        subject="synthetic",
        from_=[from_],
        has_attachments=True,
        attachment_text=text,
        sender_ambiguous=sender_ambiguous,
    )
    _insert_attachment(
        conn,
        message_id=message_id,
        thread_id=thread_id,
        attachment_id=f"{message_id}-att",
        filename=filename or f"ledger-{message_id}.pdf",
        content_type=content_type,
    )
    _insert_extraction(
        conn,
        attachment_id=f"{message_id}-att",
        status=status,
        extracted_text=text if status == "success" else None,
    )


@pytest.fixture
def carrier_db(tmp_path) -> Database:
    """Thread ``t-v``: the vendor's message ``v1`` and a colleague's
    reply ``c1``, each carrying an attachment. Thread ``t-o``: a
    colleague-only message ``o1``. Filenames hold ``ledger``, extracted
    texts ``bravo``, so each word reaches exactly one FTS lane."""
    conn, path = _open_built_db_conn(tmp_path, "carrier.db")
    _add(conn, "v1", "t-v", "2024-01-10T09:00:00+00:00", VENDOR)
    _add(conn, "c1", "t-v", "2024-02-10T09:00:00+00:00", COLLEAGUE)
    _add(conn, "o1", "t-o", "2024-03-10T09:00:00+00:00", COLLEAGUE)
    conn.close()
    return Database(str(path))


def _ids(results) -> set[str]:
    return {a.attachment_id for a in results}


class TestAttachmentCarrierSender:
    @pytest.mark.parametrize(
        ("lane", "query"), [("scan", None), ("filename", "ledger"), ("text", "bravo")]
    )
    def test_each_lane_keeps_only_the_carriers_attachments(self, carrier_db, lane, query):
        # The lane's word reaches every attachment without a sender.
        assert _ids(carrier_db.search_attachments(query=query)) == {"v1-att", "c1-att", "o1-att"}
        # from_addr selects the vendor's thread, colleague's reply included.
        assert _ids(carrier_db.search_attachments(query=query, from_addr="vendor@example.com")) == {
            "v1-att",
            "c1-att",
        }
        assert _ids(carrier_db.search_attachments(query=query, sender="vendor@example.com")) == {
            "v1-att"
        }
        assert _ids(carrier_db.search_attachments(query=query, sender="colleague@other.test")) == {
            "c1-att",
            "o1-att",
        }

    def test_lanes_are_separated_by_their_words(self, carrier_db):
        # The fixture's words reach one FTS lane each, so the lane test
        # above exercises the lane it names.
        assert carrier_db._attachment_text_lane('"ledger"', None, [], [], 10) == []
        assert carrier_db._attachment_filename_lane('"bravo"', [], [], 10) == []

    @pytest.mark.parametrize("query", [None, "ledger", "bravo"])
    def test_finds_a_match_behind_more_than_the_oversample_window(self, tmp_path, query):
        """The vendor's one attachment is older than more colleague
        attachments in its thread than the thread-level filter's
        candidate window holds: ``sender`` is SQL, so it still finds it;
        ``from_addr`` cannot tell them apart."""
        conn, path = _open_built_db_conn(tmp_path, "window.db")
        newer = _FILTERED_OVERSAMPLE + 2
        for i in range(newer):
            _add(conn, f"c{i}", "t", f"2024-02-{i + 1:02d}T00:00:00+00:00", COLLEAGUE)
        # Oldest, inserted last, and a longer filename and text, so it
        # ranks last in every lane (date order, then BM25).
        _add(
            conn,
            "v0",
            "t",
            "2024-01-01T00:00:00+00:00",
            VENDOR,
            filename="ledger-v0-with-a-much-longer-name.pdf",
            text="alpha bravo charlie delta echo foxtrot golf hotel",
        )
        conn.close()
        db = Database(str(path))
        assert _ids(db.search_attachments(query=query, sender="vendor@example.com", limit=1)) == {
            "v0-att"
        }
        assert _ids(
            db.search_attachments(query=query, from_addr="vendor@example.com", limit=1)
        ) != {"v0-att"}

    @pytest.mark.parametrize("query", [None, "ledger", "bravo"])
    def test_conjoins_with_the_other_filters(self, tmp_path, query):
        conn, path = _open_built_db_conn(tmp_path, "conjoin.db")
        _add(conn, "v1", "t", "2024-01-10T00:00:00+00:00", VENDOR)
        _add(conn, "v2", "t", "2024-06-10T00:00:00+00:00", VENDOR)
        _add(conn, "v3", "t", "2024-06-11T00:00:00+00:00", VENDOR, content_type="text/csv")
        _add(conn, "v4", "t", "2024-06-12T00:00:00+00:00", VENDOR, status="failed")
        _add(conn, "c1", "t", "2024-06-13T00:00:00+00:00", COLLEAGUE)
        conn.close()
        db = Database(str(path))

        def ids(**kw) -> set[str]:
            return _ids(db.search_attachments(query=query, sender="vendor@example.com", **kw))

        # v4's failed extraction has no text, so the text lane cannot see it.
        all_vendor = {"v1-att", "v2-att", "v3-att"} | ({"v4-att"} if query != "bravo" else set())
        assert ids() == all_vendor
        assert ids(date_from="2024-03-01") == all_vendor - {"v1-att"}
        assert ids(date_to="2024-03-01") == {"v1-att"}
        assert ids(content_type="text/csv") == {"v3-att"}
        assert ids(extracted_only=True) == {"v1-att", "v2-att", "v3-att"}
        assert ids(from_addr="colleague@other.test") == all_vendor
        assert ids(from_addr="nobody@example.com") == set()

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_blank_sender_is_no_filter(self, carrier_db, blank):
        assert _ids(carrier_db.search_attachments(sender=blank)) == {"v1-att", "c1-att", "o1-att"}

    def test_trash_stays_left_out(self, tmp_path):
        conn, path = _open_built_db_conn(tmp_path, "trash.db")
        _add(conn, "v1", "t", "2024-01-10T00:00:00+00:00", VENDOR)
        conn.execute("UPDATE message_occurrences SET folder = 'Trash'")
        conn.commit()
        conn.close()
        assert _ids(Database(str(path)).search_attachments(sender="vendor@example.com")) == set()


# Value shapes for the differential check against ``query_messages``:
# full addresses (exact), display forms, case, domains and name
# fragments (substring), Unicode casefolding, padding, a value that
# makes ``parseaddr`` recurse, an over-long value and a non-match.
_SHAPES = [
    "vendor@example.com",
    "VENDOR@Example.COM",
    "Vendor Person <vendor@example.com>",
    "  vendor@example.com  ",
    "@example.com",
    "example.com",
    "vendor",
    "Person",
    "person",
    "STRASSE",
    "straße",
    "@other.test",
    "colleague",
    "(" * 2000 + "a@example.com",
    "x" * 20000,
    "nobody@nowhere.test",
]


@pytest.fixture
def shapes_db(tmp_path) -> Database:
    """Senders over every ``sender_ambiguous`` state (0, 1, NULL), one
    attachment per message, spread over two threads."""
    conn, path = _open_built_db_conn(tmp_path, "shapes.db")
    rows = [
        ("s0", "t1", VENDOR, 0),
        ("s1", "t1", "vendor@example.com", 1),
        ("s2", "t2", VENDOR, None),
        ("s3", "t2", COLLEAGUE, 0),
        ("s4", "t1", "Büro Straße <office@example.com>", 0),
        ("s5", "t2", COLLEAGUE, 1),
        ("s6", "t1", COLLEAGUE, None),
    ]
    for i, (message_id, thread_id, from_, ambiguous) in enumerate(rows):
        _add(
            conn,
            message_id,
            thread_id,
            f"2024-01-{i + 1:02d}T00:00:00+00:00",
            from_,
            sender_ambiguous=ambiguous,
        )
    conn.close()
    return Database(str(path))


@pytest.mark.parametrize("value", _SHAPES)
@pytest.mark.parametrize("query", [None, "ledger", "bravo"])
def test_matches_exactly_what_query_messages_sender_matches(shapes_db, value, query):
    """The carrying messages ``sender`` keeps are exactly the messages
    ``query_messages(sender=...)`` matches, whatever the value's shape
    or the message's ``sender_ambiguous``: one compiled leaf, one rule.
    A carrying message the leaf cannot decide (``sender_ambiguous`` 1
    or NULL, #1153) is in neither."""
    page = shapes_db.query_messages(sender=value, limit=100)
    expected = {m.claimant_id for m in page.messages}
    found = {a.claimant_id for a in shapes_db.search_attachments(query=query, sender=value)}
    assert found == expected


def test_the_differential_catalogue_covers_both_match_modes(shapes_db):
    """Guard the catalogue: exact and substring values both match some
    message, a value matches none, and the exact vendor address also
    has messages the leaf cannot decide (``sender_ambiguous`` 1 or NULL,
    #1153), which neither tool returns."""
    matched = {
        value: {m.claimant_id for m in shapes_db.query_messages(sender=value, limit=100).messages}
        for value in _SHAPES
    }
    assert matched["vendor@example.com"] == {claimant_of("s0")}
    assert matched["straße"] == {claimant_of("s4")}
    assert matched["colleague"] == {claimant_of("s3")}
    assert shapes_db.query_messages(sender="vendor@example.com").indeterminate == 4
    assert matched["nobody@nowhere.test"] == set()


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------


def _handler(fake_server, fake_embed, db):
    register_search_tools(fake_server, db, fake_embed)
    return fake_server.tools["search_attachments"]


def _real_server(db):
    from fastmcp import FastMCP

    from tests.conftest import FakeEmbedClient

    server = FastMCP("attachment-sender-test")
    register_search_tools(server, db, FakeEmbedClient())
    return server


class TestSearchAttachmentsSenderTool:
    def test_sender_passed_to_the_database(self, fake_server, fake_embed, carrier_db):
        out = asyncio.run(
            _handler(fake_server, fake_embed, carrier_db)(sender="vendor@example.com")
        )
        assert [r["attachment_id"] for r in out.structured_content["results"]] == ["v1-att"]

    def test_structured_output_keeps_thread_senders(self, fake_server, fake_embed, carrier_db):
        out = asyncio.run(
            _handler(fake_server, fake_embed, carrier_db)(sender="colleague@other.test")
        )
        rows = {r["attachment_id"]: r for r in out.structured_content["results"]}
        assert set(rows) == {"c1-att", "o1-att"}
        # ``senders`` is still the thread's: c1's thread lists the vendor too.
        assert rows["c1-att"]["senders"] == [VENDOR, COLLEAGUE]
        assert rows["c1-att"]["claimant_id"] == claimant_of("c1")

    def test_no_match_prose(self, fake_server, fake_embed, carrier_db):
        out = asyncio.run(
            _handler(fake_server, fake_embed, carrier_db)(sender="nobody@nowhere.test")
        )
        # With a sender the count is stated, 0 included (#1204).
        assert out.content[0].text.startswith("No attachments found.\nindeterminate: 0\n")
        assert out.structured_content["results"] == []

    def test_sender_value_never_reaches_logs(self, fake_server, fake_embed, carrier_db, caplog):
        assert "sender" not in _LOGGABLE_TOOL_PARAMS
        with caplog.at_level(logging.DEBUG):
            asyncio.run(_handler(fake_server, fake_embed, carrier_db)(sender=MARKER, query="bravo"))
        assert "tool=search_attachments" in caplog.text
        assert "'sender'" in caplog.text  # named in withheld=[...]
        assert MARKER not in caplog.text

    def test_sender_is_published_with_its_description(self, carrier_db):
        from tests.test_tool_annotations import _wire_tools

        schema = _wire_tools(_real_server(carrier_db))["search_attachments"]["inputSchema"]
        prop = schema["properties"]["sender"]
        assert {v.get("type") for v in prop["anyOf"]} == {"string", "null"}
        assert prop.get("default") is None
        assert "sender" not in schema.get("required", [])
        doc = " ".join(prop["description"].split())
        assert "message carrying the attachment" in doc
        assert "query_messages" in doc

    @pytest.mark.parametrize("value", [True, 7, 1.5, {"k": MARKER}, [MARKER]])
    def test_wrong_typed_sender_is_rejected_by_the_argument_model(self, carrier_db, caplog, value):
        from src.lib.argument_validation import ArgumentValidationLog

        server = _real_server(carrier_db)
        server.add_middleware(ArgumentValidationLog(server))

        async def run():
            async with Client(server) as client:
                return await client.call_tool_mcp("search_attachments", {"sender": value})

        with caplog.at_level(logging.INFO):
            result = asyncio.run(run())
        assert result.isError
        assert "rejected invalid argument: search_attachments.sender" in caplog.text
        assert MARKER not in caplog.text

    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            ({}, {"v1-att", "c1-att", "o1-att"}),
            ({"sender": None}, {"v1-att", "c1-att", "o1-att"}),
            ({"sender": ""}, {"v1-att", "c1-att", "o1-att"}),
            ({"sender": "vendor@example.com"}, {"v1-att"}),
            ({"sender": "x" * 20000}, set()),
        ],
    )
    def test_sender_wire_values(self, carrier_db, args, expected):
        server = _real_server(carrier_db)

        async def run():
            async with Client(server) as client:
                return await client.call_tool_mcp("search_attachments", args)

        result = asyncio.run(run())
        assert not result.isError
        assert {r["attachment_id"] for r in result.structuredContent["results"]} == expected


@pytest.mark.parametrize(
    "sender,mode,addresses",
    [
        ("vendor@example.com", "exact", ["vendor@example.com"]),
        ("Person", "substring", ["vendor@example.com"]),
        ("no-such-sender", "substring", []),
    ],
)
def test_sender_match_disclosure_is_scoped_to_returned_occurrences(
    carrier_db, fake_server, fake_embed, sender, mode, addresses
):
    register_search_tools(fake_server, carrier_db, fake_embed)
    out = asyncio.run(fake_server.tools["search_attachments"](sender=sender))
    match = out.structured_content["sender_match"]
    assert match == dict(
        mode=mode,
        scope="returned_occurrences",
        distinct_addresses=len(addresses),
        addresses=addresses,
    )
    assert "among returned attachment occurrences" in out.content[0].text


def test_sender_disclosure_caps_addresses_and_only_counts_the_returned_page(
    tmp_path, fake_server, fake_embed, caplog
):
    conn, path = _open_built_db_conn(tmp_path, "sender-disclosure.db")
    try:
        for i in range(20):
            _add(
                conn,
                f"shared{i}",
                f"t-shared{i}",
                f"2024-04-{i + 1:02}T00:00:00+00:00",
                f"Shared Person <p{i:02}@example.test>",
            )
    finally:
        conn.close()
    register_search_tools(fake_server, Database(str(path)), fake_embed)
    handler = fake_server.tools["search_attachments"]
    with caplog.at_level("INFO"):
        page = asyncio.run(handler(sender="Shared", limit=1))
        all_rows = asyncio.run(handler(sender="Shared", limit=50))
        unfiltered = asyncio.run(handler(limit=1))
    assert page.structured_content["sender_match"]["distinct_addresses"] == 1
    assert page.structured_content["sender_match"]["addresses"] == ["p19@example.test"]
    full = all_rows.structured_content["sender_match"]
    assert full["distinct_addresses"] == 20 and len(full["addresses"]) == 10
    assert unfiltered.structured_content["sender_match"] is None
    assert "p19@example.test" not in caplog.text
