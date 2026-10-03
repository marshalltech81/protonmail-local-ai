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
from src.chunker import (
    CHUNK_KINDS,
    chunk_message,
    chunk_segments,
    estimate_tokens,
    normalize_body,
)
from src.quoting import (
    _MAX_FALLBACK_SEGMENTS,
    Segment,
    segment_for_embedding,
    strip_for_embedding,
)

from tests.test_chunker import MAX_TOKENS_SHAPES, _catalogue_cases

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


def _production_chunks(body: str):
    """Chunk ``body`` the way the indexer does (``main._chunk_message_plan``)."""
    segments = segment_for_embedding(body)
    chunks = chunk_segments(
        message_pk="m1",
        segments=[(s.kind, s.text) for s in segments],
        target_tokens=_TARGET,
        max_tokens=_MAX,
        overlap_tokens=_OVERLAP,
    )
    return segments, chunks


# Shapes with no body text: ``strip_for_embedding`` falls back on the whole
# original, which the indexer now chunks as its kind runs, so these move.
FALLBACK_KINDS: dict[str, list[str]] = {
    "empty": [],
    "whitespace_only": [],
    "fallback_quote_only": ["quote"],
    "fallback_quoted_lines": ["quote"],
    "fallback_signature_only": ["signature"],
    "fallback_forward_only": ["forwarded"],
    "fallback_outlook_only": ["quote"],
    "fallback_wrapped_header_only": ["quote"],
    "fallback_long_quote": ["quote"],
}


class TestProductionPathMatchesPin:
    """Every shape with body text chunks exactly as it did on main."""

    @pytest.mark.parametrize("name", sorted(set(SHAPES) - set(FALLBACK_KINDS)))
    def test_body_shape_is_one_body_segment_with_pinned_chunks(self, name):
        body = SHAPES[name]
        segments, chunks = _production_chunks(body)
        assert segments == [Segment("body", strip_for_embedding(body))]
        digest = hashlib.sha256(segments[0].text.encode())
        for c in chunks:
            digest.update(b"\x00" + c.chunk_id.encode())
        assert digest.hexdigest()[:16] == PINNED[name]
        assert {c.kind for c in chunks} == {"body"}

    @pytest.mark.parametrize("name", sorted(FALLBACK_KINDS))
    def test_fallback_shape_is_tagged_by_kind(self, name):
        segments, chunks = _production_chunks(SHAPES[name])
        assert [s.kind for s in segments] == FALLBACK_KINDS[name]
        assert {c.kind for c in chunks} == set(FALLBACK_KINDS[name])


# Bodies with no body text whose lines change kind, with the kind of each run.
MIXED_FALLBACK: dict[str, tuple[str, list[str]]] = {
    "signature_then_quote": (
        f"-- \nBob Example\n\n{_ATTRIBUTION}\n> Can we meet?\n> Thanks",
        ["signature", "quote"],
    ),
    "forward_with_quote_inside": (
        "---------- Forwarded message ---------\nFrom: Carol\n\n> quoted in it\nmore text",
        ["forwarded", "quote", "forwarded"],
    ),
    "quote_then_outlook": (f"> earlier\n\n{_OUTLOOK}\nOld body.", ["quote"]),
    "quote_then_signature": ("> question?\n-- \nAlice", ["quote", "signature"]),
    "quote_then_apple_forward": (
        "> question?\n\nBegin forwarded message:\n\nFrom: Carol\nBody.",
        ["quote", "forwarded"],
    ),
    "outlook_dashed_then_signature": (
        "-----Original Message-----\nFrom: a\n\nOld.\n-- \nSig line",
        ["quote", "signature"],
    ),
    "crlf_signature_then_quote": ("-- \r\nBob\r\n\r\n> quoted\r\n", ["signature", "quote"]),
}


class TestFallbackSegments:
    @pytest.mark.parametrize("name", sorted(MIXED_FALLBACK))
    def test_runs_get_their_kinds(self, name):
        body, kinds = MIXED_FALLBACK[name]
        assert strip_for_embedding(body) == body  # a fallback shape
        assert [s.kind for s in segment_for_embedding(body)] == kinds

    @pytest.mark.parametrize("name", sorted(MIXED_FALLBACK))
    def test_no_text_is_lost(self, name):
        body, _ = MIXED_FALLBACK[name]
        segments = segment_for_embedding(body)
        assert "".join(s.text for s in segments).split() == body.split()

    def test_wrapped_reply_header_is_not_a_segment(self):
        segments = segment_for_embedding(SHAPES["fallback_wrapped_header_only"])
        assert segments == [Segment("quote", "> Question?")]

    def test_quote_and_signature_text_land_in_their_own_chunks(self):
        body = "-- \nBob Example | PM\n\n> Can we meet on Wednesday?"
        _, chunks = _production_chunks(body)
        assert [(c.kind, c.text) for c in chunks] == [
            ("signature", "-- \nBob Example | PM"),
            ("quote", "> Can we meet on Wednesday?"),
        ]
        assert [c.chunk_index for c in chunks] == [0, 1]

    def test_segment_count_is_capped_without_losing_text_or_mixing_kinds(self):
        # A body alternating kinds line by line: one segment (and so at
        # least one chunk and one embedding) per line without the cap.
        body = "".join(f"-- \nsig {i}\n> quote {i}\n" for i in range(5000))
        segments = segment_for_embedding(body)
        assert len(segments) <= _MAX_FALLBACK_SEGMENTS + 1
        assert sorted("".join(s.text for s in segments).split()) == sorted(body.split())
        for s in segments:
            quoted = [line.startswith(">") for line in s.text.splitlines()]
            assert all(quoted) if s.kind == "quote" else not any(quoted)


