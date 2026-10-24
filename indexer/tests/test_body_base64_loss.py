"""Known base64 decoder defects invalidate only retained body evidence."""

import base64
import json
import logging
from unittest.mock import patch

import pytest
from src import parser

from tests.test_occurrence_reconciliation import Box

HEAD = b"Message-ID: <body@example.test>\r\nDate: Mon, 01 Jan 2024 00:00:00 +0000\r\n"
PLAIN = b"Content-Type: text/plain\r\n\r\nintact words"
MARKER = "SYNTHETIC_PRIVATE_MARKER"


def multipart(*parts, subtype="mixed"):
    return (
        f'Content-Type: multipart/{subtype}; boundary="b"\r\n\r\n'.encode()
        + b"".join(b"--b\r\n" + p + b"\r\n" for p in parts)
        + b"--b--\r\n"
    )


def encoded(payload):
    return b"Content-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n" + payload


@pytest.mark.parametrize(
    "payload,loss",
    [
        (base64.b64encode(b"intact words"), False),
        (b"aW50YWN0IHdvcmRz$", True),
        (b"aW50YWN0IHdvcmQ", False),  # Missing padding is repaired losslessly.
        (b"c2VjcmV0A", True),
        (b"", False),
    ],
)
@pytest.mark.parametrize("nested", [False, True])
def test_base64_defect_catalogue(tmp_path, caplog, payload, loss, nested):
    path = tmp_path / "body.eml"
    part = encoded(payload)
    path.write_bytes(HEAD + (multipart(PLAIN, part) if nested else part))
    with (
        caplog.at_level(logging.WARNING),
        patch.object(parser, "_decoded_payload", wraps=parser._decoded_payload) as decode,
    ):
        msg = parser.parse_email(path)
    assert msg is not None
    assert msg.body_complete is (not loss)
    assert msg.parse_caps == ({"body_decode": 1} if loss else {})
    # Exactly one decode per body leaf, also when full-body capture checks loss.
    assert decode.call_count == 1 + int(nested)
    assert msg.attachments_manifest_complete
    assert ("body_decode=1" in caplog.text) is loss
    assert MARKER not in caplog.text


@pytest.mark.parametrize("bad_first", [False, True])
def test_an_unselected_base64_alternative_does_not_degrade_body(tmp_path, bad_first):
    path = tmp_path / "alternative.eml"
    bad = b"Content-Type: text/html\r\nContent-Transfer-Encoding: base64\r\n\r\n!!!!"
    path.write_bytes(
        HEAD + multipart(*((bad, PLAIN) if bad_first else (PLAIN, bad)), subtype="alternative")
    )
    msg = parser.parse_email(path)
    assert msg.body_text == "intact words"
    assert msg.body_complete
    assert msg.parse_caps == {}


def test_body_decode_loss_persists_with_committed_chunks(tmp_path, caplog):
    box = Box(tmp_path)
    try:
        path = box.put(
            "INBOX/cur/body:2,S",
            HEAD + b"Subject: " + MARKER.encode() + b"\r\n" + encoded(b"c2VjcmV0A"),
        )
        with caplog.at_level(logging.WARNING):
            box.index(path)
        row = box.db._conn.execute("SELECT body_complete,caps_json FROM messages").fetchone()
        assert row["body_complete"] == 0
        assert json.loads(row["caps_json"]) == {"body_decode": 1}
        assert MARKER not in caplog.text
    finally:
        box.close()


@pytest.mark.parametrize("content_type", ["text/calendar", "text/enriched", "text/xml"])
@pytest.mark.parametrize("capture", [False, True])
@pytest.mark.parametrize("loss", [False, True])
def test_source_only_text_does_not_change_legacy_body_completeness(
    tmp_path, content_type, capture, loss
):
    payload = b"c2VjcmV0A" if loss else base64.b64encode(b"intact source")
    other = encoded(payload).replace(b"text/plain", content_type.encode())
    path = tmp_path / "source.eml"
    path.write_bytes(HEAD + multipart(PLAIN, other))
    msg = parser.parse_email(path, capture_full_body=capture)
    assert msg.body_text == "intact words"
    assert msg.body_complete
    assert msg.parse_caps == {}
    if capture and loss:
        from src import full_body

        assert any(part.losses & full_body.TRANSFER_LOSS for part in msg.full_body.parts)


@pytest.mark.parametrize("content_type", ["text/calendar", "text/enriched", "text/xml"])
def test_nonplain_root_text_is_still_legacy_body_evidence(tmp_path, content_type):
    path = tmp_path / "root.eml"
    path.write_bytes(HEAD + encoded(b"c2VjcmV0A").replace(b"text/plain", content_type.encode()))
    msg = parser.parse_email(path)
    assert msg.body_text == "c2VjcmV0A"
    assert not msg.body_complete
    assert msg.parse_caps == {"body_decode": 1}
