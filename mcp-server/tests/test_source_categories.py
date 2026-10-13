"""#1569: each occurrence has a folder-derived source category and its
provenance.

The category is derived at read time from the occurrence's folder by
its account provider's policy (PLAN.md decision 45, #1317): ordinary
mailbox folders, unsent evidence (Drafts, Scheduled), Bridge-local
recovery mail (untrusted), and an explicit ``unknown`` for any folder,
or provider, the policy does not name. Recovery mail's identity headers
never count toward an ``authority_class``. Each occurrence carries how
and when it was observed, with its account. All data is synthetic.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import sqlite_vec
from src.lib.sources import category_of
from src.lib.sqlite import Database
from src.tools.retrieval import register_retrieval_tools

from tests.conftest import (
    TEST_ACCOUNT_SCOPE,
    FakeMCPServer,
    _build_schema,
    _insert_message,
    claimant_of,
)
from tests.test_authority import spam_db  # noqa: F401 - fixture

CASES = {
    "INBOX": "mailbox",
    "Sent": "mailbox",
    "Archive": "mailbox",
    "Spam": "mailbox",
    "Trash": "mailbox",
    "Folders/Clients": "mailbox",
    "Drafts": "unsent",
    "Scheduled": "unsent",
    "Recovered Messages": "recovered",
    "Mystery": "unknown",
    "Labels/Work": "unknown",
}


def _conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _mid(folder: str) -> str:
    return "m-" + folder.replace("/", "-").replace(" ", "-").lower()


@pytest.fixture
def folders(tmp_path) -> Database:
    """One message per folder in ``CASES``, each in its own thread."""
    path = tmp_path / "categories.db"
    conn = _conn(path)
    _build_schema(conn)
    for folder in CASES:
        _insert_message(
            conn,
            message_id=_mid(folder),
            thread_id=f"t-{_mid(folder)}",
            sent_at="2024-01-01T00:00:00+00:00",
            folder=folder,
            from_=["alice@example.com"],
        )
    conn.execute(
        "UPDATE message_occurrences SET provenance = 'scan', "
        "first_observed_at = '2024-02-01T00:00:00+00:00', "
        "last_observed_at = '2024-02-03T00:00:00+00:00' WHERE claimant_id = ?",
        (claimant_of(_mid("Drafts")),),
    )
    conn.commit()
    conn.close()
    return Database(str(path))


@pytest.mark.parametrize(("folder", "category"), sorted(CASES.items()))
def test_each_folder_maps_to_its_category(folders, folder, category):
    page = folders.query_messages(folder=folder)
    (message,) = page.messages
    (copy,) = message.occurrences
    assert copy.category == category


def test_the_policy_is_the_providers():
    assert category_of("proton-bridge", "Drafts") == "unsent"
    # Another provider never inherits Proton's names.
    assert category_of("other-provider", "Drafts") == "unknown"
    assert category_of("other-provider", "INBOX") == "unknown"


def test_a_prefix_needs_a_child_folder():
    assert category_of("proton-bridge", "Folders") == "unknown"
    assert category_of("proton-bridge", "FoldersX/a") == "unknown"


def test_another_providers_account_reads_unknown(folders):
    with closing(_conn(Path(folders.path))) as conn:
        conn.execute(
            "INSERT INTO accounts VALUES (2, '00000000-0000-4000-8000-000000000002', "
            "'other-provider', '2024-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "UPDATE message_occurrences SET account_id = 2 WHERE claimant_id = ?",
            (claimant_of(_mid("INBOX")),),
        )
        conn.commit()
    (message,) = folders.query_messages(folder="INBOX").messages
    assert [(o.provider, o.category) for o in message.occurrences] == [
        ("other-provider", "unknown")
    ]


def test_occurrences_carry_their_provenance(folders):
    (message,) = folders.query_messages(folder="Drafts").messages
    (copy,) = message.occurrences
    assert (copy.account_scope, copy.provider) == (TEST_ACCOUNT_SCOPE, "proton-bridge")
    assert copy.provenance == "scan"
    assert copy.first_observed_at == "2024-02-01T00:00:00+00:00"
    assert copy.last_observed_at == "2024-02-03T00:00:00+00:00"


def test_tools_serve_the_category_and_provenance(folders):
    server = FakeMCPServer()
    register_retrieval_tools(server, folders)
    out = asyncio.run(server.tools["get_message"](message_id=_mid("Drafts")))
    message = out.structured_content["message"]
    (copy,) = message["occurrences"]
    assert copy["category"] == "unsent"
    assert copy["provenance"] == "scan"
    assert copy["first_observed_at"] == "2024-02-01T00:00:00+00:00"
    assert copy["last_observed_at"] == "2024-02-03T00:00:00+00:00"
    assert "Folder: Drafts (unsent)" in out.content[0].text
    out = asyncio.run(server.tools["get_message"](message_id=_mid("Recovered Messages")))
    assert "Folder: Recovered Messages (recovered, untrusted)" in out.content[0].text
    out = asyncio.run(server.tools["get_message"](message_id=_mid("INBOX")))
    assert "Folder: INBOX\n" in out.content[0].text
    rows = asyncio.run(server.tools["query_messages"](folder="Scheduled"))
    assert "Scheduled (unsent)" in rows.content[0].text


class TestRecoveredIsNeverAuthority:
    """#1317: Recovered Messages headers are client-supplied, so a
    recovery copy never counts toward an ``authority_class``, like Spam."""

    def _recover(self, db: Database, message_id: str) -> None:
        with closing(sqlite3.connect(db.path)) as conn:
            conn.execute(
                "UPDATE message_occurrences SET folder = 'Recovered Messages' "
                "WHERE claimant_id = ?",
                (claimant_of(message_id),),
            )
            conn.commit()

    def test_query_messages(self, spam_db):  # noqa: F811
        self._recover(spam_db, "mixed-2")
        page = spam_db.query_messages(authority_class="counsel")
        assert sorted(m.message_id for m in page.messages) == ["t-inbox"]
        # Still searchable without the filter.
        assert spam_db.query_messages(folder="Recovered Messages").total_matches == 1

    def test_aggregate_by_authority_class(self, spam_db):  # noqa: F811
        self._recover(spam_db, "mixed-2")
        page = spam_db.aggregate_messages(group_by="authority_class", sender="firm.example")
        groups = {g.value: g.messages for g in page.groups}
        assert groups.get("counsel") == 1
