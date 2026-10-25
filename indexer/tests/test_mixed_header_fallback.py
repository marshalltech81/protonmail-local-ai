"""Mixed malformed words retain recovered UTF-8 across text-header consumers."""

import pytest
from src import parser
from src.extractors import eml


@pytest.mark.parametrize("word", ["=?utf-8?b?a?=", "=?utf-8?b?abcde?="])
@pytest.mark.parametrize("field", ["Subject", "From"])
@pytest.mark.parametrize("attached", [False, True])
def test_malformed_word_does_not_replace_valid_raw_utf8(word, field, attached, caplog):
    marker = "SYNTHETIC_HEADER_FALLBACK"
    value = f"café {marker} {word}" + (" <>" if field == "From" else "")
    headers = {
        "Message-ID": "<synthetic@example.test>",
        "Subject": "synthetic",
        "From": "a@example.test",
    }
    headers[field] = value
    raw = ("".join(f"{key}: {text}\r\n" for key, text in headers.items()) + "\r\nbody\r\n").encode()
    if attached:
        text, _ = eml.extract_text(raw)
    else:
        source = parser.SourceMetadata(
            folder="INBOX", flags=frozenset(), size=len(raw), mtime_ns=None, path="/synthetic.eml"
        )
        msg = parser.parse_email_bytes(raw, source)
        assert msg is not None
        text = msg.subject if field == "Subject" else msg.from_addr
    assert value in text
    assert "�" not in text
    assert marker not in caplog.text
