"""Attachment formats and container shapes against the built baseline (#909).

Six paths the indexer and the attachment tools take on real mail,
pinned on the synthetic corpus (``indexer/tests/baseline/corpus.py``,
threads 82-87):

- t82: a DOCX whose fact (the Ravensholm abbey) is in the attachment only;
- t83: an XLSX whose fact (the Quillon greenhouse) is on its second sheet;
- t84: a JSON attachment no extractor reads (``unsupported``), found by
  its filename;
- t85: a whitespace-only text attachment (``empty``);
- t86: an attached email (``message/rfc822``) carrying its own
  attachment, whose inner body is neither attachment text nor the outer
  body;
- t87: a text attachment with an RFC 2231 encoded non-ASCII filename.

The indexer side (extractor stamps, statuses, no parser cap firing) is
checked in ``indexer/tests/baseline/test_baseline_build.py``.

Skipped unless ``BASELINE_DIR`` is set; ``make baseline`` runs it.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from src.lib.sqlite import Database
from src.tools.retrieval import register_retrieval_tools
from src.tools.search import register_search_tools

from tests.answer_eval.runner import PrecomputedEmbedder
from tests.conftest import FakeMCPServer

pytestmark = pytest.mark.baseline

_DOMAIN = "@baseline.example"
_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _mid(ref: str) -> str:
    return f"{ref}{_DOMAIN}"


@pytest.fixture(scope="module")
def baseline_dir() -> Path:
    baseline_dir = os.environ.get("BASELINE_DIR")
    if not baseline_dir:
        pytest.skip("BASELINE_DIR not set; run `make baseline`")
    return Path(baseline_dir)


@pytest.fixture(scope="module")
def tools(baseline_dir: Path) -> dict[str, Any]:
    vectors = json.loads((baseline_dir / "query_vectors.json").read_text(encoding="utf-8"))
    db = Database(str(baseline_dir / "mail.db"))
    server = FakeMCPServer()
    register_retrieval_tools(server, db)
    register_search_tools(server, db, PrecomputedEmbedder(vectors))
    return server.tools


def _call(tools: dict[str, Any], name: str, **arguments: Any) -> dict:
    return asyncio.run(tools[name](**arguments)).structured_content


def _attachment_chunks(tools: dict, query: str, ref: str) -> list[dict]:
    evidence = _call(tools, "get_evidence", query=query, thread_id=_mid(ref))
    return [c for t in evidence["threads"] for c in t["chunks"] if c["source"] == "attachment"]


@pytest.mark.parametrize(
    ("query", "ref", "filename", "content_type"),
    [
        ("Ravensholm abbey", "t82.1", "rehearsal-notes.docx", _DOCX),
        ("Quillon greenhouse", "t83.1", "seed-swap.xlsx", _XLSX),
    ],
    ids=["docx", "xlsx-second-sheet"],
)
def test_office_attachment_fact_is_found_in_the_attachment(
    tools: dict, query: str, ref: str, filename: str, content_type: str
) -> None:
    (hit,) = _call(tools, "search_attachments", query=query)["results"]
    assert hit["message_id"] == _mid(ref)
    assert (hit["filename"], hit["content_type"]) == (filename, content_type)
    assert hit["extraction_status"] == "success"
    (chunk,) = [c for c in _attachment_chunks(tools, query, ref) if query in c["text"]]
    assert chunk["attachment_id"] == hit["attachment_id"]
    # The attachment_only contract: no body holds the fact.
    assert _call(tools, "query_messages", text=query, limit=100)["messages"] == []


def test_xlsx_second_sheet_is_extracted_under_its_title(tools: dict) -> None:
    chunks = _attachment_chunks(tools, "Quillon greenhouse", "t83.1")
    assert any("[Sheet: Pickup]" in c["text"] and "Quillon" in c["text"] for c in chunks)


def test_unsupported_attachment_is_found_by_filename(tools: dict) -> None:
    (hit,) = _call(tools, "search_attachments", query="roster")["results"]
    assert hit["message_id"] == _mid("t84.1")
    assert (hit["filename"], hit["content_type"]) == ("kite-roster.json", "application/json")
    assert hit["extraction_status"] == "unsupported"
    assert not hit["text_snippet"]
    assert _attachment_chunks(tools, "kite flyers export", "t84.1") == []


def test_empty_attachment_is_listed_without_evidence(tools: dict) -> None:
    (hit,) = _call(tools, "search_attachments", query="rota", content_type="text/plain")["results"]
    assert hit["message_id"] == _mid("t85.1")
    assert hit["filename"] == "spring-rota.txt"
    assert hit["extraction_status"] == "empty"
    assert not hit["text_snippet"]
    assert _attachment_chunks(tools, "blank spring rota", "t85.1") == []


def test_attached_email_is_attachment_text_and_not_the_body(tools: dict) -> None:
    (hit,) = _call(tools, "search_attachments", query="Corrigan")["results"]
    assert hit["message_id"] == _mid("t86.1")
    assert (hit["filename"], hit["content_type"]) == ("crossing.txt", "text/plain")
    assert hit["extraction_status"] == "success"
    (container,) = _call(tools, "search_attachments", content_type="message/rfc822")["results"]
    assert container["message_id"] == _mid("t86.1")
    assert container["filename"] == "ferry-crossing.eml"
    # #922: the attached email's headers and body are its attachment
    # text, never the outer body (its exact text, without its own
    # attachment's, is pinned in the indexer's baseline build test).
    assert container["extraction_status"] == "success"
    (inner,) = _call(tools, "search_attachments", query="gangway")["results"]
    assert (inner["message_id"], inner["filename"]) == (_mid("t86.1"), "ferry-crossing.eml")
    message = _call(tools, "get_message", message_id=_mid("t86.1"))
    assert "Forwarding this one" in message["body"]
    assert "gangway" not in message["body"]
    assert _call(tools, "query_messages", text="gangway", limit=100)["messages"] == []


def test_non_ascii_filename_is_stored_decoded_and_searchable(tools: dict) -> None:
    (hit,) = _call(tools, "search_attachments", query="Mélèzes")["results"]
    assert hit["message_id"] == _mid("t87.1")
    assert hit["filename"] == "fête-des-Mélèzes.txt"
    assert hit["extraction_status"] == "success"
    assert "Pellow orchard" in hit["text_snippet"]
