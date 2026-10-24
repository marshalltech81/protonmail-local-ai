"""Undecomposed multipart bodies retain unknown absence, scoped to selection."""

import json
import logging

import pytest
from src import parser

from tests.test_occurrence_reconciliation import Box

HEAD = b"Message-ID: <structure@example.test>\r\nDate: Mon, 01 Jan 2024 00:00:00 +0000\r\n"
PLAIN = b"Content-Type: text/plain\r\n\r\nintact words"
HTML = b"Content-Type: text/html\r\n\r\n<p>html words</p>"
MARKER = "SYNTHETIC_PRIVATE_MARKER"


def multipart(*parts, subtype="mixed", close=True):
    return (
        f'Content-Type: multipart/{subtype}; boundary="b"\r\n\r\n'.encode()
        + b"".join(b"--b\r\n" + p + b"\r\n" for p in parts)
        + (b"--b--\r\n" if close else b"")
    )


@pytest.mark.parametrize(
    "broken",
    [
        b"Content-Type: multipart/mixed\r\n\r\n" + MARKER.encode(),
        b'Content-Type: multipart/mixed; boundary="absent"\r\n\r\n' + MARKER.encode(),
    ],
)
@pytest.mark.parametrize("nested", [False, True])
def test_unsplit_catalogue_marks_retained_body_incomplete(tmp_path, caplog, broken, nested):
    path = tmp_path / "body.eml"
    path.write_bytes(HEAD + (multipart(PLAIN, broken) if nested else broken))
    with caplog.at_level(logging.WARNING):
        msg = parser.parse_email(path)
    assert msg is not None
    assert msg.body_text == ("intact words" if nested else "")
    assert not msg.body_complete
    assert msg.attachments_manifest_complete
    assert msg.parse_caps == {"body_structure": 1}
    assert "body_structure=1" in caplog.text
    assert MARKER not in caplog.text


@pytest.mark.parametrize(
    "shape",
    ["missing_close", "missing_separator", "unused_alternative", "attachment", "html_fallback"],
)
def test_structure_loss_selection_catalogue(tmp_path, shape):
    broken = b"Content-Type: multipart/related\r\n\r\n" + MARKER.encode()
    raw, complete, expected = {
        "missing_close": (multipart(PLAIN, close=False), True, "intact words"),
        # The stdlib retains the scalar text when only the separator is missing.
        "missing_separator": (
            multipart(b"Content-Type: text/plain\r\nintact words"),
            True,
            "intact words",
        ),
        "unused_alternative": (
            multipart(PLAIN, broken, subtype="alternative"),
            True,
            "intact words",
        ),
        "attachment": (
            multipart(
                PLAIN, broken.replace(b"\r\n\r\n", b"\r\nContent-Disposition: attachment\r\n\r\n")
            ),
            True,
            "intact words",
        ),
        "html_fallback": (multipart(broken, HTML, subtype="alternative"), False, "html words"),
    }[shape]
    path = tmp_path / "body.eml"
    path.write_bytes(HEAD + raw)
    msg = parser.parse_email(path)
    assert msg.body_text == expected
    assert msg.body_complete is complete
    assert msg.parse_caps.get("body_structure", 0) == int(not complete)


def test_structure_loss_persists_after_phase_two(tmp_path, caplog):
    box = Box(tmp_path)
    try:
        path = box.put(
            "INBOX/cur/body:2,S", HEAD + b"Content-Type: multipart/mixed\r\n\r\n" + MARKER.encode()
        )
        with caplog.at_level(logging.WARNING):
            box.index(path)
        row = box.db._conn.execute("SELECT body_complete,caps_json FROM messages").fetchone()
        assert row["body_complete"] == 0
        assert json.loads(row["caps_json"]) == {"body_structure": 1}
        assert MARKER not in caplog.text
    finally:
        box.close()
