"""Decoded mail text is UTF-8 safe, with visible, content-free replacements (#1447)."""

from collections import Counter

import pytest
from src import parser
from src.chunker import chunk_message
from src.extractors import extractor_child

MARKER = "SYNTHETIC_DECODE_1447"


def test_utf7_plain_body_reaches_chunking(caplog):
    raw = (
        "Message-ID: <surrogate@example.test>\r\nFrom: a@example.test\r\n"
        "Content-Type: text/plain; charset=utf-7\r\n\r\n"
        f"hello +2DQ- world {MARKER}"
    ).encode()
    source = parser.SourceMetadata(
        folder="INBOX", flags=frozenset(), size=len(raw), mtime_ns=0, path="synthetic"
    )
    with caplog.at_level("INFO"):
        message = parser.parse_email_bytes(raw, source)
    assert message is not None
    assert "hello ? world" in message.body_text
    assert chunk_message(message_pk=message.claimant_id, body_text=message.body_text)
    assert "replaced 1 lone surrogate" in caplog.text
    assert MARKER not in caplog.text


def test_header_decode_replaces_surrogates_and_counts_degradation(caplog):
    degraded = Counter()
    with caplog.at_level("INFO"):
        text = parser._decode_header_parts([(b"hello +2DQ- world", "utf-7")], degraded)
    assert text == "hello ? world"
    assert degraded[parser.HEADER_DEGRADED] == 1
    assert "replaced 1 lone surrogate" in caplog.text


def test_rfc2231_filename_decode_cannot_return_surrogate(caplog):
    import email

    part = email.message.Message()
    part["Content-Disposition"] = "attachment; filename*=utf-7''a%2B2DQ-.txt"
    degraded = Counter()
    with caplog.at_level("INFO"):
        filename = parser._part_filename(part, degraded)
    assert filename == "a?.txt"
    assert degraded[parser.FILENAME_DEGRADED] == 1
    assert "replaced 1 lone surrogate" in caplog.text


def test_child_result_replacements_cross_as_a_fixed_count():
    assert extractor_child.result_frames("a\ud800b\udfff", []) == (
        b"N text_surrogates_replaced 2\nT 4\na?b?"
    )


def test_child_result_replacement_count_is_visible_in_parent(monkeypatch, caplog):
    from src.extractors import image

    from tests.test_extractor_child import stub_child_output

    stub_child_output(monkeypatch, b"N text_surrogates_replaced 2\nT 4\na?b?")
    with caplog.at_level("INFO"):
        assert image.extract(b"synthetic")[0] == "a?b?"
    assert "text_surrogates_replaced=2" in caplog.text


def test_eml_decode_replacements_cross_to_parent(caplog):
    from src.extractors import eml

    raw = (
        f"Subject: synthetic\r\nContent-Type: text/plain; charset=utf-7\r\n\r\nhello +2DQ- {MARKER}"
    ).encode()
    with caplog.at_level("INFO"):
        text, _ = eml.extract(raw)
    assert "hello ?" in text
    assert "eml_charsets_degraded=1" in caplog.text
    assert MARKER not in caplog.text


@pytest.mark.serial
def test_surrogate_replacement_makes_one_bounded_pass(monkeypatch, caplog):
    import time

    real = parser.html_conversion.replace_lone_surrogates
    work = []

    def counted(text):
        work.append(len(text))
        return real(text)

    monkeypatch.setattr(parser.html_conversion, "replace_lone_surrogates", counted)
    degraded = Counter()
    started = time.perf_counter()
    text = parser._safe_decode(b"+2DQ-" * 20000, "utf-7", degraded)
    assert time.perf_counter() - started < 2
    assert text == "?" * 20000
    assert work == [20000]
    assert degraded[parser.CHARSET_DEGRADED] == 1
