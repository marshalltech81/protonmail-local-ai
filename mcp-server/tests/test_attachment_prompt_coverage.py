"""Per-attachment prompt-budget accounting (#1057), synthetic only."""

import asyncio
import logging
from dataclasses import replace

import pytest
import src.tools.intelligence as intelligence
from src.tools.intelligence import _build_evidence

from tests.conftest import FakeEmbedClient, FakeInferenceClient, FakeMCPServer
from tests.test_intelligence import _chunk, _result


def test_one_cited_attachment_exposes_its_own_cut_and_another_its_omission():
    chunks = [
        _chunk(
            "a" * 3000,
            index=0,
            attachment_id="a",
            attachment_filename="synthetic-a.txt",
            attachment_mime="text/plain",
        ),
        _chunk(
            "b" * 3000,
            index=1,
            attachment_id="b",
            attachment_filename="synthetic-b.txt",
            attachment_mime="text/plain",
        ),
    ]
    known, attachments = {}, {}
    _texts, coverage = _build_evidence(
        [_result(evidence_chunks=chunks)],
        600,
        evidence_map=known,
        attachment_coverage=attachments,
    )
    assert coverage.truncated == coverage.omitted == 1
    shown = attachments[("t", "m1#00000000", "a")]
    omitted = attachments[("t", "m1#00000000", "b")]
    assert (
        shown["passages_available"] == shown["passages_shown"] == shown["passages_truncated"] == 1
    )
    assert shown["passages_omitted"] == 0
    assert shown["chars_available"] == 3000
    assert 0 < shown["chars_shown"] < 3000
    assert omitted["passages_available"] == omitted["passages_omitted"] == 1
    assert omitted["passages_shown"] == omitted["chars_shown"] == 0
    assert omitted["chars_available"] == 3000
    assert "complete" not in shown  # Prompt coverage cannot certify model extraction completeness.


@pytest.mark.parametrize("budget", [0, 600, 2000, 20000])
def test_handler_reports_all_payloads_even_when_model_returns_null(
    empty_db, monkeypatch, caplog, budget
):
    marker = "SYNTHETIC_PRIVATE_ATTACHMENT_TEXT"
    chunks = [
        _chunk(marker * 100, index=i, attachment_id="same-payload", char_start=i * 100)
        for i in range(2)
    ]
    threads = [replace(_result(evidence_chunks=chunks), thread_id=f"t{i}") for i in range(2)]
    monkeypatch.setattr(empty_db, "hybrid_search", lambda **kwargs: threads)
    monkeypatch.setattr(intelligence, "_text_budget", lambda *args: budget)
    llm = FakeInferenceClient(response="null")
    server = FakeMCPServer()
    intelligence.register_intelligence_tools(server, empty_db, FakeEmbedClient(), llm)
    with caplog.at_level(logging.INFO):
        out = asyncio.run(
            server.tools["extract_from_emails"](query="synthetic", schema={"value": "string"})
        )
    assert out.structured_content["records"] == []
    rows = out.structured_content["attachment_prompt_coverage"]
    assert len(rows) == 2  # Same bytes/claimant in separate thread prompts stay separate.
    assert {row["thread_id"] for row in rows} == {"t0", "t1"}
    for row in rows:
        assert row["attachment_id"] == "same-payload"
        assert row["scope"] == "retrieved_passages"
        assert row["model_extraction_completeness"] == "unknown"
        assert row["passages_available"] == 2
        assert row["chars_available"] == 2 * len(marker * 100)  # Overlap is counted.
        assert row["passages_omitted"] == 2 - row["passages_shown"]
        assert row["chars_shown"] <= row["chars_available"]
        if budget == 0:
            assert row["passages_omitted"] == 2
            assert row["chars_shown"] == row["passages_shown"] == 0
        elif budget == 20000:
            assert row["passages_shown"] == 2
            assert row["passages_omitted"] == row["passages_truncated"] == 0
        else:
            assert row["passages_truncated"] == row["passages_omitted"] == 1
    assert len(llm.complete_calls) == 2
    assert "model extracted every relevant record is unknown" in out.structured_content["notice"]
    assert marker not in caplog.text


def test_body_only_extraction_has_no_attachment_accounting(empty_db, monkeypatch):
    monkeypatch.setattr(
        empty_db,
        "hybrid_search",
        lambda **kwargs: [_result(evidence_chunks=[_chunk("synthetic body")])],
    )
    server = FakeMCPServer()
    intelligence.register_intelligence_tools(
        server, empty_db, FakeEmbedClient(), FakeInferenceClient("null")
    )
    out = asyncio.run(
        server.tools["extract_from_emails"](query="synthetic", schema={"value": "string"})
    )
    assert out.structured_content["attachment_prompt_coverage"] == []
    assert "Attachment prompt coverage" not in str(out.content)
