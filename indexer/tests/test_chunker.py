"""
Tests for src/chunker.py.

Covers paragraph packing, oversized-paragraph splitting, overlap, offset
round-tripping, normalization, determinism, and the small validation
surface. Fixtures live in ``indexer/tests/fixtures/chunker/`` as raw
``.eml`` files; the tests parse the bodies on the fly so the fixtures
can stay readable and editable.
"""

from email import message_from_bytes
from pathlib import Path

import pytest
from src.chunker import (
    chunk_message,
    estimate_tokens,
    normalize_body,
)
from src.quoting import strip_for_embedding

FIXTURES = Path(__file__).parent / "fixtures" / "chunker"


def _load_body(name: str) -> str:
    """Return the text/plain body of a fixture .eml file."""
    raw = (FIXTURES / name).read_bytes()
    msg = message_from_bytes(raw)
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                assert isinstance(payload, bytes)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset)
        raise AssertionError(f"no text/plain part in {name}")
    payload = msg.get_payload(decode=True)
    assert isinstance(payload, bytes)
    charset = msg.get_content_charset() or "utf-8"
    return payload.decode(charset)


class TestEstimateTokens:
    def test_empty_string_is_zero(self):
        assert estimate_tokens("") == 0

    def test_short_string_is_at_least_one(self):
        assert estimate_tokens("hi") >= 1

    def test_scales_roughly_with_length(self):
        # The real BPE tokenizer is non-linear (single tokens absorb
        # common substrings), but counts strictly increase with text
        # size for repeated content. Just confirm monotonicity.
        assert estimate_tokens("a" * 100) > estimate_tokens("a" * 10)

    def test_cjk_text_costs_more_tokens_than_chars_per_4_heuristic(self):
        """The previous 4-chars/token heuristic massively under-counted
        CJK because BPE tokenizes CJK characters roughly 1:1. A 500-
        char Chinese passage that the heuristic estimated at ~125
        tokens is actually ~500-1000 real tokens — enough to blow past
        nomic-embed-text's 2048 ceiling when packed into a chunk.

        This test pins the relationship qualitatively so a future
        regression to a char-based heuristic gets caught."""
        cjk = "中文测试字符串" * 50  # 350 CJK characters
        char_based_4 = len(cjk) // 4
        real = estimate_tokens(cjk)
        assert real > char_based_4 * 2, (
            f"BPE token count for CJK ({real}) should be at least 2x the "
            f"naive chars-per-4 heuristic ({char_based_4}); a regression "
            "to char-based estimation would re-open the embed-stage 500s."
        )

    def test_english_text_estimate_is_in_realistic_range(self):
        """English BPE typically averages ~4-5 chars/token. Check that
        a known English passage lands in that ballpark — both far above
        zero and well below the char count."""
        text = "The quick brown fox jumps over the lazy dog. " * 20
        n_chars = len(text)
        n_tokens = estimate_tokens(text)
        # Token count must be between 1/8 and 1/2 of char count for
        # ASCII English. Anything outside that range means the
        # tokenizer was misconfigured (e.g., counting bytes, counting
        # characters, or counting words).
        assert n_chars // 8 < n_tokens < n_chars // 2


class TestNormalizeBody:
    def test_empty_returns_empty(self):
        assert normalize_body("") == ""

    def test_crlf_collapses_to_lf(self):
        assert normalize_body("a\r\nb\r\nc") == "a\nb\nc"

    def test_lone_cr_collapses_to_lf(self):
        assert normalize_body("a\rb\rc") == "a\nb\nc"

    def test_runs_of_blank_lines_collapse_to_one(self):
        # Three or more newlines in a row are formatting noise; reduce to
        # two so the paragraph regex sees a single blank-line gap.
        assert normalize_body("para1\n\n\n\npara2") == "para1\n\npara2"

    def test_strips_leading_and_trailing_newlines(self):
        assert normalize_body("\n\nbody\n\n") == "body"

    def test_preserves_two_newlines_between_paragraphs(self):
        # Exactly one blank line between paragraphs should survive — that
        # is the canonical paragraph separator the chunker keys off.
        assert normalize_body("para1\n\npara2") == "para1\n\npara2"


class TestEmptyAndDegenerate:
    def test_empty_body_returns_no_chunks(self):
        assert chunk_message(message_pk="m1", body_text="") == []

    def test_whitespace_only_body_returns_no_chunks(self):
        assert chunk_message(message_pk="m1", body_text="   \n\n\t \n") == []

    def test_only_blank_lines_returns_no_chunks(self):
        assert chunk_message(message_pk="m1", body_text="\n\n\n\n\n") == []


