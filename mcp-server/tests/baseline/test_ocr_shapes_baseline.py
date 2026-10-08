"""OCR shapes against the built baseline (#908, #1113).

Three attachments the indexer reads with Tesseract, pinned on the
synthetic corpus (``indexer/tests/baseline/corpus.py``, threads 90-92;
the images are the committed fixtures under
``indexer/tests/baseline/fixtures/``):

- t90: a PNG of one line of text, read by the image OCR extractor: the
  words are found by ``search_attachments`` and returned by
  ``get_evidence`` as an attachment chunk, and are in no body tool;
- t91: a scanned PDF (image pages, no text layer) with one page more
  than the build's lowered OCR page cap (``build.py``: 2, not the
  production default): the pages within the cap are found, and the
  last page's words are a known loss recorded in
  ``tests/eval/outstanding_items.json`` ``corpus_evidence`` (layers
  measured by ``test_outstanding_items_baseline.py``);
- t92: a multipage TIFF (one image frame per page) with one frame more
  than the same cap, read frame by frame by the image OCR extractor
  (#885 logs the frame left unread): the frames within the cap are
  found, and the last frame's words are a known loss recorded the same
  way.

Three Tesseract versions are in play (Homebrew, Ubuntu's apt, the
image), so the OCR'd words are matched case-insensitively with
whitespace normalised, and they are in no golden search query: a
one-character OCR difference fails these checks, never the rank
snapshot.

The indexer side (extractor stamps, the cap WARNING and the aggregate
counts) is checked in ``indexer/tests/baseline/test_baseline_build.py``.

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
# The fixtures' text, as the corpus states it (``OCR_IMAGE_TEXT``,
# ``OCR_PDF_PAGES`` and ``OCR_TIFF_FRAMES`` in
# indexer/tests/baseline/corpus.py).
CHALKBOARD_TEXT = "GANNET QUAY POTTERY"
READ_PAGES = ("PLOVER CREEK REGATTA", "CORMORANT CUP RESULTS")
LOST_PAGE = "CURLEW PAVILION SUPPER"
READ_FRAMES = ("SANDERLING WHARF CENSUS", "TURNSTONE INLET MUSTER")
LOST_FRAME = "WHIMBREL JETTY PENNANT"


def _mid(ref: str) -> str:
    return f"{ref}{_DOMAIN}"


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


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


def _attachment_chunks(tools: dict, query: str, ref: str) -> list[str]:
    evidence = _call(tools, "get_evidence", query=query, thread_id=_mid(ref), limit=60)
    return [
        c["text"] for t in evidence["threads"] for c in t["chunks"] if c["source"] == "attachment"
    ]


def test_image_ocr_text_is_found_in_the_attachment(tools: dict) -> None:
    (hit,) = _call(tools, "search_attachments", query="gannet quay")["results"]
    assert hit["message_id"] == _mid("t90.1")
    assert (hit["filename"], hit["content_type"]) == ("chalkboard.png", "image/png")
    assert hit["extraction_status"] == "success"
    assert _norm(CHALKBOARD_TEXT) in _norm(hit["text_snippet"])
    chunks = _attachment_chunks(tools, "gannet quay", "t90.1")
    assert any(_norm(CHALKBOARD_TEXT) in _norm(c) for c in chunks)


def test_image_ocr_text_is_not_in_the_body_tools(tools: dict) -> None:
    """The attachment_only contract: a text lookup for the OCR'd words
    lists nothing, and get_message and get_thread show no attachment
    text."""
    assert _call(tools, "query_messages", text="gannet", limit=100)["messages"] == []
    message = _call(tools, "get_message", message_id=_mid("t90.1"))
    assert message["next_offset"] is None
    assert "gannet" not in message["body"].casefold()
    thread = _call(tools, "get_thread", thread_id=_mid("t90.1"))
    assert thread["messages"] and all(
        "gannet" not in m["body"].casefold() for m in thread["messages"]
    )


def test_capped_scanned_pdf_is_found_by_the_pages_within_the_cap(tools: dict) -> None:
    (hit,) = _call(tools, "search_attachments", query="plover creek")["results"]
    assert hit["message_id"] == _mid("t91.1")
    assert (hit["filename"], hit["content_type"]) == ("scanned-notes.pdf", "application/pdf")
    assert hit["extraction_status"] == "success"
    chunks = _attachment_chunks(tools, "plover creek regatta", "t91.1")
    assert chunks
    for page in READ_PAGES:
        assert any(_norm(page) in _norm(c) for c in chunks), page


def test_capped_scanned_pdf_page_past_the_cap_is_in_no_tool(tools: dict) -> None:
    """The last page is the recorded known loss: its words reach
    neither the attachment index nor the evidence chunks."""
    assert _call(tools, "search_attachments", query="curlew")["results"] == []
    chunks = _attachment_chunks(tools, "curlew pavilion", "t91.1")
    assert chunks and not any("curlew" in _norm(c) for c in chunks)
    assert _norm(LOST_PAGE).split()[0] == "curlew"
    assert _call(tools, "query_messages", text="curlew", limit=100)["messages"] == []


def test_capped_tiff_is_found_by_the_frames_within_the_cap(tools: dict) -> None:
    """#1113: the image OCR extractor reads the TIFF frame by frame up to
    the cap; the words of those frames reach the attachment index and
    the evidence chunks."""
    (hit,) = _call(tools, "search_attachments", query="sanderling wharf")["results"]
    assert hit["message_id"] == _mid("t92.1")
    assert (hit["filename"], hit["content_type"]) == ("headland-fax.tiff", "image/tiff")
    assert hit["extraction_status"] == "success"
    chunks = _attachment_chunks(tools, "sanderling wharf census", "t92.1")
    assert chunks
    for frame in READ_FRAMES:
        assert any(_norm(frame) in _norm(c) for c in chunks), frame


def test_capped_tiff_frame_past_the_cap_is_in_no_tool(tools: dict) -> None:
    """The last frame is the recorded known loss (#885 logs it on the
    indexer side): its words reach neither the attachment index nor the
    evidence chunks nor any body tool."""
    assert _call(tools, "search_attachments", query="whimbrel")["results"] == []
    chunks = _attachment_chunks(tools, "whimbrel jetty", "t92.1")
    assert chunks and not any("whimbrel" in _norm(c) for c in chunks)
    assert _norm(LOST_FRAME).split()[0] == "whimbrel"
    assert _call(tools, "query_messages", text="whimbrel", limit=100)["messages"] == []
    message = _call(tools, "get_message", message_id=_mid("t92.1"))
    assert "sanderling" not in message["body"].casefold()
    assert "whimbrel" not in message["body"].casefold()
