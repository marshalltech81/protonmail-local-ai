"""
``from_name`` resolution reports the resolved address and how many
senders matched (#864).

Every tool that takes ``from_name`` resolves it through ``find_contact``
to the most active matching sender. When several senders share the
name, the structured output says so: ``resolved_from_addr`` is the
address filtered by and ``from_name_matches`` the number of distinct
sender addresses the name matched, counted up to
``MAX_FROM_NAME_MATCHES``. Nothing about the values is logged. All
data is synthetic.
"""

import asyncio
import logging
import sqlite3

import pytest
import sqlite_vec
from src.lib.sqlite import Database
from src.tools.intelligence import (
    FromNameResolution,
    register_intelligence_tools,
    resolve_from_name,
)
from src.tools.outputs import MAX_FROM_NAME_MATCHES
from src.tools.search import register_search_tools

from tests.conftest import (
    FakeEmbedClient,
    FakeInferenceClient,
    FakeMCPServer,
    _build_schema,
    _insert_chunk,
    _insert_thread,
)
from tests.test_tool_annotations import _server, _wire_tools

# The address of the busier "Jordan" carries a marker the log must not
# show: the address is read from mail, not from the caller.
_ADDRESS_MARKER = "q7zmarker"
_VALE = "Jordan Vale <vale@inbox.example>"
_REED = f"Jordan Reed <reed@{_ADDRESS_MARKER}.example>"
_REED_ADDR = f"reed@{_ADDRESS_MARKER}.example"

FROM_NAME_TOOLS = ["search_emails", "get_evidence", "ask_mailbox", "extract_from_emails"]


def _db(tmp_path, threads: list[tuple[str, str, str]]) -> Database:
    """A mailbox of ``(thread_id, sender, folder)`` threads, each one
    invoice thread the sender sent to the same recipient."""
    db_path = tmp_path / "from-name.db"
    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _build_schema(conn)
    for tid, sender, folder in threads:
        _insert_thread(
            conn,
            thread_id=tid,
            subject=f"invoice {tid}",
            participants=[sender, "lee@home.example"],
            senders=[sender],
            folder=folder,
            body_text="the invoice for may",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        _insert_chunk(
            conn,
            chunk_id=f"{tid}-c0",
            message_id=tid,
            thread_id=tid,
            text="the invoice for may",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
    conn.close()
    return Database(str(db_path))


def _two_jordans(tmp_path) -> Database:
    """Two senders match "Jordan": Vale with one thread, Reed with two."""
    return _db(
        tmp_path,
        [("t-vale", _VALE, "INBOX"), ("t-reed-1", _REED, "INBOX"), ("t-reed-2", _REED, "INBOX")],
    )


def _call(db: Database, tool: str, **kwargs):
    server = FakeMCPServer()
    embed, inference = FakeEmbedClient(), FakeInferenceClient(response="null")
    register_search_tools(server, db, embed)
    register_intelligence_tools(server, db, embed, inference)
    if tool == "search_emails":
        args: dict = {"query": "invoice", "mode": "keyword"}
    elif tool == "get_evidence":
        args = {"query": "invoice"}
    elif tool == "ask_mailbox":
        args = {"question": "invoice"}
    else:
        args = {"query": "invoice", "schema": {"amount": "string"}}
    return asyncio.run(server.tools[tool](**args, **kwargs)).structured_content


def _report(out) -> tuple[str | None, int | None]:
    return out["resolved_from_addr"], out["from_name_matches"]


def test_every_tool_taking_from_name_reports_the_resolution(empty_db):
    """Derived from the registered tools: each tool whose input schema
    has ``from_name`` reports both fields in its output schema, and
    they are the four tools that resolve it (no exclusions)."""
    tools = _wire_tools(_server(empty_db))
    taking = {name for name, t in tools.items() if "from_name" in t["inputSchema"]["properties"]}
    assert taking == set(FROM_NAME_TOOLS)
    for name in sorted(taking):
        fields = set(tools[name]["outputSchema"]["properties"])
        assert {"resolved_from_addr", "from_name_matches"} <= fields, name


@pytest.mark.parametrize("tool", FROM_NAME_TOOLS)
class TestReport:
    def test_several_senders_report_the_most_active_and_the_count(self, tmp_path, tool, caplog):
        with caplog.at_level(logging.DEBUG):
            out = _call(_two_jordans(tmp_path), tool, from_name="Jordan")
        assert _report(out) == (_REED_ADDR, 2)
        assert {
            t["thread_id"] for t in out["threads" if tool != "search_emails" else "results"]
        } == {
            "t-reed-1",
            "t-reed-2",
        }
        assert _ADDRESS_MARKER not in caplog.text

    def test_one_sender_reports_one(self, tmp_path, tool):
        out = _call(_two_jordans(tmp_path), tool, from_name="Vale")
        assert _report(out) == ("vale@inbox.example", 1)

    def test_no_contact_reports_zero(self, tmp_path, tool):
        out = _call(_two_jordans(tmp_path), tool, from_name="zzznosuchcontact")
        assert _report(out) == (None, 0)
        assert out["threads" if tool != "search_emails" else "results"] == []

    def test_without_from_name_both_are_null(self, tmp_path, tool):
        out = _call(_two_jordans(tmp_path), tool)
        assert _report(out) == (None, None)

    def test_count_follows_the_folder_scope(self, tmp_path, tool):
        db = _db(
            tmp_path,
            [
                ("t-vale", _VALE, "INBOX"),
                ("t-reed-1", _REED, "Trash"),
                ("t-reed-2", _REED, "Trash"),
            ],
        )
        assert _report(_call(db, tool, from_name="Jordan")) == ("vale@inbox.example", 1)
        out = _call(db, tool, from_name="Jordan", folders=["Trash"])
        assert _report(out) == (_REED_ADDR, 1)

    def test_count_saturates_at_the_cap(self, tmp_path, tool):
        many = [
            (f"t-{n:02d}", f"Jordan {n:02d} <jordan{n:02d}@inbox.example>", "INBOX")
            for n in range(MAX_FROM_NAME_MATCHES + 1)
        ]
        out = _call(_db(tmp_path, many), tool, from_name="Jordan")
        assert _report(out) == ("jordan00@inbox.example", MAX_FROM_NAME_MATCHES)


@pytest.mark.parametrize("tool", ["search_emails", "get_evidence", "ask_mailbox"])
def test_an_explicit_from_addr_leaves_from_name_unused(tmp_path, tool):
    out = _call(_two_jordans(tmp_path), tool, from_name="Jordan", from_addr="vale@inbox.example")
    assert _report(out) == (None, None)


class TestResolveFromName:
    def test_counts_senders_up_to_the_cap_in_one_lookup(self, tmp_path):
        db = _two_jordans(tmp_path)
        lookups: list = []
        original = db.find_contact

        def spy(query, limit, *, senders_only=False, folders=None):
            lookups.append((query, limit, senders_only, folders))
            return original(query, limit, senders_only=senders_only, folders=folders)

        db.find_contact = spy  # type: ignore[method-assign]
        resolution = asyncio.run(resolve_from_name(db, "Jordan", ["INBOX"]))
        assert resolution == FromNameResolution(address=_REED_ADDR, senders=2)
        assert lookups == [("Jordan", MAX_FROM_NAME_MATCHES, True, ["INBOX"])]

    def test_no_match(self, tmp_path):
        resolution = asyncio.run(resolve_from_name(_two_jordans(tmp_path), "nobody", None))
        assert resolution == FromNameResolution(address=None, senders=0)