def _segment_ranges(segments) -> list[tuple[str, int, int]]:
    """``(kind, start, end)`` of each segment in the joined normalized text."""
    ranges, offset = [], 0
    for kind, text in segments:
        normalized = normalize_body(text)
        if not normalized:
            continue
        ranges.append((kind, offset, offset + len(normalized)))
        offset += len(normalized) + 2
    return ranges


def _assert_no_chunk_spans_kinds(segments, chunks, max_tokens):
    source = "\n\n".join(n for n in (normalize_body(t) for _, t in segments) if n)
    ranges = _segment_ranges(segments)
    for c in chunks:
        assert c.text == source[c.char_start : c.char_end]
        assert estimate_tokens(c.text) <= max_tokens
        owners = [k for k, start, end in ranges if start <= c.char_start and c.char_end <= end]
        assert owners == [c.kind]


class TestChunkNeverSpansKinds:
    """Invariant (#646): every chunk lies inside one segment and has its kind."""

    @pytest.mark.parametrize("name", sorted(MIXED_FALLBACK))
    @pytest.mark.parametrize("overlap", [0, 2])
    def test_mixed_fallback_bodies(self, name, overlap):
        body, _ = MIXED_FALLBACK[name]
        segments = [(s.kind, s.text) for s in segment_for_embedding(body)]
        chunks = chunk_segments(
            message_pk="m1",
            segments=segments,
            target_tokens=4,
            max_tokens=8,
            overlap_tokens=overlap,
        )
        assert chunks
        _assert_no_chunk_spans_kinds(segments, chunks, 8)

    @pytest.mark.parametrize(("name", "overlap"), _catalogue_cases())
    def test_rendered_ceiling_catalogue_across_segments(self, name, overlap):
        # The #208 / #550 shape catalogue, each shape as three segments of
        # different kinds: the rendered ceiling still holds, overlap is
        # never carried into the next kind, and no visible text is lost.
        body, target, max_tokens = MAX_TOKENS_SHAPES[name]
        segments = [("quote", body), ("signature", body), ("forwarded", body)]
        chunks = chunk_segments(
            message_pk="m1",
            segments=segments,
            target_tokens=target,
            max_tokens=max_tokens,
            overlap_tokens=overlap,
        )
        _assert_no_chunk_spans_kinds(segments, chunks, max_tokens)
        indexes = [c.chunk_index for c in chunks]
        assert indexes == sorted(set(indexes))
        source = "\n\n".join(normalize_body(t) for _, t in segments)
        covered = bytearray(len(source))
        for c in chunks:
            covered[c.char_start : c.char_end] = b"\x01" * (c.char_end - c.char_start)
        assert all(covered[i] or ch.isspace() for i, ch in enumerate(source))

    def test_one_segment_is_chunk_message(self):
        body, target, max_tokens = MAX_TOKENS_SHAPES["plain_paragraphs"]
        kwargs = {"target_tokens": target, "max_tokens": max_tokens, "overlap_tokens": 25}
        assert chunk_segments(message_pk="m1", segments=[("quote", body)], **kwargs) == (
            chunk_message(message_pk="m1", body_text=body, kind="quote", **kwargs)
        )

    def test_chunk_ids_keep_their_shape(self):
        chunks = chunk_segments(message_pk="m1", segments=[("quote", "> a"), ("signature", "-- ")])
        assert [c.kind for c in chunks] == ["quote", "signature"]
        for c in chunks:
            expected = hashlib.sha256(f"m1\x00{c.chunk_index}\x00{c.text}".encode()).hexdigest()
            assert c.chunk_id == expected


class TestKindValidation:
    def test_unknown_kind_is_rejected(self):
        with pytest.raises(ValueError, match="unknown chunk kind"):
            chunk_segments(message_pk="m1", segments=[("headline", "text")])  # type: ignore[list-item]

    def test_enum_is_closed(self):
        assert CHUNK_KINDS == ("body", "quote", "signature", "forwarded", "calendar", "attachment")


class TestSegmentationWorkBound:
    def test_each_line_is_checked_once(self, monkeypatch):
        # Segmentation adds no rescans: one marker check and at most one
        # reply-header check per line, on a large body of short lines.
        import time

        import src.quoting as quoting

        calls = {"cut": 0, "header": 0}
        real_cut, real_header = quoting._hard_cut_kind, quoting._is_reply_header

        def counting_cut(line):
            calls["cut"] += 1
            return real_cut(line)

        def counting_header(line):
            calls["header"] += 1
            return real_header(line)

        monkeypatch.setattr(quoting, "_hard_cut_kind", counting_cut)
        monkeypatch.setattr(quoting, "_is_reply_header", counting_header)
        body = "".join(f"> q{i}\n-- \nOn {i}\n" for i in range(33_000))
        started = time.monotonic()
        segments = segment_for_embedding(body)
        assert time.monotonic() - started < 20.0
        lines = body.count("\n")
        assert calls["cut"] == lines
        assert calls["header"] <= lines
        assert len(segments) <= _MAX_FALLBACK_SEGMENTS + 1
