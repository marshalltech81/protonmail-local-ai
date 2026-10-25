"""Scope declarations are reported through both MCP and make status."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from src.tools.system import _mailbox_status, _render, get_mailbox_status

from tests.conftest import write_ingestion


@pytest.mark.parametrize("declared", ["combined", "split", "unverified"])
def test_declarations_never_certify_or_block_currency(seeded_db, monkeypatch, declared):
    now = datetime.now(UTC).isoformat()
    write_ingestion(
        seeded_db.path, sync_completed_at=now, sync_interval_secs=60, indexer_seen_at=now
    )
    with closing(sqlite3.connect(seeded_db.path)) as conn:
        conn.execute(
            "INSERT INTO account_address_scope VALUES (1, ?, ?, ?)", (declared, "unverified", now)
        )
        conn.commit()
    monkeypatch.setenv("SQLITE_PATH", seeded_db.path)
    out = get_mailbox_status()
    assert out["status"] == "ok"
    assert out["current"] is True and out["ingestion_caught_up"] is True
    assert out["upstream_currency"] == "unknown"
    assert out["address_scope"] == {
        "declared": declared,
        "previous_declared": "unverified",
        "changed_at": now.replace("+00:00", "Z"),
    }
    rendered = _render(_mailbox_status(seeded_db, rerank_mode="none", rerank_candidates=None))
    assert f"Bridge address scope (declared, not observed): {declared}" in rendered
    assert "Changed from unverified" in rendered


def test_unrecorded_declaration_is_unverified(empty_db, monkeypatch):
    monkeypatch.setenv("SQLITE_PATH", empty_db.path)
    assert get_mailbox_status()["address_scope"] == {
        "declared": "unverified",
        "previous_declared": None,
        "changed_at": None,
    }


def test_served_folder_description_limits_its_coverage(empty_db):
    import asyncio

    from fastmcp import Client, FastMCP
    from src.tools.retrieval import register_retrieval_tools

    server = FastMCP("scope-description-test")
    register_retrieval_tools(server, empty_db)

    async def described():
        async with Client(server) as client:
            return next(
                t.description for t in await client.list_tools() if t.name == "list_folders"
            )

    description = " ".join(asyncio.run(described()).split())
    assert "indexed mail" in description
    assert "indexed-folder coverage" in description
    assert (
        "never evidence of Bridge address scope or complete collection from Proton" in description
    )