class TestShortMessages:
    def test_short_single_paragraph_is_one_chunk(self):
        body = "This is a short reply confirming the meeting on Friday."
        chunks = chunk_message(message_pk="m1", body_text=body)
        assert len(chunks) == 1
        assert chunks[0].text == body
        assert chunks[0].chunk_index == 0

    def test_two_short_paragraphs_pack_into_one_chunk(self):
        body = "Hi Alice,\n\nThanks for the update. Looks great."
        chunks = chunk_message(message_pk="m1", body_text=body)
        assert len(chunks) == 1
        assert "Hi Alice" in chunks[0].text
        assert "Looks great" in chunks[0].text

    def test_token_est_is_positive_for_non_empty_chunk(self):
        chunks = chunk_message(message_pk="m1", body_text="hello world")
        assert chunks[0].token_est > 0


class TestPackingAndSplitting:
    def test_long_body_splits_into_multiple_chunks(self):
        # Each paragraph ~120 tokens; total well above target_tokens=350.
        para = "word " * 120
        body = "\n\n".join([para.strip()] * 6)
        chunks = chunk_message(message_pk="m1", body_text=body)
        assert len(chunks) >= 2

    def test_chunk_indexes_are_monotonic_starting_at_zero(self):
        para = "word " * 120
        body = "\n\n".join([para.strip()] * 6)
        chunks = chunk_message(message_pk="m1", body_text=body)
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))

    def test_chunks_respect_max_tokens(self):
        para = "word " * 120
        body = "\n\n".join([para.strip()] * 6)
        chunks = chunk_message(
            message_pk="m1",
            body_text=body,
            target_tokens=200,
            max_tokens=300,
            overlap_tokens=40,
        )
        # Some slack for the renderer including inter-paragraph whitespace.
        for c in chunks:
            assert c.token_est <= 320

    def test_oversized_single_paragraph_is_split(self):
        # One giant paragraph well above max_tokens; sentence splitter
        # should produce multiple chunks rather than emitting a single
        # over-budget chunk or losing content.
        sentence = "This is a sentence about a topic of moderate complexity. "
        body = sentence * 200  # ~11k chars, ~2700 tokens
        chunks = chunk_message(
            message_pk="m1",
            body_text=body,
            target_tokens=200,
            max_tokens=300,
            overlap_tokens=30,
        )
        assert len(chunks) >= 5
        # No chunk should be empty, and all should fit roughly under max.
        for c in chunks:
            assert c.text.strip() != ""

    def test_runaway_sentence_is_split_by_word(self):
        # No punctuation at all — sentence splitter cannot help, so word
        # fallback must kick in and still produce bounded chunks.
        body = ("word " * 2000).strip()
        chunks = chunk_message(
            message_pk="m1",
            body_text=body,
            target_tokens=200,
            max_tokens=300,
            overlap_tokens=30,
        )
        assert len(chunks) >= 5
        for c in chunks:
            assert c.token_est <= 320

    def test_no_chunk_is_empty(self):
        body = "para one.\n\npara two.\n\n\n\npara three.\n\n   \n\npara four."
        chunks = chunk_message(message_pk="m1", body_text=body)
        for c in chunks:
            assert c.text.strip() != ""

    def test_cjk_wall_without_whitespace_splits_under_max(self):
        """Regression: ``_split_by_word`` cannot reduce a single
        non-whitespace span. CJK text typically has no spaces, so a
        long Chinese passage used to pass straight through to the
        embedder as a single oversized chunk and trigger Ollama 500
        ('input length exceeds the context length'). The
        ``_split_by_tokens`` fallback handles this by slicing at
        embed-tokenizer boundaries."""
        # ~5,000 real BPE tokens worth of Chinese with no Latin
        # whitespace — under the old chunker this was 1 chunk; the
        # tokenizer fallback should produce many.
        body = "中文测试字符串，包含一些标点符号。" * 300
        chunks = chunk_message(message_pk="cjk-wall", body_text=body, max_tokens=500)
        assert len(chunks) > 1, "CJK wall must be split into multiple chunks"
        for c in chunks:
            assert c.token_est <= 500, (
                f"chunk {c.chunk_index} has {c.token_est} tokens > max_tokens=500"
            )

    def test_long_url_repeated_splits_under_max(self):
        """Same regression class for URLs: a paste of one giant URL
        repeated has no exploitable whitespace — the URL itself is
        one ``\\S+`` token. Tokenizer fallback must split it."""
        body = (
            "https://example.com/very/long/path/with/many/segments/and/a/query?"
            "param=value&other=stuff"
        ) * 50
        chunks = chunk_message(message_pk="url-wall", body_text=body, max_tokens=500)
        assert len(chunks) > 1
        for c in chunks:
            assert c.token_est <= 500

    def test_base64_wall_splits_under_max(self):
        """Same regression class for Base64: pasted attachment payload
        as text. No spaces, BPE tokenizer chops it into many tokens
        per character — a few hundred chars can blow past 500 tokens.
        Tokenizer fallback keeps each chunk under the ceiling."""
        body = (
            (
                "aGVsbG8gd29ybGQgaG93IGFyZSB5b3UgdG9kYXkgaXQgaXMgYSBuaWNlIGRheQ=="  # pragma: allowlist secret
            )
            * 50
        )
        chunks = chunk_message(message_pk="b64-wall", body_text=body, max_tokens=500)
        assert len(chunks) > 1
        for c in chunks:
            assert c.token_est <= 500


