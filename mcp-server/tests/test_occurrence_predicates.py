"""#1568: folder, flag and Trash filters match within one occurrence.

Each Maildir copy of a message is its own occurrence (#1566). Folder,
flag, Trash and Spam terms of one query are conjoined on a single copy;
results are one row per message, counted and paged without duplicates;
the default scope is an active copy outside Trash, counting only the
live (not ``T``-flagged) copies when the message has one (owner decision
on #1568, option B), the Trash role dispatched by the copy's account
provider; outputs
list the copies with their account and label the single folder and flags
shown as the representative copy's. All data is synthetic.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from pathlib import Path

import pytest
import sqlite_vec
from src.lib.sqlite import Database
from src.tools.retrieval import register_retrieval_tools
from src.tools.search import register_search_tools

from tests.conftest import (
    TEST_ACCOUNT_SCOPE,
    FakeEmbedClient,
    FakeMCPServer,
    _build_schema,
    _insert_attachment,
    _insert_message,
    claimant_of,
    insert_occurrence,
)

MARKER = "SYNTHETIC_OCCURRENCE_MARKER"


def _conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


@pytest.fixture
def mailbox(tmp_path) -> Database:
    """``two``: an INBOX copy (read) and an Archive copy (flagged).
    ``trash_only``: a live Trash copy. ``deleted``: a ``T``-flagged INBOX
    copy and a live Trash copy. ``gone``: one retired copy. ``plain``: one
    INBOX copy. Each in its own thread."""
    path = tmp_path / "occ.db"
    conn = _conn(path)
    _build_schema(conn)
    for mid, folder in (
        ("two", "INBOX"),
        ("trash_only", "Trash"),
        ("deleted", "INBOX"),
        ("gone", "INBOX"),
        ("plain", "INBOX"),
    ):
        _insert_message(
            conn,
            message_id=mid,
            thread_id=f"t-{mid}",
            sent_at="2024-01-01T00:00:00+00:00",
            subject=f"{MARKER} invoice {mid}",
            folder=folder,
            from_=["alice@example.com"],
            body=f"invoice {mid}",
            seen=mid == "two",
        )
    insert_occurrence(conn, claimant_of("two"), "Archive", "Archive/cur/two-2", flagged=True)
    conn.execute(
        "UPDATE message_occurrences SET trashed = 1 WHERE claimant_id = ?",
        (claimant_of("deleted"),),
    )
    insert_occurrence(conn, claimant_of("deleted"), "Trash", "Trash/cur/deleted-2")
    conn.execute(
        "UPDATE message_occurrences SET state = 'retired', "
        "retired_at = '2024-02-01T00:00:00+00:00' WHERE claimant_id = ?",
        (claimant_of("gone"),),
    )
    conn.commit()
    conn.close()
    return Database(str(path))


def _ids(page) -> list[str]:
    return sorted(m.message_id for m in page.messages)


class TestOneCopy:
    def test_folder_and_flag_match_the_same_copy(self, mailbox):
        assert _ids(mailbox.query_messages(folder="Archive", flagged=True)) == ["two"]
        assert _ids(mailbox.query_messages(folder="INBOX", flagged=True)) == []
        assert _ids(mailbox.query_messages(folder="INBOX", seen=True)) == ["two"]
        assert _ids(mailbox.query_messages(folder="Archive", seen=True)) == []

    def test_a_message_with_two_matching_copies_counts_and_pages_once(self, mailbox):
        page = mailbox.query_messages()
        assert page.total_matches == len(page.messages) == 2
        assert _ids(page) == ["plain", "two"]
        first = mailbox.query_messages(limit=1)
        second = mailbox.query_messages(limit=1, cursor=first.next_cursor)
        assert sorted(m.message_id for m in first.messages + second.messages) == ["plain", "two"]
        assert second.has_more is False

    def test_flag_filters_without_a_folder_use_a_default_scope_copy(self, mailbox):
        assert _ids(mailbox.query_messages(flagged=True)) == ["two"]
        assert _ids(mailbox.query_messages(seen=False)) == ["plain", "two"]


class TestDefaultScope:
    def test_a_trash_only_live_copy_is_hidden_until_trash_is_named(self, mailbox):
        assert "trash_only" not in _ids(mailbox.query_messages())
        assert _ids(mailbox.query_messages(folder="Trash")) == ["deleted", "trash_only"]

    def test_a_trashed_inbox_copy_beside_a_trash_copy_is_hidden(self, mailbox):
        assert "deleted" not in _ids(mailbox.query_messages())
        # The T-flagged copy is still an active INBOX copy.
        assert "deleted" in _ids(mailbox.query_messages(folder="INBOX"))

    def test_a_retired_copy_never_matches(self, mailbox):
        assert "gone" not in _ids(mailbox.query_messages())
        assert "gone" not in _ids(mailbox.query_messages(folder="INBOX"))

    def test_search_leaves_out_threads_with_no_default_scope_copy(self, mailbox):
        found = {r.thread_id for r in mailbox.keyword_search("invoice", limit=20)}
        assert found == {"t-two", "t-plain"}
        found = {
            r.thread_id for r in mailbox.keyword_search("invoice", limit=20, folders=["Trash"])
        }
        assert found == {"t-trash_only", "t-deleted"}

    def test_trash_is_the_providers_role(self, tmp_path, mailbox):
        """A copy in a folder named Trash under an account whose provider
        has no such role stays in the default scope."""
        conn = _conn(Path(mailbox.path))
        conn.execute(
            "INSERT INTO accounts VALUES (2, '00000000-0000-4000-8000-000000000002', "
            "'other-provider', '2024-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "UPDATE message_occurrences SET account_id = 2 WHERE claimant_id = ?",
            (claimant_of("trash_only"),),
        )
        conn.commit()
        conn.close()
        assert "trash_only" in _ids(mailbox.query_messages())


class TestFoldersAndGroups:
    def test_list_folders_counts_threads_with_an_active_copy(self, mailbox):
        assert {f["name"]: f["thread_count"] for f in mailbox.list_folders()} == {
            "INBOX": 3,
            "Archive": 1,
            "Trash": 2,
        }

    def test_list_threads_flags_belong_to_the_listed_folders_copy(self, mailbox):
        assert {t.thread_id for t in mailbox.list_threads("Archive", "flagged")} == {"t-two"}
        assert {t.thread_id for t in mailbox.list_threads("INBOX", "flagged")} == set()
        assert {t.thread_id for t in mailbox.list_threads("INBOX", "unread")} == {
            "t-plain",
            "t-deleted",
        }

    def test_group_by_folder_counts_each_matched_copys_folder(self, mailbox):
        page = mailbox.aggregate_messages(group_by="folder")
        assert page.total_matches == 2
        assert {g.value: g.messages for g in page.groups} == {"INBOX": 2, "Archive": 1}
        page = mailbox.aggregate_messages(group_by="folder", flagged=True)
        assert {g.value: g.messages for g in page.groups} == {"Archive": 1}

    def test_where_leaves_combine_with_the_occurrence_terms(self, mailbox):
        from src.lib.predicates import Where, WhereLeafItem, normalize_where

        where = normalize_where(
            Where(all=[WhereLeafItem(leaf="subject_contains", value="invoice two")])
        )
        assert _ids(mailbox.query_messages(where=where, folder="Archive")) == ["two"]
        assert _ids(mailbox.query_messages(where=where, folder="Trash")) == []


class TestOutputs:
    def test_rows_list_every_copy_with_its_account(self, mailbox):
        (two,) = [m for m in mailbox.query_messages().messages if m.message_id == "two"]
        assert two.occurrence_count == 2
        assert [(o.folder, o.flagged, o.seen) for o in two.occurrences] == [
            ("INBOX", False, True),
            ("Archive", True, False),
        ]
        assert {(o.account_scope, o.provider) for o in two.occurrences} == {
            (TEST_ACCOUNT_SCOPE, "proton-bridge")
        }
        # The representative: the first live copy outside Trash.
        assert (two.folder, two.seen, two.flagged) == ("INBOX", True, False)
        assert two.source_file is not None
        assert two.source_file.locator == "INBOX/cur/two"

    def test_the_representative_prefers_a_live_copy_outside_trash(self, mailbox):
        (deleted,) = [
            m for m in mailbox.query_messages(folder="Trash").messages if m.message_id == "deleted"
        ]
        assert deleted.folder == "Trash"
        assert [(o.folder, o.trashed) for o in deleted.occurrences] == [
            ("Trash", False),
            ("INBOX", True),
        ]

    def test_tools_serve_the_copies_and_label_the_representative(self, mailbox):
        server = FakeMCPServer()
        register_retrieval_tools(server, mailbox)
        register_search_tools(server, mailbox, FakeEmbedClient())
        out = asyncio.run(server.tools["get_message"](message_id="two"))
        message = out.structured_content["message"]
        assert message["folder"] == "INBOX"
        assert message["occurrence_count"] == 2
        assert [o["folder"] for o in message["occurrences"]] == ["INBOX", "Archive"]
        assert message["occurrences"][1]["account_scope"] == TEST_ACCOUNT_SCOPE
        text = out.content[0].text
        assert "Folder: INBOX (representative of 2 copies)" in text
        assert "Copies: INBOX (read); Archive (unread, flagged)" in text
        rows = asyncio.run(server.tools["query_messages"]())
        assert "INBOX (+1 copies)" in rows.content[0].text


# -- Owner decision on #1568 (2026-10-13), option B --------------------------
# A message is in the default scope when it has an active copy outside its
# provider's Trash role, considering its non-T active copies if it has any,
# otherwise all its active copies.


def _tombstone(conn: sqlite3.Connection, claimant: str) -> None:
    (occurrence_id,) = conn.execute(
        "SELECT id FROM message_occurrences WHERE claimant_id = ? ORDER BY id LIMIT 1",
        (claimant,),
    ).fetchone()
    conn.execute(
        "INSERT INTO pending_deletions (occurrence_id, claimant_id, marked_at) "
        "VALUES (?, ?, '2024-03-01T00:00:00+00:00')",
        (occurrence_id, claimant),
    )


def _trash_all(conn: sqlite3.Connection, claimant: str) -> None:
    conn.execute("UPDATE message_occurrences SET trashed = 1 WHERE claimant_id = ?", (claimant,))


@pytest.fixture
def scoped(tmp_path) -> Database:
    """One thread per case, sender ``<case>@example.com``:

    - ``t_mirror``: only a T-flagged INBOX copy, tombstoned (mirror mode).
    - ``t_archive``: only a T-flagged INBOX copy, no tombstone (archive).
    - ``t_inherited``: only a T-flagged INBOX copy with a tombstone left by
      an earlier mirror-mode run (archive mode keeps it).
    - ``t_two``: T-flagged INBOX and Archive copies.
    - ``t_pair``: a T-flagged INBOX copy and a live Trash copy.
    - ``sep`` (two claimants in thread ``t-sep``): variant ``""`` has a
      T-flagged INBOX copy, variant ``b`` a live Trash copy.
    - ``t_in_trash``: only a T-flagged Trash copy.
    - ``flag_on_t``: a live unflagged INBOX copy and a T-flagged, flagged
      Archive copy.
    - ``gone``: one retired copy.
    """
    path = tmp_path / "scoped.db"
    conn = _conn(path)
    _build_schema(conn)
    cases = [
        ("t_mirror", "", "INBOX"),
        ("t_archive", "", "INBOX"),
        ("t_inherited", "", "INBOX"),
        ("t_two", "", "INBOX"),
        ("t_pair", "", "INBOX"),
        ("sep", "", "INBOX"),
        ("sep", "b", "Trash"),
        ("t_in_trash", "", "Trash"),
        ("flag_on_t", "", "INBOX"),
        ("gone", "", "INBOX"),
    ]
    for mid, variant, folder in cases:
        _insert_message(
            conn,
            message_id=mid,
            variant=variant,
            thread_id=f"t-{mid}",
            sent_at="2024-01-01T00:00:00+00:00",
            subject=f"{MARKER} invoice {mid}{variant}",
            folder=folder,
            from_=[f"{mid}{variant}@example.com"],
            body=f"invoice {mid}",
            has_attachments=mid in ("t_archive", "t_pair"),
        )
    for mid in ("t_mirror", "t_archive", "t_inherited", "t_two", "t_pair", "t_in_trash"):
        _trash_all(conn, claimant_of(mid))
    _trash_all(conn, claimant_of("sep"))
    insert_occurrence(conn, claimant_of("t_two"), "Archive", "Archive/cur/t_two-2", trashed=True)
    insert_occurrence(conn, claimant_of("t_pair"), "Trash", "Trash/cur/t_pair-2")
    insert_occurrence(
        conn,
        claimant_of("flag_on_t"),
        "Archive",
        "Archive/cur/flag_on_t-2",
        trashed=True,
        flagged=True,
    )
    _tombstone(conn, claimant_of("t_mirror"))
    _tombstone(conn, claimant_of("t_inherited"))
    conn.execute(
        "UPDATE message_occurrences SET state = 'retired', "
        "retired_at = '2024-02-01T00:00:00+00:00' WHERE claimant_id = ?",
        (claimant_of("gone"),),
    )
    for mid in ("t_archive", "t_pair"):
        _insert_attachment(
            conn,
            message_id=mid,
            thread_id=f"t-{mid}",
            attachment_id=f"att-{mid}",
            filename=f"invoice-{mid}.pdf",
        )
    conn.commit()
    conn.close()
    return Database(str(path))


def _keys(page) -> list[str]:
    """``message_id`` plus ``/b`` for the second ``sep`` claimant."""
    return sorted(
        m.message_id + ("/b" if m.claimant_id == claimant_of(m.message_id, "b") else "")
        for m in page.messages
    )


_DEFAULT = ["flag_on_t", "sep", "t_archive", "t_inherited", "t_mirror", "t_two"]


class TestTFlaggedFallback:
    def test_only_t_flagged_copies_outside_trash_stay_listed_in_both_modes(self, scoped):
        page = scoped.query_messages()
        assert _keys(page) == _DEFAULT
        pending = {m.message_id: m.pending_deletion for m in page.messages}
        assert pending["t_mirror"] is True
        assert pending["t_inherited"] is True
        assert pending["t_archive"] is False

    def test_a_t_inbox_copy_beside_a_live_trash_copy_is_hidden_until_trash_is_named(self, scoped):
        assert "t_pair" not in _keys(scoped.query_messages())
        assert "t_pair" in _keys(scoped.query_messages(folder="Trash"))

    def test_the_same_pair_as_separate_claimants_lists_the_t_inbox_copy(self, scoped):
        assert "sep" in _keys(scoped.query_messages())
        assert "sep/b" not in _keys(scoped.query_messages())
        assert "sep" in _keys(scoped.query_messages(folder="INBOX"))

    def test_trash_only_and_copyless_messages_stay_hidden(self, scoped):
        keys = _keys(scoped.query_messages())
        assert "t_in_trash" not in keys
        assert "gone" not in keys

    def test_a_flag_only_on_a_t_copy_does_not_open_the_fallback(self, scoped):
        assert _keys(scoped.query_messages(flagged=True)) == []
        # A named folder matches the T-flagged copy itself.
        assert _keys(scoped.query_messages(folder="Archive", flagged=True)) == ["flag_on_t"]

    def test_t_only_copies_count_and_page_once(self, scoped):
        page = scoped.query_messages()
        assert page.total_matches == len(page.messages) == len(_DEFAULT)
        seen: list[str] = []
        cursor = None
        while True:
            step = scoped.query_messages(limit=2, cursor=cursor)
            seen += _keys(step)
            if not step.has_more:
                break
            cursor = step.next_cursor
        assert sorted(seen) == _DEFAULT

    def test_trash_is_the_providers_role_for_the_fallback(self, scoped):
        conn = _conn(Path(scoped.path))
        conn.execute(
            "INSERT INTO accounts VALUES (2, '00000000-0000-4000-8000-000000000002', "
            "'other-provider', '2024-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "UPDATE message_occurrences SET account_id = 2 WHERE claimant_id = ?",
            (claimant_of("t_in_trash"),),
        )
        conn.commit()
        conn.close()
        assert "t_in_trash" in _keys(scoped.query_messages())


class TestEveryDefaultScopeCaller:
    """Each default-scoped reader applies the same membership, in its own
    counting unit."""

    def test_query_threads(self, scoped):
        page = scoped.query_threads()
        assert sorted(t.thread_id for t in page.threads) == sorted(f"t-{m}" for m in _DEFAULT)
        assert page.total_threads == len(_DEFAULT)
        assert page.total_matching_messages == len(_DEFAULT)

    def test_aggregate_messages(self, scoped):
        page = scoped.aggregate_messages(group_by="folder")
        assert page.total_matches == len(_DEFAULT)
        assert {g.value: g.messages for g in page.groups} == {"INBOX": 6, "Archive": 1}

    def test_query_attachments(self, scoped):
        page = scoped.query_attachments()
        assert [a.claimant_id for a in page.attachments] == [claimant_of("t_archive")]
        assert page.total_matches == 1
        page = scoped.query_attachments(folder="Trash")
        assert [a.claimant_id for a in page.attachments] == [claimant_of("t_pair")]

    def test_search_attachments(self, scoped):
        found = {r.thread_id for r in scoped.search_attachments()}
        assert found == {"t-t_archive"}

    def test_thread_search(self, scoped):
        found = {r.thread_id for r in scoped.keyword_search("invoice", limit=50)}
        assert found == {f"t-{m}" for m in _DEFAULT}

    def test_sender_resolution(self, scoped):
        emails = {c["email"] for c in scoped.find_contact("@example.com", 50, senders_only=True)}
        # Thread membership: ``sepb`` (live Trash copy) shares thread t-sep
        # with ``sep``, which is in the default scope.
        assert emails == {f"{m}@example.com" for m in _DEFAULT} | {"sepb@example.com"}


def test_every_default_scope_site_uses_the_shared_helper():
    """Derived from the code: every SQL fragment in ``src/`` that names the
    Trash role or the T flag lives in ``sources``; outside it, a reader
    that applies the default scope does so through ``default_scope_sql``
    (the call sites found here), and no other module builds its own."""
    import ast as _ast

    src = Path(__file__).resolve().parents[1] / "src"
    builders: dict[str, list[str]] = {}
    for path in sorted(src.rglob("*.py")):
        if path.name == "sources.py":
            continue
        tree = _ast.parse(path.read_text())
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Constant) and isinstance(node.value, str):
                text = node.value
                # A condition on an occurrence's T flag (``o.trashed = 0``).
                assert not re.search(r"\w\.trashed\s*(=|<>|!=|IS\b)", text), path.name
            if (
                isinstance(node, _ast.Call)
                and isinstance(node.func, _ast.Name)
                and node.func.id == "role_sql"
                and node.args
                and isinstance(node.args[0], _ast.Constant)
            ):
                assert node.args[0].value != "trash", path.name
            if (
                isinstance(node, _ast.Call)
                and isinstance(node.func, _ast.Name)
                and node.func.id == "default_scope_sql"
            ):
                builders.setdefault(path.name, []).append(str(node.lineno))
    # The readers the owner decision names reach the scope through these
    # modules: the predicates' default_scope leaf (query_messages,
    # query_threads, aggregate_messages, query_attachments) and sqlite's
    # search folder membership, its DefaultFolderScope lookup (thread
    # search, sender resolution) and the attachment search default.
    assert set(builders) == {"predicates.py", "sqlite.py"}
