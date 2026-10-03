"""
Shape catalogue for body segmentation before chunking (#646).

Pins what the embedding path chunks for each shape of body: the text
``strip_for_embedding`` keeps and the chunk IDs (which bind each chunk's
index and text) that ``chunk_message`` produces from it. The digests
were taken on ``main`` before chunk kinds were added, so a shape whose
digest moves is a shape whose stored chunks moved. Synthetic text only.
"""

import hashlib
from email import message_from_bytes
from pathlib import Path

import pytest
from src.chunker import chunk_message
from src.quoting import strip_for_embedding

FIXTURES = Path(__file__).parent / "fixtures" / "chunker"

_ATTRIBUTION = "On Mon, Jan 1, 2024 at 10:00 AM Alice <alice@example.com> wrote:"
_OUTLOOK = "From: Alice\nSent: Monday, January 1, 2024 10:00 AM\nTo: Bob\nSubject: Plan\n"


def _fixture_body(name: str) -> str:
    msg = message_from_bytes((FIXTURES / name).read_bytes())
    part = next(p for p in msg.walk() if p.get_content_type() == "text/plain")
    payload = part.get_payload(decode=True)
    assert isinstance(payload, bytes)
    return payload.decode(part.get_content_charset() or "utf-8")


def _words(word: str, n: int) -> str:
    return " ".join([word] * n)


# Body shapes. Every entry is the raw ``body_text`` the parser hands the
# indexer.
SHAPES: dict[str, str] = {
    "empty": "",
    "whitespace_only": " \n\n\t\n",
    "plain_one_line": "Short note.",
    "plain_paragraphs": "\n\n".join(_words("alpha", 30) for _ in range(6)),
    "crlf_plain": "First line.\r\nSecond line.\r\n\r\nNew paragraph.",
    "unicode_8bit": "Grüße aus Köln.\n\n这是一个测试。\n\nCafé ☕",
    "folded_long_line": "x" * 4000 + " tail",
    "top_post_quote": f"Sounds good.\n\n{_ATTRIBUTION}\n> Original question.\n> More.",
    "inline_reply": (
        f"{_ATTRIBUTION}\n> First question?\nFirst answer.\n\n> Second question?\nSecond answer."
    ),
    "nested_quotes": "Reply.\n>> deep quote\n> shallow quote\n  > indented quote",
    "signature_then_quote": (
        f"Thanks,\nBob\n-- \nBob Example | PM\n\n{_ATTRIBUTION}\n> Can we meet?"
    ),
    "signature_only_delimiter_no_space": "Body text.\n--\nNot a delimiter.",
    "outlook_dashed": "Approved.\n\n-----Original Message-----\nFrom: a\nSubject: x\n\nOld.",
    "outlook_block": f"Approved.\n\n{_OUTLOOK}\nOld body.",
    "outlook_block_crlf": f"Approved.\r\n\r\n{_OUTLOOK}\r\nOld body.".replace("\n", "\r\n"),
    "gmail_forward": (
        "FYI below.\n\n---------- Forwarded message ---------\nFrom: Carol\n\nForwarded body."
    ),
    "apple_forward": "See this.\n\nBegin forwarded message:\n\nFrom: Carol\n\nForwarded body.",
    "wrapped_header": (
        "Yes.\n\nOn Mon, Jan 1, 2024 at 10:00 AM Alice Example <alice.example@example.com>\n"
        "wrote:\n> Question?"
    ),
    "wrapped_header_crlf": (
        "Yes.\r\n\r\nOn Mon, Jan 1, 2024 at 10:00 AM Alice <alice@example.com>\r\n"
        "wrote:\r\n> Question?"
    ),
    "german_header": "Danke.\n\nAm 1. Jan. 2024 schrieb Alice <alice@example.com>:\n> Frage?",
    "long_reply_header_like_line": "On " + "<a@b> " * 80 + "wrote:\n> quoted",
    "many_paragraphs_with_quotes": "\n\n".join(f"Answer {i}.\n> Question {i}?" for i in range(40)),
    # Fallback shapes: nothing survives stripping, so the original body
    # is chunked whole.
    "fallback_quote_only": f"{_ATTRIBUTION}\n> Original question text.",
    "fallback_quoted_lines": "> line one\n> line two\n> line three",
    "fallback_signature_only": "-- \nBob Example\nPhone 555 0100",
    "fallback_forward_only": "---------- Forwarded message ---------\nFrom: Carol\n\nBody.",
    "fallback_outlook_only": f"{_OUTLOOK}\nOld body.",
    "fallback_wrapped_header_only": (
        "On Mon, Jan 1, 2024 at 10:00 AM Alice Example <alice.example@example.com>\n"
        "wrote:\n> Question?"
    ),
    "fallback_long_quote": "\n".join(f"> {_words('beta', 40)}" for _ in range(60)),
    "fixture_long_body": _fixture_body("long_body.eml"),
    "fixture_quoted_reply": _fixture_body("quoted_reply.eml"),
    "fixture_short_reply": _fixture_body("short_reply.eml"),
    "fixture_unicode": _fixture_body("unicode.eml"),
}

