"""Capped attachments against the built baseline (#907).

Two attachments the indexer refuses or cuts, pinned on the synthetic
corpus (``indexer/tests/baseline/corpus.py``, threads 88-89). The build
lowers the caps (``indexer/tests/baseline/build.py``: 64 KiB and 20,000
characters, not the production defaults) so the fixtures stay small:

- t88: a text attachment over the size cap (``too_large``): listed by
  ``search_attachments`` by filename with its status and no text, and
  never returned by ``get_evidence``;
- t89: a text attachment cut at the extracted-characters cap
  (``success``): text before the cut is found, and the sentence past it
  is a known loss recorded in ``tests/eval/outstanding_items.json``
  ``corpus_evidence`` (layers measured by
  ``test_outstanding_items_baseline.py``).

The indexer side (status, stored text length, the cap WARNING and the
aggregate counts) is checked in
``indexer/tests/baseline/test_baseline_build.py``.

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


def _evidence_chunks(tools: dict, query: str, ref: str) -> list[dict]:
    evidence = _call(tools, "get_evidence", query=query, thread_id=_mid(ref), limit=60)
    return [c for t in evidence["threads"] for c in t["chunks"]]


def test_too_large_attachment_is_listed_by_filename_without_text(tools: dict) -> None:
    (hit,) = _call(tools, "search_attachments", query="ledger")["results"]
    assert hit["message_id"] == _mid("t88.1")
    assert (hit["filename"], hit["content_type"]) == ("harbour-ledger.txt", "text/plain")
    assert hit["extraction_status"] == "too_large"
    assert not hit["text_snippet"]
    extracted = _call(tools, "search_attachments", query="ledger", extracted_only=True)
    assert extracted["results"] == []


def test_too_large_attachment_is_never_evidence(tools: dict) -> None:
    chunks = _evidence_chunks(tools, "harbour ledger transcription", "t88.1")
    assert chunks, "the thread's body is still evidence"
    assert all(c["source"] == "body" for c in chunks)
    # The attachment's text is in no tool: not in the body, not searchable.
    assert _call(tools, "query_messages", text="schooner", limit=100)["messages"] == []
    assert _call(tools, "search_attachments", query="schooner")["results"] == []


def test_char_capped_attachment_is_found_before_the_cut(tools: dict) -> None:
    (hit,) = _call(tools, "search_attachments", query="Corncrake towpath")["results"]
    assert hit["message_id"] == _mid("t89.1")
    assert hit["filename"] == "wheelers-route-notes.txt"
    assert hit["extraction_status"] == "success"
    chunks = _evidence_chunks(tools, "Corncrake Wheelers towpath viaduct", "t89.1")
    assert any(c["source"] == "attachment" and "Corncrake Wheelers" in c["text"] for c in chunks)


def test_char_capped_sentence_past_the_cut_is_in_no_tool(tools: dict) -> None:
    """The sentence past the cap is the recorded known loss; its words
    reach neither the attachment index nor the evidence chunks."""
    assert _call(tools, "search_attachments", query="Kittiwake")["results"] == []
    chunks = _evidence_chunks(tools, "Kittiwake boathouse sportive", "t89.1")
    assert chunks and not any("Kittiwake" in c["text"] for c in chunks)
    assert _call(tools, "query_messages", text="Kittiwake", limit=100)["messages"] == []
