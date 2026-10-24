"""The stall watchdog must disclose when a permitted extractor child can outlive it."""

import importlib
import logging

import pytest
from src import extractors, main
from src.extractors import eml, html, image, pdf


@pytest.mark.parametrize(
    "ocr,pages,timeout,digital,guard,warn",
    [
        (True, 20, 60, 500, 3600, False),
        (True, 20, 0, 500, 3600, True),
        (True, 100, 60, 500, 3600, True),
        (False, 20, 0, 500, 3600, False),
        (False, 20, 0, 500, 100, True),
        (False, 20, 0, 0, 60, True),
        (False, 20, 0, 0, 500, True),
        (True, 20, 0, 500, 0, False),
        (True, 20, 0, 500, 10000, False),
    ],
)
def test_timeout_configuration_warning(
    tmp_path, monkeypatch, caplog, ocr, pages, timeout, digital, guard, warn
):
    for key, value in {
        "INDEXER_ATTACHMENT_EXTRACTION_ENABLED": True,
        "INDEXER_OCR_ENABLED": ocr,
        "INDEXER_OCR_MAX_PAGES": pages,
        "INDEXER_OCR_TIMEOUT_SECONDS": timeout,
        "INDEXER_PDF_MAX_DIGITAL_PAGES": digital,
        "INDEXER_MESSAGE_TIMEOUT_SECONDS": guard,
    }.items():
        monkeypatch.setattr(main, key, value)
    with caplog.at_level(logging.WARNING):
        main._warn_extraction_timeout_config()
    lines = [r.getMessage() for r in caplog.records if "can expire before" in r.getMessage()]
    assert bool(lines) is warn
    if warn:
        assert len(lines) == 1
        assert f"INDEXER_MESSAGE_TIMEOUT_SECONDS={guard}" in lines[0]
        assert "largest_extractor_child_seconds=" in lines[0]
        assert "raise INDEXER_MESSAGE_TIMEOUT_SECONDS" in lines[0]


def test_disabled_extraction_has_no_timeout_warning(monkeypatch, caplog):
    monkeypatch.setattr(main, "INDEXER_ATTACHMENT_EXTRACTION_ENABLED", False)
    monkeypatch.setattr(main, "INDEXER_MESSAGE_TIMEOUT_SECONDS", 1)
    with caplog.at_level(logging.WARNING):
        main._warn_extraction_timeout_config()
    assert "can expire before" not in caplog.text


def test_warning_uses_the_actual_child_timeout_functions(monkeypatch, caplog):
    monkeypatch.setattr(main, "INDEXER_ATTACHMENT_EXTRACTION_ENABLED", True)
    monkeypatch.setattr(main, "INDEXER_OCR_ENABLED", True)
    monkeypatch.setattr(main, "INDEXER_MESSAGE_TIMEOUT_SECONDS", 3600)
    monkeypatch.setattr(image, "child_timeout_seconds", lambda *a: 9001.0)
    with caplog.at_level(logging.WARNING):
        main._warn_extraction_timeout_config()
    assert "largest_extractor_child_seconds=9001" in caplog.text


def test_fixed_child_timeouts_fit_the_non_ocr_floor():
    # Derive the formats from dispatch, so a new format/changed timeout
    # cannot silently escape the startup comparison. The three exceptions
    # are dynamic PDF/image clocks and child-free constant-pass text.
    modules = set(extractors._MIME_DISPATCH.values()) | set(extractors._EXT_DISPATCH.values())
    fixed = modules - {"pdf", "image", "text"}
    for name in fixed | {"container"}:
        module = importlib.import_module(f"src.extractors.{name}")
        clocks = [value for key, value in vars(module).items() if key.endswith("TIMEOUT_SECONDS")]
        assert clocks, name
        assert max(clocks) <= max(eml.CHILD_TIMEOUT_SECONDS, html.CHILD_TIMEOUT_SECONDS), name
    assert (
        pdf.child_timeout_seconds(
            ocr_enabled=False, max_ocr_pages=20, ocr_timeout_seconds=None, max_pdf_pages=0
        )
        == 30
    )