class TestOffsetRoundTrip:
    def test_offsets_round_trip_through_normalized_body(self):
        body = "Para one is short.\n\n" + ("longer content " * 80) + "\n\nPara three."
        chunks = chunk_message(
            message_pk="m1",
            body_text=body,
            target_tokens=120,
            max_tokens=200,
            overlap_tokens=20,
        )
        normalized = normalize_body(body)
        for c in chunks:
            # Slicing the normalized body at the stored offsets should
            # reproduce the chunk text exactly. This is the contract that
            # lets PR 2's "show me the source" path map a chunk back to
            # its position in the message.
            assert normalized[c.char_start : c.char_end] == c.text

    def test_offsets_are_monotonic_across_chunks(self):
        body = "\n\n".join(f"Paragraph number {i} with some filler text." for i in range(20))
        chunks = chunk_message(
            message_pk="m1",
            body_text=body,
            target_tokens=40,
            max_tokens=80,
            overlap_tokens=8,
        )
        # Without overlap we would expect strictly increasing starts; with
        # overlap, a later chunk may begin at or after the previous chunk's
        # start but never before it.
        for prev, nxt in zip(chunks, chunks[1:], strict=False):
            assert nxt.char_start >= prev.char_start
            assert nxt.char_end >= prev.char_end

    def test_unicode_offsets_are_correct(self):
        body = "Café meeting notes — résumé attached.\n\nLet's sync on naïve approach."
        chunks = chunk_message(message_pk="m1", body_text=body)
        normalized = normalize_body(body)
        for c in chunks:
            assert normalized[c.char_start : c.char_end] == c.text


class TestOverlap:
    def test_overlap_text_appears_in_consecutive_chunks(self):
        # Distinct paragraph markers make it easy to assert which content
        # the overlap actually carried forward.
        paragraphs = [f"Paragraph marker {i}: " + ("filler " * 20) for i in range(8)]
        body = "\n\n".join(paragraphs)
        chunks = chunk_message(
            message_pk="m1",
            body_text=body,
            target_tokens=80,
            max_tokens=150,
            overlap_tokens=30,
        )
        assert len(chunks) >= 2
        # At least one consecutive pair shares a paragraph marker.
        shared_any = False
        for prev, nxt in zip(chunks, chunks[1:], strict=False):
            for i in range(8):
                marker = f"Paragraph marker {i}"
                if marker in prev.text and marker in nxt.text:
                    shared_any = True
                    break
        assert shared_any, "expected overlap to carry at least one paragraph forward"

    def test_zero_overlap_produces_disjoint_chunks(self):
        paragraphs = [f"Marker {i}: " + ("filler " * 20) for i in range(8)]
        body = "\n\n".join(paragraphs)
        chunks = chunk_message(
            message_pk="m1",
            body_text=body,
            target_tokens=80,
            max_tokens=150,
            overlap_tokens=0,
        )
        assert len(chunks) >= 2
        for prev, nxt in zip(chunks, chunks[1:], strict=False):
            for i in range(8):
                marker = f"Marker {i}"
                # No marker should appear in both adjacent chunks when
                # overlap is disabled.
                assert not (marker in prev.text and marker in nxt.text)


class TestDeterminism:
    def test_same_input_produces_same_chunk_ids(self):
        body = "para one " * 50 + "\n\n" + "para two " * 50
        a = chunk_message(message_pk="m1", body_text=body)
        b = chunk_message(message_pk="m1", body_text=body)
        assert [c.chunk_id for c in a] == [c.chunk_id for c in b]
        assert [c.text for c in a] == [c.text for c in b]

    def test_different_message_pk_changes_chunk_ids(self):
        body = "stable content " * 100
        a = chunk_message(message_pk="m1", body_text=body)
        b = chunk_message(message_pk="m2", body_text=body)
        assert a and b
        assert a[0].chunk_id != b[0].chunk_id
        # Text is unchanged — only the id should shift, since the id binds
        # a chunk to its parent message.
        assert a[0].text == b[0].text

    def test_chunk_id_is_hex_sha256(self):
        chunks = chunk_message(message_pk="m1", body_text="hello world")
        assert len(chunks[0].chunk_id) == 64
        int(chunks[0].chunk_id, 16)  # raises if non-hex


