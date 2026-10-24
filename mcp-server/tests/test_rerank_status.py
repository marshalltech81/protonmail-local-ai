"""Status discloses rerank volume before any provider call."""

import asyncio
from types import SimpleNamespace

import pytest
from src.tools import system


@pytest.mark.parametrize("candidates", [None, 7, 100])
def test_registered_status_reports_actual_backend(fake_server, seeded_db, candidates):
    backend = (
        None
        if candidates is None
        else SimpleNamespace(
            candidates=candidates,
            base_url="PRIVATE_URL",
            model="PRIVATE_MODEL",
            api_key="PRIVATE_KEY",  # pragma: allowlist secret -- synthetic disclosure marker
        )
    )
    system.register_system_tools(fake_server, seeded_db, reranker=backend)
    result = asyncio.run(fake_server.tools["get_mailbox_status"]())
    data = result.structuredContent
    assert data["rerank_mode"] == ("none" if backend is None else "cohere")
    assert data["rerank_candidates"] == candidates
    wire = result.model_dump_json()
    assert "PRIVATE_" not in wire
    assert (
        "max(limit, rerank_candidates)" in result.content[0].text
        if backend
        else "Reranking: none" in result.content[0].text
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 20),
        ("", 20),
        (" \t ", 20),
        ("37", 37),
        (" 37 ", 37),
    ],
)
def test_standalone_status_reads_container_configuration(monkeypatch, seeded_db, raw, expected):
    monkeypatch.setenv("SQLITE_PATH", seeded_db.path)
    monkeypatch.setenv("RERANK_MODE", " CoHeRe ")
    if raw is None:
        monkeypatch.delenv("RERANK_CANDIDATES", raising=False)
    else:
        monkeypatch.setenv("RERANK_CANDIDATES", raw)
    data = system.get_mailbox_status()
    assert data["status"] == "ok"
    assert data["rerank_mode"] == "cohere"
    assert data["rerank_candidates"] == expected


@pytest.mark.parametrize("raw", ["0", "-1", "PRIVATE_CONFIG_MARKER", "1.5"])
def test_invalid_candidates_fail_with_fixed_error(monkeypatch, seeded_db, raw):
    monkeypatch.setenv("SQLITE_PATH", seeded_db.path)
    monkeypatch.setenv("RERANK_MODE", "cohere")
    monkeypatch.setenv("RERANK_CANDIDATES", raw)
    data = system.get_mailbox_status()
    assert data["status"] == "error"
    assert "PRIVATE_CONFIG_MARKER" not in repr(data)