# Small budgets so multi-paragraph shapes split into several chunks.
_TARGET, _MAX, _OVERLAP = 60, 90, 15


def _pin(body: str) -> str:
    stripped = strip_for_embedding(body)
    chunks = chunk_message(
        message_pk="m1",
        body_text=stripped,
        target_tokens=_TARGET,
        max_tokens=_MAX,
        overlap_tokens=_OVERLAP,
    )
    digest = hashlib.sha256(stripped.encode())
    for c in chunks:
        digest.update(b"\x00" + c.chunk_id.encode())
    return digest.hexdigest()[:16]


# Taken on main (646ba5e) before chunk kinds.
PINNED: dict[str, str] = {
    "apple_forward": "c910a290e0af2720",  # pragma: allowlist secret
    "crlf_plain": "6b38ad98c6a80f76",  # pragma: allowlist secret
    "empty": "e3b0c44298fc1c14",  # pragma: allowlist secret
    "fallback_forward_only": "a1bf8b852876fa73",  # pragma: allowlist secret
    "fallback_long_quote": "e1af3a770b439efc",  # pragma: allowlist secret
    "fallback_outlook_only": "aa36ff45e66f21da",  # pragma: allowlist secret
    "fallback_quote_only": "22af1b3b15328017",  # pragma: allowlist secret
    "fallback_quoted_lines": "2039f167dcd0ea02",  # pragma: allowlist secret
    "fallback_signature_only": "824bbf023ecf0f62",  # pragma: allowlist secret
    "fallback_wrapped_header_only": "c2b8f539dd2c7e46",  # pragma: allowlist secret
    "fixture_long_body": "5576c2faef68bcad",  # pragma: allowlist secret
    "fixture_quoted_reply": "9acb2f19ffd2ff36",  # pragma: allowlist secret
    "fixture_short_reply": "b6f08a113c22894d",  # pragma: allowlist secret
    "fixture_unicode": "040f5f3a82d97aa2",  # pragma: allowlist secret
    "folded_long_line": "f82400092a53143c",  # pragma: allowlist secret
    "german_header": "315af9d49ef9f1e1",  # pragma: allowlist secret
    "gmail_forward": "50846ec5bdecbc26",  # pragma: allowlist secret
    "inline_reply": "01eb789332d4e99b",  # pragma: allowlist secret
    "long_reply_header_like_line": "2280e95f67fafdab",  # pragma: allowlist secret
    "many_paragraphs_with_quotes": "3e89421b7696e7eb",  # pragma: allowlist secret
    "nested_quotes": "3a9ba730deb5f355",  # pragma: allowlist secret
    "outlook_block": "3508c0512c0ffc75",  # pragma: allowlist secret
    "outlook_block_crlf": "3508c0512c0ffc75",  # pragma: allowlist secret
    "outlook_dashed": "3508c0512c0ffc75",  # pragma: allowlist secret
    "plain_one_line": "4965036a17ab131d",  # pragma: allowlist secret
    "plain_paragraphs": "5e3628cdcd06e129",  # pragma: allowlist secret
    "signature_only_delimiter_no_space": "9bae845c1a978e14",  # pragma: allowlist secret
    "signature_then_quote": "99f5a49082724f35",  # pragma: allowlist secret
    "top_post_quote": "b3f3c354f3c2e6ae",  # pragma: allowlist secret
    "unicode_8bit": "0c8e1bab9d8fd8a3",  # pragma: allowlist secret
    "whitespace_only": "5602a9e26e0d4111",  # pragma: allowlist secret
    "wrapped_header": "af43482317d7bbc6",  # pragma: allowlist secret
    "wrapped_header_crlf": "af43482317d7bbc6",  # pragma: allowlist secret
}


class TestPinnedEmbeddingInput:
    @pytest.mark.parametrize("name", sorted(SHAPES))
    def test_shape_is_unchanged(self, name):
        assert _pin(SHAPES[name]) == PINNED[name]
