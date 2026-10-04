"""
Tests for src/chunker.py.

Covers paragraph packing, oversized-paragraph splitting, overlap, offset
round-tripping, normalization, determinism, and the small validation
surface. Fixtures live in ``indexer/tests/fixtures/chunker/`` as raw
``.eml`` files; the tests parse the bodies on the fly so the fixtures
can stay readable and editable.
"""

import hashlib
from email import message_from_bytes
from pathlib import Path

import pytest
from src.chunker import (
    chunk_message,
    chunk_segments,
    estimate_tokens,
    normalize_body,
)
from src.quoting import segment_for_embedding

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

    def test_cjk_text_uses_real_bpe_not_char_heuristic(self):
        """A regression to a 4-chars/token heuristic would return
        ``char_count // 4`` regardless of script. CJK is the cleanest
        discriminator: even Qwen3's efficient multilingual BPE produces
        noticeably more tokens for dense multi-byte text than the naive
        heuristic, so a comfortable margin above ``char_count // 4``
        catches a silent fallback without being brittle to tokenizer
        upgrades."""
        cjk = "中文测试字符串" * 50  # 350 CJK characters
        char_based_4 = len(cjk) // 4
        real = estimate_tokens(cjk)
        assert real > int(char_based_4 * 1.5), (
            f"BPE token count for CJK ({real}) should comfortably exceed "
            f"the naive chars-per-4 heuristic ({char_based_4}); a "
            "regression to char-based estimation would silently undersize "
            "CJK chunks."
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

    def test_large_inputs_do_not_pollute_lru_cache(self):
        """Large strings (above the cache-threshold) are computed but
        NOT inserted into the LRU. Without this gate, an attacker-
        controlled email body or attachment text could pin megabytes
        of strings in ``_cached_estimate_tokens.cache``, pushing the
        indexer toward OOM during a backfill.
        """
        from src.chunker import (
            _TOKEN_ESTIMATE_CACHE_THRESHOLD_BYTES,
            _cached_estimate_tokens,
        )

        _cached_estimate_tokens.cache_clear()
        # A unique large string that nothing else in the test process
        # has cached. Use a distinct prefix so other tests' inputs
        # cannot collide with this one's cache key.
        big = "z" * (_TOKEN_ESTIMATE_CACHE_THRESHOLD_BYTES + 1)
        before = _cached_estimate_tokens.cache_info().currsize
        _ = estimate_tokens(big)
        after = _cached_estimate_tokens.cache_info().currsize
        assert after == before, (
            "estimate_tokens must not insert oversized strings into the "
            "LRU; doing so would let attacker-controlled inputs pin "
            "megabytes of memory in the cache."
        )

    def test_small_inputs_do_use_lru_cache(self):
        """Counterpart to the threshold test: a string under the
        threshold is cached, so a second call hits the cache (not
        the tokenizer) and the LRU's hit counter advances.
        """
        from src.chunker import _cached_estimate_tokens

        _cached_estimate_tokens.cache_clear()
        small = "small string under threshold"
        estimate_tokens(small)
        hits_before = _cached_estimate_tokens.cache_info().hits
        estimate_tokens(small)
        hits_after = _cached_estimate_tokens.cache_info().hits
        assert hits_after == hits_before + 1, (
            "second call on the same short text must hit the LRU; "
            "without caching the chunker re-encodes every span at "
            "every greedy-pack and overlap-tail evaluation."
        )

    def test_non_ascii_text_obeys_byte_threshold_not_char_threshold(self):
        """Regression: the cache threshold is a memory bound, so it
        must gate on UTF-8 byte length — not character count. A
        multilingual attachment whose char count is under the threshold
        but whose UTF-8 size is several multiples of it would otherwise
        bypass the documented memory cap, defeating the defense against
        attacker-controlled multilingual content pinning megabytes of
        strings in the LRU.
        """
        from src.chunker import (
            _TOKEN_ESTIMATE_CACHE_THRESHOLD_BYTES,
            _cached_estimate_tokens,
        )

        # Each emoji "🚀" is 4 UTF-8 bytes. A 3000-char string of them is
        # ~12 KB UTF-8 — over the 8 KB threshold — but ``len(text)`` is
        # 3000 (well under the threshold if we naively gated on char
        # count). The cache must NOT accept it.
        big_emoji = "🚀" * 3000
        assert len(big_emoji) < _TOKEN_ESTIMATE_CACHE_THRESHOLD_BYTES, (
            "precondition: char count must be below threshold to exercise "
            "the bytes-vs-chars distinction"
        )
        assert len(big_emoji.encode("utf-8")) > _TOKEN_ESTIMATE_CACHE_THRESHOLD_BYTES, (
            "precondition: UTF-8 byte length must exceed threshold so the "
            "byte-gate has something to defend against"
        )

        _cached_estimate_tokens.cache_clear()
        before = _cached_estimate_tokens.cache_info().currsize
        _ = estimate_tokens(big_emoji)
        after = _cached_estimate_tokens.cache_info().currsize
        assert after == before, (
            "non-ASCII string above the BYTE threshold must NOT enter the "
            "LRU even when char count is below threshold; otherwise an "
            "attacker can pin megabytes of multilingual content in cache"
        )


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
        embedder as a single oversized chunk and trigger an embedding
        service 500 ('input length exceeds the context length'). The
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

    def test_long_punctuation_run_scans_in_linear_time(self):
        """Regression (#221): a punctuation run not followed by
        whitespace made the sentence regex retry the lookahead from
        every position in the run — quadratic, so a 128 KB body
        stalled the single indexing worker. 64K dots took ~20 s
        before the fix."""
        import time

        from src.chunker import _SENTENCE_END_RE

        text = "." * 64_000 + "x"
        started = time.monotonic()
        matches = list(_SENTENCE_END_RE.finditer(text))
        assert time.monotonic() - started < 1.0
        assert matches == []

    def test_sentence_boundaries_unchanged_by_linear_regex(self):
        """The #221 rewrite must find exactly the boundaries the old
        pattern did: whole punctuation runs followed by whitespace or
        end of text."""
        from src.chunker import _SENTENCE_END_RE

        text = "One. Two!! Three?! v1.2 ok... end...x Last."
        ends = [m.group() for m in _SENTENCE_END_RE.finditer(text)]
        spans = [m.span() for m in _SENTENCE_END_RE.finditer(text)]
        assert ends == [".", "!!", "?!", "...", "."]
        assert spans == [(3, 4), (8, 10), (16, 18), (26, 29), (42, 43)]


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


class TestTruncateToTokens:
    """``truncate_to_tokens`` is the token-based replacement for the
    previous char-based ``THREAD_BODY_TEXT_MAX_CHARS`` truncation. A
    char cap under-counted CJK / URL / Base64 / dense content by 4-6×;
    a token cap aligns with the embed model's actual context budget.
    """

    def test_short_text_unchanged(self):
        from src.chunker import truncate_to_tokens

        text = "short body"
        # Cap well above the token count → return verbatim.
        assert truncate_to_tokens(text, max_tokens=100) == text

    def test_empty_text_returns_empty(self):
        from src.chunker import truncate_to_tokens

        assert truncate_to_tokens("", max_tokens=100) == ""

    def test_zero_max_tokens_returns_empty(self):
        from src.chunker import truncate_to_tokens

        # Defensive: a zero cap means "no budget" → drop the text.
        assert truncate_to_tokens("anything", max_tokens=0) == ""

    def test_long_text_is_cut_to_token_budget(self):
        from src.chunker import estimate_tokens, truncate_to_tokens

        # Build a body whose token count comfortably exceeds the cap.
        body = ("The quick brown fox jumps over the lazy dog. " * 100).strip()
        cap = 50
        assert estimate_tokens(body) > cap, (
            "precondition: input must exceed the cap to exercise the truncation branch"
        )
        truncated = truncate_to_tokens(body, max_tokens=cap)
        assert estimate_tokens(truncated) <= cap, (
            "post-truncation token count must respect the cap; the "
            "tokenizer's own offsets are the source of truth so the "
            "cut is on a real token boundary"
        )
        # And the truncation is a prefix of the source — no rewriting.
        assert body.startswith(truncated)

    def test_cjk_text_cut_on_codepoint_boundary(self):
        # Multi-byte codepoints must not be split. The tokenizer's
        # offset metadata is character-aligned, so the cut is always
        # safe; this test pins that invariant against a regression to
        # naive byte slicing.
        from src.chunker import truncate_to_tokens

        body = "中文测试" * 100  # 400 CJK chars
        truncated = truncate_to_tokens(body, max_tokens=10)
        # Slicing on a non-codepoint boundary would corrupt the string;
        # ``encode("utf-8")`` round-trips losslessly when no codepoint
        # was split.
        assert truncated == truncated.encode("utf-8").decode("utf-8")


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
        For quoted replies the indexer segments first (``main.py`` chunks
        ``segment_for_embedding``'s runs with ``chunk_segments``) so the
        chunks contain only the new content. The test documents that
        caller pattern."""
        raw_body = _load_body("quoted_reply.eml")
        segments = segment_for_embedding(raw_body)
        chunks = chunk_segments(
            message_pk="fix-quoted", segments=[(s.kind, s.text) for s in segments]
        )
        assert {c.kind for c in chunks} == {"body"}
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


def _prose_pin_body() -> str:
    """Prose that exercises every split path and every edge trim.

    Paragraph packing with overlap, a sentence split, a word split of a
    punctuation-free run, a paragraph indented with spaces, and
    sentences separated by runs of spaces and newlines.
    """
    paragraphs = [f"Paragraph {i} opens here. " + ("filler words follow " * 15) for i in range(6)]
    paragraphs.append("   Indented with spaces. " + ("Another sentence ends.  " * 40))
    paragraphs.append(" ".join(f"word{i}" for i in range(400)))
    paragraphs.append("Closing line one.\n  Closing line two.\n   \nTail.")
    return "\n\n".join(paragraphs)


# Pinned on main before #433: prose has no tabs opening a line, so its
# chunk count and IDs (which bind each chunk's index and text) must not
# move with the fix.
PROSE_PIN_COUNT = 27
PROSE_PIN_DIGEST = (
    "83ce2d6b0f0e3729a5c1db2668713e28bfc776933ede75ef67b796d227107a26"  # pragma: allowlist secret
)


class TestLeadingTabsAtSplits:
    """#433: a split keeps the tabs that open a line.

    The xlsx extractor writes each row as tab-separated cells, with empty
    leading cells as empty fields, so the tabs at the start of a line
    carry the column of its first value. Leading spaces and newlines at
    a chunk edge are still trimmed, and so are tabs that do not open a
    line.
    """

    @staticmethod
    def _ids_digest(chunks) -> str:
        return hashlib.sha256("\n".join(c.chunk_id for c in chunks).encode()).hexdigest()

    @staticmethod
    def _sheet_rows(n: int) -> list[str]:
        # One value per row behind 0-3 empty leading cells, so every
        # word split lands at the start of a row.
        return ["\t" * (i % 4) + f"value{i}" for i in range(n)]

    def test_prose_chunks_are_unchanged(self):
        chunks = chunk_message(
            message_pk="m1",
            body_text=_prose_pin_body(),
            target_tokens=60,
            max_tokens=90,
            overlap_tokens=15,
        )
        assert len(chunks) == PROSE_PIN_COUNT
        assert self._ids_digest(chunks) == PROSE_PIN_DIGEST
        for c in chunks:
            assert c.text == c.text.strip()

    def test_word_split_keeps_leading_empty_cells(self):
        rows = self._sheet_rows(300)
        body = "Sheet: Data\n" + "\n".join(rows)
        chunks = chunk_message(
            message_pk="m1", body_text=body, target_tokens=40, max_tokens=60, overlap_tokens=0
        )
        assert len(chunks) >= 5
        lines = [line for c in chunks for line in c.text.split("\n")]
        # Every row keeps its column alignment in whichever chunk holds it.
        assert lines == ["Sheet: Data", *rows]
        assert any(c.text.startswith("\t") for c in chunks)

    def test_sentence_split_keeps_leading_empty_cells(self):
        rows = [("\t" * (i % 3)) + f"Value {i} ends." for i in range(200)]
        chunks = chunk_message(
            message_pk="m1",
            body_text="\n".join(rows),
            target_tokens=40,
            max_tokens=60,
            overlap_tokens=0,
        )
        assert len(chunks) >= 5
        lines = [line for c in chunks for line in c.text.split("\n")]
        assert lines == rows
        assert any(c.text.startswith("\t") for c in chunks)

    def test_paragraph_start_keeps_leading_empty_cells(self):
        # Two sheets, each one paragraph whose first row opens with empty
        # cells; the second sheet starts a new chunk.
        sheet = "\n".join(["\t\tfirst"] + ["a\tb\tc"] * 20)
        chunks = chunk_message(
            message_pk="m1",
            body_text=sheet + "\n\n" + sheet,
            target_tokens=60,
            max_tokens=120,
            overlap_tokens=0,
        )
        assert len(chunks) == 2
        assert all(c.text.startswith("\t\tfirst\n") for c in chunks)

    def test_tabs_inside_a_row_are_trimmed_at_a_split(self):
        # A split that lands mid-row cannot know the column of the first
        # value it keeps, so it keeps none of the gap tabs, as before.
        rows = [f"key{i}\t\tvalue{i}" for i in range(300)]
        chunks = chunk_message(
            message_pk="m1",
            body_text="\n".join(rows),
            target_tokens=40,
            max_tokens=60,
            overlap_tokens=0,
        )
        assert len(chunks) >= 5
        for c in chunks:
            assert not c.text.startswith((" ", "\t", "\n"))

    def test_tab_run_longer_than_a_chunk_loses_its_column(self):
        # Documented limitation (PR #546 review round 1): a row whose
        # empty leading cells alone exceed max_tokens cannot keep them in
        # one chunk. The value survives, under the ceiling, without them.
        chunks = chunk_message(
            message_pk="m1",
            body_text="\t" * 20000 + "value",
            target_tokens=40,
            max_tokens=60,
            overlap_tokens=0,
        )
        assert [c.text for c in chunks] == ["value"]
        assert chunks[0].token_est <= 60

    def test_spaces_before_line_opening_tabs_are_trimmed(self):
        chunks = chunk_message(message_pk="m1", body_text="  \t\tvalue\nnext")
        assert [c.text for c in chunks] == ["value\nnext"]

    def test_sheet_chunks_round_trip_fit_max_and_are_deterministic(self):
        body = "\n".join(self._sheet_rows(300))
        a = chunk_message(
            message_pk="m1", body_text=body, target_tokens=40, max_tokens=60, overlap_tokens=0
        )
        b = chunk_message(
            message_pk="m1", body_text=body, target_tokens=40, max_tokens=60, overlap_tokens=0
        )
        assert [c.chunk_id for c in a] == [c.chunk_id for c in b]
        normalized = normalize_body(body)
        for c in a:
            assert normalized[c.char_start : c.char_end] == c.text
            assert c.token_est <= 60


def _words(n: int) -> str:
    """A paragraph of ``n`` real tokens: ``alpha`` then ``n - 1`` × `` alpha``."""
    return " ".join(["alpha"] * n)


# Shape catalogue for the rendered-chunk ceiling (#208, #550). Each entry is
# (body, target_tokens, max_tokens); every shape runs with overlap 0 and with
# overlap at a quarter of the target. Synthetic text only.
MAX_TOKENS_SHAPES: dict[str, tuple[str, int, int]] = {
    "plain_paragraphs": ("\n\n".join(_words(40) for _ in range(12)), 100, 150),
    "sentence_split": ("A sentence of moderate length ends here. " * 120, 40, 60),
    "near_limit_after_multi_span": (
        "\n\n".join([_words(400), _words(400), _words(1400)]),
        1000,
        1500,
    ),
    "many_tiny_paragraphs": ("\n\n".join(["alpha"] * 200), 60, 60),
    "tab_gap_in_paragraph": ("a\n" + "\t" * 20000 + "value\nb", 40, 60),
    "space_gap_in_paragraph": ("a " + " " * 20000 + "value b", 40, 60),
    "newline_gap_in_paragraph": ("a\n" + " \n" * 3000 + "value", 40, 60),
    "tab_line_between_paragraphs": (
        "\n\n".join(["word"] * 5) + "\n" + "\t" * 5000 + "\n" + "tail",
        40,
        60,
    ),
    "xlsx_rows_many_empty_cells": (
        "\n".join("\t" * (300 * (i % 3)) + f"value{i}" for i in range(200)),
        40,
        60,
    ),
    "xlsx_rows_short": ("\n".join("\t" * (i % 4) + f"value{i}" for i in range(300)), 40, 60),
    "cjk_without_spaces": ("这是一个测试句子没有空格" * 400, 40, 60),
    "single_over_max_word": ("x" * 5000 + " y", 40, 60),
    # Review round 1: `` differ`` is one token, ``differ`` two, so trimming
    # the paragraph's leading space takes it from 1,500 tokens to 1,501.
    "leading_space_trim_raises_count": (" differ" * 1500, 1000, 1500),
    # Review round 1: the second paragraph is the overlap seed when a long
    # whitespace line keeps the third from fitting beside it.
    "overlap_seed_before_long_gap": (
        "alpha alpha\n\nbeta beta\n" + "\t" * 5000 + "\ngamma",
        4,
        60,
    ),
    # Review round 2: the 97-token sub-span is the overlap seed, and only
    # the rendered check finds the group over (its 2-token sibling joins
    # at 102 tokens), so the cut must not emit the seed on its own.
    "overlap_seed_cut_by_rendered_check": ("alpha\n\n" + "\u8fd9\u662fword  " * 21, 100, 100),
}

# Pinned on main before the #208/#550 fix: these shapes already met the
# rendered ceiling there, so their chunk IDs (which bind each chunk's index
# and text) must not move with the fix.
MAX_TOKENS_PINNED_CASES: list[tuple[str, int]] = [
    ("plain_paragraphs", 0),
    ("plain_paragraphs", 25),
    ("sentence_split", 0),
    ("sentence_split", 10),
    ("near_limit_after_multi_span", 0),
    # Not ``newline_gap_in_paragraph``: it also fit on main, and its chunk
    # texts are unchanged (``a`` and ``value``), but its thousands of
    # whitespace-only lines pack into a different number of groups once
    # the gaps between them are counted (#550). Those groups render empty
    # and are skipped, so only the index of ``value``, and with it its ID,
    # moves.
    ("xlsx_rows_many_empty_cells", 0),
    ("xlsx_rows_many_empty_cells", 10),
    ("xlsx_rows_short", 0),
    ("xlsx_rows_short", 10),
    ("cjk_without_spaces", 0),
    ("cjk_without_spaces", 10),
    ("single_over_max_word", 0),
    ("single_over_max_word", 10),
]
MAX_TOKENS_PIN_DIGEST = (
    "a3ae3c0432873b0e43bb11555f3303390f8151e69147c24b3dc887b3fd9d7e85"  # pragma: allowlist secret
)


def _catalogue_cases() -> list[tuple[str, int]]:
    """Every catalogue shape, once with overlap 0 and once with overlap > 0."""
    return [
        (name, overlap)
        for name, (_, target, _) in MAX_TOKENS_SHAPES.items()
        for overlap in (0, target // 4)
    ]


def _catalogue_chunks(name: str, overlap: int):
    body, target, max_tokens = MAX_TOKENS_SHAPES[name]
    return chunk_message(
        message_pk="m1",
        body_text=body,
        target_tokens=target,
        max_tokens=max_tokens,
        overlap_tokens=overlap,
    )


class TestRenderedChunkCeiling:
    """Every rendered chunk's real token count is <= max_tokens (#208, #550)."""

    @pytest.mark.parametrize(("name", "overlap"), _catalogue_cases())
    def test_rendered_chunks_fit_max_tokens(self, name, overlap):
        body, _, max_tokens = MAX_TOKENS_SHAPES[name]
        chunks = _catalogue_chunks(name, overlap)
        assert chunks
        normalized = normalize_body(body)
        covered = bytearray(len(normalized))
        for c in chunks:
            assert estimate_tokens(c.text) <= max_tokens
            assert c.token_est == estimate_tokens(c.text)
            assert c.text and c.text == normalized[c.char_start : c.char_end]
            covered[c.char_start : c.char_end] = b"\x01" * (c.char_end - c.char_start)
        # Strictly increasing, not contiguous: a group of whitespace-only
        # paragraphs renders empty and is skipped with its index.
        indexes = [c.chunk_index for c in chunks]
        assert indexes == sorted(set(indexes))
        # No visible text is lost: every non-whitespace char sits in a chunk.
        assert all(covered[i] or ch.isspace() for i, ch in enumerate(normalized))
        # No chunk is only overlap: each one ends past the chunk before it.
        for prev, nxt in zip(chunks, chunks[1:], strict=False):
            assert nxt.char_end > prev.char_end

    def test_whitespace_gap_between_sub_spans_is_not_rendered_into_one_chunk(self):
        # #550: the splitters drop the 20,000 tabs between ``a`` and
        # ``value``; the packer counted only the 3 tokens of ``a``,
        # ``value`` and ``b``, and rendering the group put the tabs back
        # (a 1,255-token chunk).
        chunks = chunk_message(
            message_pk="m1",
            body_text="a\n" + "\t" * 20000 + "value\nb",
            target_tokens=40,
            max_tokens=60,
            overlap_tokens=0,
        )
        assert [c.text for c in chunks] == ["a", "value\nb"]

    def test_text_that_tokenizes_longer_joined_is_cut_by_its_rendered_count(self):
        # BPE counts are not additive. Each paragraph word-splits into a
        # 97-token and a 2-token sub-span with a 1-token gap: 100 counted
        # apart, which the packer accepts at max 100, but 102 joined. The
        # rendered count decides, so each such group is cut.
        unit = "这是word  "
        paragraph = unit * 21
        body = "\n\n".join([paragraph] * 50)
        chunks = chunk_message(
            message_pk="m1", body_text=body, target_tokens=100, max_tokens=100, overlap_tokens=0
        )
        assert max(estimate_tokens(c.text) for c in chunks) <= 100
        assert sum(c.text.count(unit.strip()) for c in chunks) == 21 * 50
        assert len(chunks) == 100

    def test_cut_takes_a_fitting_prefix_by_binary_search(self, monkeypatch):
        # Twenty 20-token paragraphs that ``_fit_rendered`` gets as one
        # group under a 100-token ceiling: each run must fit, end where
        # the next span would overflow it, and cost O(log n) renders.
        import src.chunker as chunker

        source = "\n\n".join(_words(20) for _ in range(20))
        group = chunker._paragraph_spans(source)
        calls = 0
        real_render = chunker._render_group

        def counting_render(src, spans):
            nonlocal calls
            calls += 1
            return real_render(src, spans)

        monkeypatch.setattr(chunker, "_render_group", counting_render)
        runs = chunker._fit_rendered(source, group, 100)
        assert [s for run in runs for s in run] == group
        for run, nxt in zip(runs, runs[1:], strict=False):
            assert estimate_tokens(real_render(source, run)[0]) <= 100
            assert estimate_tokens(real_render(source, [*run, nxt[0]])[0]) > 100
        assert [len(run) for run in runs] == [4, 4, 4, 4, 4]
        # One full render per cut plus about log2(20) for its search; one
        # render per span dropped would be over 50.
        assert calls <= len(runs) * 6

    def test_ceiling_check_work_stays_linear_when_every_group_is_cut(self, monkeypatch):
        # Worst case for the rendered check on a large body: every packed
        # group overflows and is cut. Renders stay a small constant per
        # chunk, and the body chunks well inside the time bound.
        import time

        import src.chunker as chunker

        calls = 0
        real_render = chunker._render_group

        def counting_render(source, group):
            nonlocal calls
            calls += 1
            return real_render(source, group)

        monkeypatch.setattr(chunker, "_render_group", counting_render)
        paragraph = "这是word  " * 21
        body = "\n\n".join([paragraph] * 2000)
        started = time.monotonic()
        chunks = chunk_message(
            message_pk="m1", body_text=body, target_tokens=100, max_tokens=100, overlap_tokens=0
        )
        assert time.monotonic() - started < 20.0
        assert len(chunks) == 4000
        assert all(c.token_est <= 100 for c in chunks)
        # One render per paragraph (``_enforce_max_tokens`` measures it),
        # one per packed group (the check that finds it over), and one per
        # chunk; a 2-span group's cut needs no search.
        assert calls <= 2000 + 2000 + len(chunks)

    def test_long_gaps_are_packed_out_before_the_rendered_check(self, monkeypatch):
        # Paragraphs separated by long whitespace-only lines. The packer
        # counts each gap once, so no group holds one and the rendered
        # check renders each gap at most once. Without that, groups of
        # dozens of spans and gaps would be rendered and cut repeatedly.
        import src.chunker as chunker

        rendered_chars = 0
        real_render = chunker._render_group

        def counting_render(source, group):
            nonlocal rendered_chars
            if group:
                rendered_chars += group[-1].end - group[0].start
            return real_render(source, group)

        monkeypatch.setattr(chunker, "_render_group", counting_render)
        body = ("word\n" + "\t" * 5000 + "\n") * 200
        chunks = chunk_message(
            message_pk="m1", body_text=body, target_tokens=60, max_tokens=60, overlap_tokens=0
        )
        assert [c.text for c in chunks] == ["word"] * 200
        assert rendered_chars <= 2 * len(body)

    def test_in_budget_shapes_are_unchanged(self):
        ids = [c.chunk_id for case in MAX_TOKENS_PINNED_CASES for c in _catalogue_chunks(*case)]
        digest = hashlib.sha256("\n".join(ids).encode()).hexdigest()
        assert digest == MAX_TOKENS_PIN_DIGEST

    def test_overlap_seed_is_dropped_when_next_span_would_overflow(self):
        # #208: 400 + 400 tokens, then a 1,400-token paragraph. The overlap
        # carried from the first chunk (the whole 400-token paragraph) used
        # to sit in front of the 1,400 tokens, giving a 1,801-token chunk.
        p1, p2, p3 = _words(400), _words(400), _words(1400)
        chunks = chunk_message(
            message_pk="m1",
            body_text="\n\n".join([p1, p2, p3]),
            target_tokens=1000,
            max_tokens=1500,
            overlap_tokens=150,
        )
        assert [c.text for c in chunks] == [p1 + "\n\n" + p2, p3]


# Shapes that reach the sentence and word splitters (#673): paragraphs
# over ``max_tokens``, split at sentence ends, then at words. Synthetic
# text only. Their chunks were pinned on main before the splitters
# stopped re-tokenizing a growing prefix per sentence and per word.
_MARKER_UNIT = "-- \nsig line\n> quoted line\nBegin forwarded message:\nfwd line\n"
SPLIT_SHAPES: dict[str, tuple[str, int, int]] = {
    "words_one_paragraph": (" ".join(f"w{i % 97}" for i in range(3000)), 40, 60),
    "sentences_one_paragraph": ("Short one. A bit longer sentence here! Why? " * 300, 40, 60),
    "punctuation_before_newlines": ("end.\n" * 800 + "wow!!!\n\n" * 3, 40, 60),
    "lines_of_short_words": ("-- \nsig line\n> q\n" * 600, 40, 60),
    "long_and_short_words": (" ".join(("x" * (i % 40)) or "y" for i in range(2000)), 40, 60),
    "emoji_and_cjk_words": (" ".join(["\U0001f600\U0001f680", "这是", "word"] * 700), 40, 60),
    "multiple_spaces": ("alpha   beta\t\tgamma  \n  delta " * 500, 40, 60),
    "marker_lines_production_budgets": (_MARKER_UNIT * 2000, 1000, 1500),
    "words_production_budgets": (" ".join(f"t{i % 1000}" for i in range(20_000)), 1000, 1500),
}
SPLIT_PIN_DIGEST = (
    "4a05068157841cdaf4dbc2b9d5172e8d422276841d1ff1a7122f1dbfc14a5808"  # pragma: allowlist secret
)


def _split_cases() -> list[tuple[dict[str, tuple[str, int, int]], str, int]]:
    """Every split shape and catalogue shape, with overlap 0 and > 0."""
    return [
        (shapes, name, overlap)
        for shapes in (SPLIT_SHAPES, MAX_TOKENS_SHAPES)
        for name, (_, target, _) in shapes.items()
        for overlap in (0, target // 4)
    ]


def _split_pin_digest() -> str:
    digest = hashlib.sha256()
    for shapes, name, overlap in _split_cases():
        body, target, max_tokens = shapes[name]
        for c in chunk_message(
            message_pk="m1",
            body_text=body,
            target_tokens=target,
            max_tokens=max_tokens,
            overlap_tokens=overlap,
        ):
            digest.update(f"{name}/{overlap}/{c.chunk_id}/{c.char_start}/{c.char_end}\n".encode())
    return digest.hexdigest()


class TestSplitterWork:
    """The sentence and word splitters' work is linear in the span (#673)."""

    def test_split_shapes_are_unchanged(self):
        assert _split_pin_digest() == SPLIT_PIN_DIGEST

    @pytest.mark.parametrize("skew", ["none", "guess_early", "guess_late"])
    @pytest.mark.parametrize("name", sorted(SPLIT_SHAPES))
    def test_first_overflow_matches_the_scan_it_replaces(self, monkeypatch, name, skew):
        # The window only gives a guess; the exact counts settle the cut.
        # Skewing the window's token offsets moves the guess early (every
        # token counted twice) or late (every other token dropped), and
        # the answer is still the first end the old per-end scan found.
        import re
        from types import SimpleNamespace

        import src.chunker as chunker

        real = chunker._load_tokenizer()

        class SkewedTokenizer:
            def encode(self, text, **kwargs):
                enc = real.encode(text, **kwargs)
                offsets = list(enc.offsets)
                if skew == "guess_early":
                    offsets = [o for o in offsets for _ in range(2)]
                elif skew == "guess_late":
                    offsets = offsets[::2]
                return SimpleNamespace(ids=enc.ids, offsets=offsets)

        text = SPLIT_SHAPES[name][0][:3000]
        ends = [m.end() for m in re.finditer(r"\S+", text)]
        starts = [m.start() for m in re.finditer(r"\S+", text)]
        max_tokens = 20

        def scan(start: int, lo: int) -> int:
            for i in range(lo, len(ends)):
                if estimate_tokens(text[start : ends[i]]) > max_tokens:
                    return i
            return len(ends)

        monkeypatch.setattr(chunker, "_load_tokenizer", SkewedTokenizer)
        for k in range(0, len(ends), max(1, len(ends) // 15)):
            expected = scan(starts[k], k + 1)
            assert chunker._first_overflow(text, starts[k], ends, k + 1, max_tokens) == expected

    @pytest.mark.parametrize(
        "body",
        [
            # #673: quote, signature and forward markers with no body text,
            # chunked as kind runs. Each run is one paragraph of short words.
            _MARKER_UNIT * 4000,
            # The same class in body text: one paragraph of short words,
            # and one of short sentences.
            " ".join(f"t{i % 1000}" for i in range(60_000)),
            " ".join(f"S{i}." for i in range(60_000)),
        ],
        ids=["marker_dense_no_body", "short_words", "short_sentences"],
    )
    def test_oversized_paragraph_is_tokenized_a_bounded_number_of_times(self, monkeypatch, body):
        # Splitting a paragraph over ``max_tokens`` used to re-tokenize the
        # prefix from the chunk start to every sentence end and then every
        # word end, about 400 times the paragraph at the production
        # budgets (14 minutes for 12 MB). Count the characters handed to
        # the tokenizer: a small constant times the body.
        import time

        import src.chunker as chunker
        from src.quoting import segment_for_embedding

        real = chunker._load_tokenizer()
        encoded = 0

        class CountingTokenizer:
            def encode(self, text, **kwargs):
                nonlocal encoded
                encoded += len(text)
                return real.encode(text, **kwargs)

        monkeypatch.setattr(chunker, "_load_tokenizer", CountingTokenizer)
        chunker._cached_estimate_tokens.cache_clear()
        segments = [(s.kind, s.text) for s in segment_for_embedding(body)]
        started = time.monotonic()
        chunks = chunk_segments(
            message_pk="m1",
            segments=segments,
            target_tokens=1000,
            max_tokens=1500,
            overlap_tokens=150,
        )
        assert time.monotonic() - started < 20.0
        chunker._cached_estimate_tokens.cache_clear()
        assert encoded <= 12 * len(body)
        assert all(estimate_tokens(c.text) <= 1500 for c in chunks)
        # Every word of the body is in a chunk of its segment's kind.
        words = sorted(w for c in chunks for w in c.text.split())
        assert set(words) == set(body.split())
        assert len({c.kind for c in chunks}) == len({k for k, _ in segments})
