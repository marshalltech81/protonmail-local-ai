"""Attachment shapes against the built baseline (#906).

Three paths the indexer and ``search_attachments`` take on real mail,
pinned on the synthetic corpus (``indexer/tests/baseline/corpus.py``,
threads 78-81):

- t78: a real digital PDF whose fact (the Brambleford granary) is in the
  attachment only, so only the attachment tools return it;
- t79 / t80: one payload under two filenames in two threads, so both
  occurrences share one ``attachment_id`` and one extraction row;
- t81: a PDF under a ``.txt`` filename, declared ``application/pdf``,
  extracted by the PDF extractor because dispatch goes by MIME type.

The indexer side (extractor stamp, ``dispatch_via``, ``cached=1`` on
the attachments line) is checked in
``indexer/tests/baseline/test_baseline_build.py``.

Skipped unless ``BASELINE_DIR`` is set; ``make baseline`` runs it.
"""

import asyncio
import json
import os
from contextlib import closing
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


def _mid(ref: str) -> str:
    return f"{ref}{_DOMAIN}"


@pytest.fixture(scope="module")
def baseline_dir() -> Path:
    baseline_dir = os.environ.get("BASELINE_DIR")
    if not baseline_dir:
        pytest.skip("BASELINE_DIR not set; run `make baseline`")
    return Path(baseline_dir)


@pytest.fixture(scope="module")
def baseline_db(baseline_dir: Path) -> Database:
    return Database(str(baseline_dir / "mail.db"))


@pytest.fixture(scope="module")
def tools(baseline_dir: Path, baseline_db: Database) -> dict[str, Any]:
    vectors = json.loads((baseline_dir / "query_vectors.json").read_text(encoding="utf-8"))
    server = FakeMCPServer()
    register_retrieval_tools(server, baseline_db)
    register_search_tools(server, baseline_db, PrecomputedEmbedder(vectors))
    return server.tools


def _call(tools: dict[str, Any], name: str, **arguments: Any) -> dict:
    return asyncio.run(tools[name](**arguments)).structured_content


def test_real_pdf_fact_is_found_in_the_attachment(tools: dict) -> None:
    (hit,) = _call(tools, "search_attachments", query="Brambleford granary")["results"]
    assert hit["message_id"] == _mid("t78.1")
    assert (hit["filename"], hit["content_type"]) == ("honey-order.pdf", "application/pdf")
    assert hit["extraction_status"] == "success"
    evidence = _call(tools, "get_evidence", query="Brambleford granary", thread_id=_mid("t78.1"))
    chunks = [c for t in evidence["threads"] for c in t["chunks"]]
    (pdf_chunk,) = [c for c in chunks if "Brambleford granary" in c["text"]]
    assert pdf_chunk["source"] == "attachment"
    assert pdf_chunk["attachment_id"] == hit["attachment_id"]


def test_real_pdf_fact_is_not_in_the_body_tools(tools: dict) -> None:
    """The attachment_only contract: a text lookup for the fact lists
    nothing, and get_message and get_thread show no attachment text."""
    page = _call(tools, "query_messages", text="Brambleford granary", limit=100)
    assert page["messages"] == []
    message = _call(tools, "get_message", message_id=_mid("t78.1"))
    assert message["next_offset"] is None
    assert "Brambleford" not in message["body"]
    thread = _call(tools, "get_thread", thread_id=_mid("t78.1"))
    assert all("Brambleford" not in m["body"] for m in thread["messages"])


def test_one_payload_under_two_filenames_shares_one_attachment_id(
    tools: dict, baseline_db: Database
) -> None:
    hits = _call(tools, "search_attachments", query="pinwheel calico muslin sashing")["results"]
    by_message = {h["message_id"]: h for h in hits}
    assert set(by_message) == {_mid("t79.1"), _mid("t80.1")}
    assert {h["thread_id"] for h in hits} == {_mid("t79.1"), _mid("t80.1")}
    assert {h["filename"] for h in hits} == {"pinwheel-pattern.txt", "guild-handout.txt"}
    assert all(h["extraction_status"] == "success" for h in hits)
    (attachment_id,) = {h["attachment_id"] for h in hits}
    with closing(baseline_db._connect()) as conn:
        (extractions,) = conn.execute(
            "SELECT COUNT(*) FROM attachment_extractions WHERE attachment_id = ?",
            (attachment_id,),
        ).fetchone()
        (occurrences,) = conn.execute(
            "SELECT COUNT(*) FROM attachments WHERE attachment_id = ?", (attachment_id,)
        ).fetchone()
    assert (extractions, occurrences) == (1, 2)


def test_pdf_under_a_txt_filename_is_extracted_as_pdf(tools: dict, baseline_db: Database) -> None:
    (hit,) = _call(tools, "search_attachments", query="Ashgrove meadow")["results"]
    assert hit["message_id"] == _mid("t81.1")
    assert hit["filename"] == "stargazing-ticket.txt"
    assert hit["content_type"] == "application/pdf"
    assert hit["extraction_status"] == "success"
    assert "Ashgrove meadow" in hit["text_snippet"]
    # The text extractor (extension dispatch) would have stamped "text@N".
    with closing(baseline_db._connect()) as conn:
        (extractor,) = conn.execute(
            "SELECT extractor FROM attachment_extractions WHERE attachment_id = ?",
            (hit["attachment_id"],),
        ).fetchone()
    assert extractor.startswith("pdf-digital@")
