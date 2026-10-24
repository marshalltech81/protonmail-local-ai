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


def test_standalone_status_reads_container_configuration(monkeypatch, seeded_db):
    monkeypatch.setenv("SQLITE_PATH", seeded_db.path)
    monkeypatch.setenv("RERANK_MODE", "cohere")
    monkeypatch.setenv("RERANK_CANDIDATES", "37")
    data = system.get_mailbox_status()
    assert data["rerank_mode"] == "cohere"
    assert data["rerank_candidates"] == 37