class TestValidation:
    def test_target_tokens_must_be_positive(self):
        with pytest.raises(ValueError):
            chunk_message(message_pk="m1", body_text="x", target_tokens=0)

    def test_target_must_not_exceed_max(self):
        with pytest.raises(ValueError):
            chunk_message(message_pk="m1", body_text="x", target_tokens=600, max_tokens=500)

    def test_overlap_must_be_below_target(self):
        with pytest.raises(ValueError):
            chunk_message(
                message_pk="m1",
                body_text="x",
                target_tokens=100,
                max_tokens=200,
                overlap_tokens=100,
            )

    def test_overlap_cannot_be_negative(self):
        with pytest.raises(ValueError):
            chunk_message(
                message_pk="m1",
                body_text="x",
                target_tokens=100,
                max_tokens=200,
                overlap_tokens=-1,
            )


class TestModuleSurface:
    def test_message_chunk_is_frozen(self):
        chunks = chunk_message(message_pk="m1", body_text="hello")
        with pytest.raises(Exception):  # FrozenInstanceError subclasses AttributeError
            chunks[0].text = "mutated"  # type: ignore[misc]


class TestFixtures:
    def test_short_reply_fixture_is_one_chunk(self):
        body = _load_body("short_reply.eml")
        chunks = chunk_message(message_pk="fix-short", body_text=body)
        assert len(chunks) == 1
        assert "approved" in chunks[0].text.lower()

    def test_long_thread_body_fixture_splits(self):
        body = _load_body("long_body.eml")
        chunks = chunk_message(
            message_pk="fix-long",
            body_text=body,
            target_tokens=200,
            max_tokens=350,
            overlap_tokens=40,
        )
        # The fixture is long enough to require at least one split. The
        # exact chunk count depends on the embed model's tokenizer (real
        # BPE counts pack denser than the prior 4-chars/token heuristic),
        # so this only asserts the splitting behavior — not a specific
        # number — to stay stable across tokenizer changes.
        assert len(chunks) >= 2
        # First and last paragraphs should each appear somewhere in the
        # output — neither end of the message should be silently dropped.
        assert any("OPENING_MARKER" in c.text for c in chunks)
        assert any("CLOSING_MARKER" in c.text for c in chunks)

    def test_quoted_reply_fixture_uses_stripped_body(self):
        """The chunker is body-agnostic: it indexes whatever it is handed.
        For quoted replies we strip first (matching the embedding path in
        ``main.py``) so the chunks contain only the new content. The test
        documents that intended caller pattern."""
        raw_body = _load_body("quoted_reply.eml")
        stripped = strip_for_embedding(raw_body)
        chunks = chunk_message(message_pk="fix-quoted", body_text=stripped)
        joined = "\n".join(c.text for c in chunks)
        assert "Sounds good, ship it" in joined
        # The quoted history must not survive into the chunks.
        assert "On Mon, Jan 1, 2024" not in joined
        assert "Original question text" not in joined

    def test_unicode_fixture_round_trips(self):
        body = _load_body("unicode.eml")
        chunks = chunk_message(message_pk="fix-unicode", body_text=body)
        normalized = normalize_body(body)
        for c in chunks:
            assert normalized[c.char_start : c.char_end] == c.text
        joined = "\n".join(c.text for c in chunks)
        assert "café" in joined.lower()


# ---------------------------------------------------------------------------
# mean_vector — used by the indexer write path and the reconciler reap path
# to derive a thread-level vector from its component chunk vectors.
# ---------------------------------------------------------------------------


class TestMeanVector:
    def test_two_vectors_average_element_wise(self):
        from src.chunker import mean_vector

        result = mean_vector([[0.0, 1.0, 2.0], [2.0, 1.0, 0.0]])
        assert result == [1.0, 1.0, 1.0]

    def test_single_vector_is_identity(self):
        from src.chunker import mean_vector

        v = [0.5, 0.25, -0.1]
        assert mean_vector([v]) == v

    def test_empty_list_raises(self):
        import pytest
        from src.chunker import mean_vector

        with pytest.raises(ValueError, match="empty"):
            mean_vector([])

    def test_mismatched_dimensions_raises(self):
        import pytest
        from src.chunker import mean_vector

        with pytest.raises(ValueError, match="dimension"):
            mean_vector([[0.1, 0.2], [0.3, 0.4, 0.5]])
