"""Approved #1417 heuristic: bounded HTML prefixes under image labels."""

import logging

import pytest
from src import extractors, main
from src.extractors import OCR_DISABLED_ERROR, STATUS_SUCCESS, extract, extraction_module

from tests.test_extraction_budget import Pipeline
from tests.test_pdf_under_image_label import _IMAGE_LABELS, _jobs, _RealExtract

MARKER = "SYNTHETIC_1417_PRIVATE"
HTML = b"<!DOCTYPE html><html><body>" + MARKER.encode() + b"</body></html>"


@pytest.mark.parametrize("ocr", [False, True])
@pytest.mark.parametrize(("mime", "filename"), _IMAGE_LABELS)
@pytest.mark.parametrize("prefix", [b"", b"\xef\xbb\xbf \r\n\t"])
def test_html_runs_html_and_shares_its_cache_namespace(mime, filename, prefix, ocr, caplog):
    payload = prefix + HTML
    result = extract(content_type=mime, filename=filename, payload=payload, ocr_enabled=ocr)
    assert result.status == STATUS_SUCCESS
    assert result.extractor == "html"
    assert MARKER in result.text
    assert result == extract(
        content_type="text/html", filename="a.html", payload=payload, ocr_enabled=ocr
    )
    assert extraction_module(mime, filename, payload) == "html"
    assert extractors.label_extraction_modules(mime, filename) == {"image", "pdf", "html"}
    assert MARKER not in caplog.text


@pytest.mark.parametrize("prefix", [b"<HtMl>", b"<!dOcTyPe HtMl >", b"<html/>"])
def test_case_insensitive_html_prefixes(prefix):
    assert extraction_module("image/gif", "a.gif", prefix) == "html"


@pytest.mark.parametrize(
    "payload",
    [b"<htmlish>", b"<!DOCTYPE htmlish>", b"x<html>", b" " * 256 + HTML, b"GIF89a" + HTML],
)
def test_near_misses_keep_the_image_namespace(payload):
    assert extraction_module("image/gif", "a.gif", payload) == "image"


def test_prefix_work_is_bounded_before_whitespace_handling():
    class Payload(bytes):
        def __getitem__(self, key):
            assert isinstance(key, slice) and key.start is None and key.stop == 256
            return super().__getitem__(key)

        def lstrip(self, *_args):
            pytest.fail("the whole payload must not be stripped")

        def lower(self):
            pytest.fail("the whole payload must not be lowercased")

    assert extraction_module("image/gif", "a.gif", Payload(HTML + b" " * 1_000_000)) == "html"


@pytest.mark.parametrize("ocr", [False, True])
@pytest.mark.parametrize("share_html", [False, True])
def test_old_image_rows_refresh_once_and_share_html_cache(
    tmp_path, monkeypatch, caplog, ocr, share_html
):
    monkeypatch.setattr(main, "INDEXER_OCR_ENABLED", ocr)
    monkeypatch.setattr(main, "INDEXER_ATTACHMENT_EXTRACTION_ENABLED", True)
    p = Pipeline(tmp_path, monkeypatch, _RealExtract(), launches=50)
    paths = [
        p.add(f"m{i}", [(HTML, mime, name)])
        for i, (mime, name) in enumerate(
            [("image/gif", "a.gif"), ("text/html", "a.html"), ("image/png", "a.png")]
        )
    ]
    while _jobs(p):
        p.drain()
    # Simulate rows recorded before #1417, including a failed image row
    # while OCR is on and an OCR-disabled row while it is off.
    p.db._conn.execute(
        "INSERT INTO attachment_extractions (attachment_id, extractor_module, "
        "extraction_status, extractor, extraction_error, extracted_at) "
        "SELECT attachment_id, 'image', ?, 'image@8', ?, extracted_at FROM attachment_extractions "
        "WHERE extractor_module = 'html'",
        (
            "failed" if ocr else "unsupported",
            "UnidentifiedImageError" if ocr else OCR_DISABLED_ERROR,
        ),
    )
    p.db._conn.execute(
        "UPDATE attachments SET extractor_module = 'image' WHERE filename <> 'a.html'"
    )
    if not share_html:
        p.db._conn.execute("DELETE FROM attachments WHERE filename = 'a.html'")
        p.db._conn.execute("DELETE FROM attachment_extractions WHERE extractor_module = 'html'")
    p.db._conn.commit()
    p.extractor.calls.clear()
    caplog.set_level(logging.INFO)
    assert main._requeue_stale_extractions(p.db, p.queue) == 2
    assert set(_jobs(p)) == {paths[0], paths[2]}
    while _jobs(p):
        p.drain()
    assert p.extractor.calls == ([] if share_html else [HTML])
    # A missing HTML cache row is extracted once and shared by both messages.
    assert {r[0] for r in p.db._conn.execute("SELECT extractor_module FROM attachments")} == {
        "html"
    }
    assert main._requeue_stale_extractions(p.db, p.queue) == 0
    assert MARKER not in caplog.text
    p.db.close()
