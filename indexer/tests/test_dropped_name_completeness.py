"""A formatter's identity fallback loses a name, never the address."""

import email.utils

import pytest
from src import parser


@pytest.mark.parametrize("role", ["From", "To", "Cc", "Bcc"])
def test_name_lost_at_identity_guard_clears_only_name_completeness(monkeypatch, caplog, role):
    original = email.utils.parseaddr
    marker = "SYNTHETIC_PRIVATE_NAME"
    formatted = f"{marker.replace('_', ' ')} <alice@example.test>"

    def parse(value, *args, **kwargs):
        # Exercise the formatter's existing fail-safe branch without
        # tying this test to a particular stdlib parser defect.
        if value == formatted:
            return "", "different@example.test"
        return original(value, *args, **kwargs)

    monkeypatch.setattr(email.utils, "parseaddr", parse)
    raw = (
        "Message-ID: <name-loss@example.test>\r\n"
        f"{role}: =?utf-8?q?{marker}?= <alice@example.test>\r\n"
        "Content-Type: text/plain\r\n\r\nSynthetic body.\r\n"
    ).encode()
    source = parser.SourceMetadata(
        folder="INBOX", flags=frozenset(), size=len(raw), mtime_ns=None, path="/synthetic.eml"
    )
    msg = parser.parse_email_bytes(raw, source)
    assert msg is not None
    assert getattr(
        msg, {"From": "from_addrs", "To": "to_addrs", "Cc": "cc_addrs", "Bcc": "bcc_addrs"}[role]
    ) == ["alice@example.test"]
    assert msg.participant_names == []
    assert msg.participant_names_complete is (role == "Bcc")
    assert msg.parse_caps["bcc_participant_names" if role == "Bcc" else "participant_names"] == 1
    assert msg.from_addresses_complete and msg.to_addresses_complete and msg.cc_addresses_complete
    assert marker not in repr(msg.parse_caps)
    assert marker not in caplog.text
    assert marker.replace("_", " ") not in caplog.text
    assert "participant_names=1" in caplog.text


def test_representable_name_stays_complete():
    raw = b"Message-ID: <name-ok@example.test>\r\nTo: Jane Roe <alice@example.test>\r\n\r\nBody"
    source = parser.SourceMetadata(
        folder="INBOX", flags=frozenset(), size=len(raw), mtime_ns=None, path="/synthetic.eml"
    )
    msg = parser.parse_email_bytes(raw, source)
    assert msg.participant_names_complete
    assert msg.participant_names == [("to", "alice@example.test", "Jane Roe")]
