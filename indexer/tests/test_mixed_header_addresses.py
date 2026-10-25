"""Text-header decoding must preserve split-before-decode address parsing."""

import email

from src import parser


def test_raw_8bit_address_header_does_not_decode_name_into_address_syntax():
    message = email.message_from_bytes(
        b"To: caf\xc3\xa9 =?utf-8?q?Eve=3Cfake=40other.example=3E=2C?= <real@example.test>\r\n\r\n"
    )
    addresses = parser._parse_addrs(message["To"])
    assert len(addresses) == 1
    assert addresses[0].endswith("<real@example.test>")
    assert "café" in addresses[0]
