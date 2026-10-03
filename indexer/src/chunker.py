"""
Chunker for per-message retrieval units.

This module splits a single message body into paragraph-packed chunks that
can be stored, FTS-indexed, and embedded individually, so retrieval can
reach the exact passage that answers a question. The chunk vectors also
feed coarse thread discovery: a thread's vector is the mean of its chunk
vectors (``mean_vector``), with a subject-only embedding as the fallback
for a thread that has no chunks. Output is a pure
function of the input: same body, same ``message_pk`` → byte-identical
``MessageChunk`` list across runs. That determinism is what makes an
idempotent "replace chunks for this message" write cheap — the caller
can diff on ``chunk_id`` and avoid needless embed work.

Input contract:

- ``body_text`` is expected to already be the text the caller wants
  indexed. Quote stripping, signature trimming, HTML-to-text conversion,
  and any other cleanup live upstream. The chunker does not second-guess
  the body it is handed.
- Segmentation by kind (``quoting.segment_for_embedding``) also lives
  upstream: ``chunk_segments`` takes the ``(kind, text)`` segments and
  chunks each on its own, so a chunk never spans kinds (#646).
- ``char_start`` / ``char_end`` are offsets into the *normalized* body the
  chunker produced (CRLF → LF, runs of 3+ blank lines collapsed to 2;
  for several segments, the normalized segments joined by a blank
  line). Offsets are stable across runs for the same input but are not
  offsets into the raw ``.eml`` source — map back through the same
  normalization if that is needed.

"""

import bisect
import hashlib
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal, get_args

from tokenizers import Tokenizer

# Path to the bundled HuggingFace tokenizer.json for
# ``Qwen/Qwen3-Embedding-8B`` — used by ``estimate_tokens`` so chunk
# size budgets reflect real BPE token counts instead of a
# 4-chars-per-token heuristic that under-counted CJK / URL / Base64 /
# code text by 4-6× and produced chunks past the embed model's
# practical context window. Tokenizer is matched to Qwen3-Embedding-8B
# (the schema-default embedding model); the file is vendored from the
# model's HuggingFace repo so the indexer never performs a runtime
# download.
_TOKENIZER_PATH = Path(__file__).parent / "data" / "qwen3-embedding" / "tokenizer.json"

# Paragraph: one or more non-blank lines separated from the next paragraph
# by one or more entirely-blank lines. A "blank" line is empty or
# whitespace-only.
_PARAGRAPH_RE = re.compile(r"[^\n]+(?:\n(?![ \t]*\n)[^\n]*)*")

# Sentence-boundary-ish split used only when a single paragraph exceeds
# ``max_tokens``. Conservative: matches runs ending in ``.``/``!``/``?``
# followed by whitespace or end-of-string. This is a fallback, not a
# general-purpose sentence splitter. The lookbehind anchors each match at
# the start of a punctuation run, so a run not followed by whitespace is
# scanned once rather than retried from every position inside it
# (quadratic on a long run such as ``"." * 128000 + "x"``).
_SENTENCE_END_RE = re.compile(r"(?<![.!?])[.!?]+(?=\s|$)")

# Characters per token of the ceiling in the first window
# ``_first_overflow`` tokenizes to find a split's cut: above the ~3.5
# chars per token of English text, so one window usually holds the cut.
# A window short of ``max_tokens`` tokens is doubled until it is not.
_CUT_WINDOW_CHARS_PER_TOKEN = 4


# Tolerance for "vector is already unit-norm". A model that already
# emits L2-normalized output (Qwen3-Embedding-8B does, per its model
# card) lands within float32's relative precision of 1.0; the cheap
# sqrt + compare lets us skip the division entirely when no
# correction is needed. Float32 round-trip noise is well under 1e-6.
_UNIT_NORM_TOLERANCE = 1e-6


def l2_normalize(vec: list[float]) -> list[float]:
    """Return ``vec`` scaled to unit L2 norm.

    Idempotent: a vector that's already within
    ``_UNIT_NORM_TOLERANCE`` of unit-norm is returned unchanged
    (no division, no float churn). Zero vectors are also returned
    unchanged — dividing by zero would NaN-poison the storage. The
    indexer's seed-vector logic intentionally writes a zero
    placeholder for genuinely-new threads (Phase 1 seed before
    Phase 2 lands the real chunk-mean vector); preserving it through
    this normalization keeps the three-case priority chain working.

    Cost is one O(dim) sum-of-squares plus a sqrt — negligible
    against the embed HTTP round-trip and inputs are already in
    Python list form post-deserialization.
    """
    norm_sq = 0.0
    for x in vec:
        norm_sq += x * x
    if not math.isfinite(norm_sq):
        # NaN or inf in any component. sqlite-vec would store it and the
        # row would then break semantic search (#232).
        raise ValueError("vector has non-finite components")
    if norm_sq <= 0.0:
        return vec
    norm = math.sqrt(norm_sq)
    if abs(norm - 1.0) < _UNIT_NORM_TOLERANCE:
        return vec
    return [x / norm for x in vec]


def mean_vector(vectors: list[list[float]]) -> list[float]:
    """Element-wise mean of equal-length float vectors.

    Lives here rather than in ``main.py`` so the reconciler's reap path
    can reuse it to compute a survivor-only thread vector after a partial
    reap. Pure Python so the indexer stays free of numpy at runtime —
    per-thread fan-out is bounded (typically <100 chunks per thread) and
    even at 4096-dim Qwen3-Embedding the per-thread aggregate is sub-ms.

    The result is *not* L2-normalized — averaging unit vectors yields a
    vector with norm < 1 in the general case. Callers that need to
    enforce the ``threads_vec`` / ``message_chunks_vec`` unit-norm
    invariant should pass through ``l2_normalize`` (the DB write
    boundary in ``database.py`` does this automatically).

    Raises ``ValueError`` on empty input or mismatched dimensions; the
    caller chooses the fallback (typically embedding the subject line).
    """
    if not vectors:
        raise ValueError("cannot mean an empty vector list")
    dim = len(vectors[0])
    if any(len(v) != dim for v in vectors):
        raise ValueError("all vectors must have the same dimension")
    sums = [0.0] * dim
    for vec in vectors:
        for i, value in enumerate(vec):
            sums[i] += value
    n = float(len(vectors))
    return [s / n for s in sums]


# What a chunk's text is (#646). A closed set, stored in
# ``message_chunks.kind`` under a ``CHECK`` that lists the same values:
#
# - ``body``: the message's own text;
# - ``quote``: quoted history (``>`` lines, reply headers, an Outlook
#   reply block);
# - ``signature``: text from the RFC 3676 ``-- `` delimiter on;
# - ``forwarded``: text from a forward preamble on;
# - ``calendar``: reserved for calendar content. Nothing produces it
#   yet: a ``text/calendar`` part is not body text inside a multipart
#   and has no attachment extractor;
# - ``attachment``: text extracted from an attachment.
#
# ``quoting.segment_for_embedding`` assigns the message-text kinds.
ChunkKind = Literal["body", "quote", "signature", "forwarded", "calendar", "attachment"]
CHUNK_KINDS: tuple[ChunkKind, ...] = get_args(ChunkKind)


@dataclass(frozen=True)
class MessageChunk:
    """One retrieval unit produced from a single message body."""

    chunk_id: str
    chunk_index: int
    text: str
    char_start: int
    char_end: int
    token_est: int
    kind: ChunkKind = "body"


@dataclass(frozen=True)
class _Span:
    """A contiguous slice of the normalized body with known offsets."""

    text: str
    start: int
    end: int


@lru_cache(maxsize=1)
def _load_tokenizer() -> Tokenizer:
    """Load the bundled Qwen3-Embedding tokenizer once per process.

    Cached because ``Tokenizer.from_file`` parses ~11 MB of JSON and
    builds the BPE merge tables; doing that per ``estimate_tokens``
    call would dominate the chunker's runtime. The lazy load also
    keeps unit tests that never call ``estimate_tokens`` (``mean_vector``
    / dataclass construction tests) free from any I/O.
    """
    return Tokenizer.from_file(str(_TOKENIZER_PATH))


# Upper bound on the byte-size of strings we cache token counts for.
# The packer's redundancy is in repeated lookups of the same paragraph-
# sized spans (a paragraph carried as overlap is re-encoded for every
# chunk it appears in). A pasted log file or a 200 KB attachment text
# is encoded once and never benefits from the cache, but caching it
# would let an attacker-controlled email pin megabytes of strings via
# the lru_cache. 8192 bytes covers any realistic paragraph and bounds
# worst-case cache memory at roughly maxsize × threshold.
#
# The gate must be byte-size, not character count: a 4096-char emoji
# string is ~16 KB UTF-8 (each emoji is 4 bytes), and a 4096-char CJK
# string is ~12 KB. Gating on ``len(text)`` would let multilingual
# content bypass the documented memory bound — exactly the
# attacker-controlled-input case the threshold exists to defend.
_TOKEN_ESTIMATE_CACHE_THRESHOLD_BYTES = 8192


@lru_cache(maxsize=1024)
def _cached_estimate_tokens(text: str) -> int:
    """Cached path for ``estimate_tokens`` — only invoked for short text.

    The size-gating wrapper above ensures we never insert a large
    string into the cache, so the entry-count-based ``maxsize`` is
    also a memory bound. ``maxsize=1024`` is well above the typical
    chunker working set per message (a few dozen unique spans) and
    keeps total cache memory below ~8 MB worst-case.
    """
    return len(_load_tokenizer().encode(text, add_special_tokens=False).ids)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Return ``text`` cut to at most ``max_tokens`` BPE tokens.

    Uses the bundled Qwen3-Embedding tokenizer's per-token character
    offsets to slice on a real token boundary (not an arbitrary char
    index that might split a multi-byte codepoint or a token mid-way).
    Returns the input unchanged when it already fits.

    Used for thread-level body caps (``THREAD_BODY_TEXT_MAX_TOKENS``)
    so the cap aligns with the embed model's actual context budget.
    A char-based cap under-counted CJK / URL / Base64 / dense code
    text by 4-6× and forced unnecessarily aggressive truncation in
    ASCII-heavy threads.
    """
    if not text or max_tokens <= 0:
        return ""
    tokenizer = _load_tokenizer()
    encoding = tokenizer.encode(text, add_special_tokens=False)
    if len(encoding.ids) <= max_tokens:
        return text
    # ``offsets`` is parallel to ``ids``; ``offsets[max_tokens][0]`` is
    # the character index where the (max_tokens+1)-th token starts. The
    # tokenizer's character offsets respect codepoint boundaries, so
    # this slice is always safe.
    cut = encoding.offsets[max_tokens][0]
    return text[:cut]


def estimate_tokens(text: str) -> int:
    """Return the real BPE token count for ``text``.

    Uses the bundled Qwen3-Embedding-8B tokenizer so chunk-size budgets
    line up with the embed model's practical context window. The
    previous char-count heuristic under-counted CJK / URL / Base64 /
    dense code text by 4-6×, producing chunks that exceeded the embed
    model's context.

    Special tokens are not added — the embed service adds those on the
    server side, so counting them here would double-count.

    Caching is gated by UTF-8 byte size: short inputs (paragraph-sized,
    under ``_TOKEN_ESTIMATE_CACHE_THRESHOLD_BYTES``) go through the
    bounded ``_cached_estimate_tokens`` LRU because the packer evaluates
    the same paragraph repeatedly while greedy-packing and computing
    overlap tails. Larger inputs (a pasted log file, a long attachment
    text) bypass the cache: re-encoding once is cheap and caching them
    would let an attacker-controlled email pin megabytes of strings in
    the LRU. Byte-size — not ``len(text)`` — is the right gate because
    a 4096-char CJK or emoji string is multiples of that in UTF-8 bytes,
    and the threshold is a memory bound.
    """
    if not text:
        return 0
    if len(text.encode("utf-8")) <= _TOKEN_ESTIMATE_CACHE_THRESHOLD_BYTES:
        return _cached_estimate_tokens(text)
    return len(_load_tokenizer().encode(text, add_special_tokens=False).ids)


def normalize_body(body_text: str) -> str:
    """Normalize line endings and collapse excess blank lines.

    The chunker operates on this normalized form and its ``char_start`` /
    ``char_end`` offsets are into it, not into the raw input. Exposed so
    callers can round-trip offsets back to source text when they need to.
    """
    if not body_text:
        return ""
    text = body_text.replace("\r\n", "\n").replace("\r", "\n")
    # Runs of 3+ blank lines are almost always formatting noise
    # (signature padding, Outlook-style spacing). Collapsing them keeps
    # paragraph detection simple and offsets stable.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n")


def chunk_message(
    *,
    message_pk: str,
    body_text: str,
    kind: ChunkKind = "body",
    target_tokens: int = 350,
    max_tokens: int = 500,
    overlap_tokens: int = 60,
) -> list[MessageChunk]:
    """Split a message body into ordered ``MessageChunk`` entries.

    ``target_tokens`` is the preferred chunk size; a chunk closes once it
    reaches this budget. ``max_tokens`` is the hard ceiling: every
    returned chunk's ``text`` is at most ``max_tokens`` tokens by the
    bundled tokenizer, separators and whitespace between its spans
    included (#208, #550). Oversized paragraphs are split at sentence,
    word and then token boundaries to meet it.
    ``overlap_tokens`` is the approximate size of the tail carried from
    the previous chunk into the next — overlap is always carried as whole
    paragraph-spans, never mid-sentence. Every chunk gets ``kind``.
    """
    return chunk_segments(
        message_pk=message_pk,
        segments=[(kind, body_text)],
        target_tokens=target_tokens,
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
    )


def chunk_segments(
    *,
    message_pk: str,
    segments: Iterable[tuple[ChunkKind, str]],
    target_tokens: int = 350,
    max_tokens: int = 500,
    overlap_tokens: int = 60,
) -> list[MessageChunk]:
    """Chunk a body made of ``(kind, text)`` segments, one segment at a time.

    Segmentation happens before chunking, so a chunk never spans kinds:
    each segment is normalized and chunked on its own, with no overlap
    carried across a segment boundary. ``chunk_index`` runs on across
    segments. Offsets are into the normalized segments joined by a
    blank line (``"\n\n"``); for one segment that is just its
    normalized text, so ``chunk_message`` output is unchanged. Budgets
    are as in ``chunk_message``. Raises ``ValueError`` on a kind
    outside ``CHUNK_KINDS``.
    """
    if not (0 < target_tokens <= max_tokens):
        raise ValueError("target_tokens must be > 0 and <= max_tokens")
    if overlap_tokens < 0 or overlap_tokens >= target_tokens:
        raise ValueError("overlap_tokens must be >= 0 and < target_tokens")

    chunks: list[MessageChunk] = []
    # Index of the next packed group. A group that renders empty is
    # skipped with its index, as before segments existed.
    index = 0
    # Offset of the current segment in the joined normalized text.
    offset = 0
    for kind, text in segments:
        if kind not in CHUNK_KINDS:
            raise ValueError("unknown chunk kind")
        normalized = normalize_body(text)
        if not normalized:
            continue
        for group in _pack_segment(normalized, target_tokens, max_tokens, overlap_tokens):
            rendered, char_start, char_end = _render_group(normalized, group)
            if rendered:
                chunks.append(
                    MessageChunk(
                        chunk_id=_chunk_id(message_pk, index, rendered),
                        chunk_index=index,
                        text=rendered,
                        char_start=offset + char_start,
                        char_end=offset + char_end,
                        token_est=estimate_tokens(rendered),
                        kind=kind,
                    )
                )
            index += 1
        offset += len(normalized) + len("\n\n")
    return chunks


def _pack_segment(
    normalized: str, target_tokens: int, max_tokens: int, overlap_tokens: int
) -> list[list[_Span]]:
    """Return the span groups, one per chunk, of one normalized segment."""
    spans = _paragraph_spans(normalized)
    # Split oversized paragraphs up front so the packer only ever sees
    # spans it can fit under ``max_tokens``. Keeps packing logic simple.
    spans = _enforce_max_tokens(spans, normalized, max_tokens)

    packed = _pack_spans(
        spans,
        normalized,
        target_tokens=target_tokens,
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
    )

    # The packer budgets each span and gap on its own; check the text
    # each chunk actually renders to (#550). A cut can leave a run made
    # only of the overlap seed, already in the chunk before it: a run
    # that ends no further than its predecessor adds nothing and is
    # dropped, so it gets no index of its own.
    runs: list[list[_Span]] = []
    for group in packed:
        for run in _fit_rendered(normalized, group, max_tokens):
            if not runs or run[-1].end > runs[-1][-1].end:
                runs.append(run)
    return runs


def _paragraph_spans(text: str) -> list[_Span]:
    """Return paragraph spans with offsets into ``text``."""
    return [
        _Span(text=m.group(), start=m.start(), end=m.end()) for m in _PARAGRAPH_RE.finditer(text)
    ]


def _enforce_max_tokens(spans: list[_Span], source: str, max_tokens: int) -> list[_Span]:
    """Replace any span over ``max_tokens`` with a sequence of sub-spans.

    Tries sentence boundaries first; falls back to word boundaries when a
    single sentence is itself too large (e.g. a pasted log line).
    """
    result: list[_Span] = []
    for span in spans:
        # Measure the text the span renders to alone: trimming its edges
        # can raise the count (`` differ`` is one token, ``differ`` two).
        if estimate_tokens(_render_group(source, [span])[0]) <= max_tokens:
            result.append(span)
            continue
        result.extend(_split_by_sentence(span, source, max_tokens))
    return result


def _split_by_sentence(span: _Span, source: str, max_tokens: int) -> list[_Span]:
    """Split ``span`` at sentence boundaries, recursing to words if needed."""
    # Collect end-of-sentence offsets local to the span's text, then pack
    # sentences greedily under the size budget.
    text = span.text
    cut_points = [0]
    for m in _SENTENCE_END_RE.finditer(text):
        cut_points.append(m.end())
    if cut_points[-1] != len(text):
        cut_points.append(len(text))

    # Each segment runs from ``cut_points[k]`` to the sentence end before
    # the first one its text overflows at, or holds its first sentence
    # alone when that overflows on its own. A last sentence is the tail
    # whatever it counts, so it is not counted here.
    sub_spans: list[_Span] = []
    k = 0
    last = len(cut_points) - 1
    while k + 1 < last:
        i = _first_overflow(text, cut_points[k], cut_points, k + 1, max_tokens)
        cut = max(i - 1, k + 1)
        if cut >= last:
            break
        sub = _make_subspan(span, source, cut_points[k], cut_points[cut])
        if sub is not None:
            sub_spans.append(sub)
        k = cut

    tail = _make_subspan(span, source, cut_points[k], len(text))
    if tail is not None:
        sub_spans.append(tail)

    # A single sentence may still be too large. Recurse into words for
    # those only. Everything else is already safely under the ceiling.
    # If word-splitting also leaves a span over ``max_tokens`` (CJK
    # text without spaces, a long URL, a Base64 wall — all single
    # "words" by ``\\S+``), fall back to ``_split_by_tokens`` which
    # uses the embed model's tokenizer to slice at exact token
    # boundaries. Without that final layer, runaway non-whitespace
    # spans pass through and trigger ``input length exceeds the
    # context length`` from the embedding service at embed time.
    final: list[_Span] = []
    for sub in sub_spans:
        if estimate_tokens(sub.text) <= max_tokens:
            final.append(sub)
        else:
            for word_split in _split_by_word(sub, source, max_tokens):
                if estimate_tokens(word_split.text) <= max_tokens:
                    final.append(word_split)
                else:
                    final.extend(_split_by_tokens(word_split, source, max_tokens))
    return final if final else [span]


def _split_by_word(span: _Span, source: str, max_tokens: int) -> list[_Span]:
    """Last-resort splitter for single sentences larger than ``max_tokens``.

    Budgets by the rendered slice length, not by summing per-word token
    estimates — the rendered chunk includes the whitespace between words,
    so a per-word sum would underestimate the chunk's true token count
    and push the packer back over ``max_tokens``.
    """
    text = span.text
    word_ends = [m.end() for m in re.finditer(r"\S+", text)]
    if not word_ends:
        return [span]

    sub_spans: list[_Span] = []
    # Each segment starts at its first word (``word_ends[k]``), or at the
    # tabs that open that word's line (#433), so the budget counts the
    # kept tabs too. It closes at the word before the first one it
    # overflows at, so it always holds at least one word: a single
    # runaway word is emitted on its own rather than never.
    segment_start = _leading_trim(source, span.start, span.end)
    k = 0
    while True:
        i = _first_overflow(text, segment_start, word_ends, k + 1, max_tokens)
        if i >= len(word_ends):
            break
        prev_end = word_ends[i - 1]
        sub = _make_subspan(span, source, segment_start, prev_end)
        if sub is not None:
            sub_spans.append(sub)
        segment_start = prev_end + _leading_trim(source, span.start + prev_end, span.end)
        k = i

    tail = _make_subspan(span, source, segment_start, len(text))
    if tail is not None:
        sub_spans.append(tail)
    return sub_spans if sub_spans else [span]


def _split_by_tokens(span: _Span, source: str, max_tokens: int) -> list[_Span]:
    """Last-resort splitter for spans without exploitable whitespace.

    Used when ``_split_by_word`` cannot reduce a span — typical inputs
    are CJK text (no spaces), a single very long URL, or a Base64 wall
    pasted from an attachment. The splitter encodes the span's text
    with the embed-model tokenizer, takes the per-token ``offsets``
    metadata that the HuggingFace ``Encoding`` exposes, and slices
    the source at those exact token boundaries. The result is
    guaranteed to fit under ``max_tokens`` (with a small safety
    margin for whitespace re-insertion at slice boundaries) without
    losing the offset round-trip invariant — each emitted sub-span's
    ``[start, end)`` still maps back to the parent.

    The safety margin matters because the tokenizer treats whitespace
    differently mid-word vs at boundaries; ``max_tokens - 8`` keeps
    re-encoding the decoded slice safely under the ceiling on every
    BPE the bundled tokenizer ships with.
    """
    encoding = _load_tokenizer().encode(span.text, add_special_tokens=False)
    n_tokens = len(encoding.ids)
    if n_tokens <= max_tokens:
        return [span]

    # ``offsets`` is parallel to ``ids``: for each token, ``(start,
    # end)`` are character offsets into the encoded text. Use the
    # first token's start and the last token's end of each slice.
    offsets = encoding.offsets
    chunk_size = max(1, max_tokens - 8)
    sub_spans: list[_Span] = []
    for i in range(0, n_tokens, chunk_size):
        last = min(i + chunk_size, n_tokens) - 1
        local_start = offsets[i][0]
        local_end = offsets[last][1]
        if local_end <= local_start:
            continue
        sub = _make_subspan(span, source, local_start, local_end)
        if sub is not None:
            sub_spans.append(sub)
    return sub_spans if sub_spans else [span]


def _first_overflow(text: str, start: int, ends: list[int], lo: int, max_tokens: int) -> int:
    """Return the first ``i >= lo`` whose ``text[start:ends[i]]`` is over ``max_tokens``.

    Returns ``len(ends)`` when none is; ``ends`` are increasing offsets
    into ``text``. The splitters used to count the prefix up to every
    sentence or word end in turn, re-tokenizing about the ceiling's worth
    of text per sentence or word: some 400 times the paragraph at the
    production budgets (#673). Instead one window from ``start``, a few
    times the ceiling in size, is tokenized, and its token offsets give a
    guess: the first end with more than ``max_tokens`` tokens before it.
    BPE counts are not additive, so a prefix counted on its own can differ
    from the window's count near the cut. Exact counts then settle it:
    a gallop from the guess brackets the answer and a binary search
    finds it, so each cut costs one window plus a few exact counts of
    about the chunk it emits. The search assumes a longer prefix takes no
    fewer tokens; where one does, it can return a later overflow than the
    per-end scan would have, but the prefix to the end before it (when
    that is ``lo`` or later) was counted exactly and fits, so the ceiling
    still holds.
    """
    n = len(ends)
    if lo >= n:
        return n
    tokenizer = _load_tokenizer()
    width = _CUT_WINDOW_CHARS_PER_TOKEN * (max_tokens + 1)
    while True:
        offsets = tokenizer.encode(text[start : start + width], add_special_tokens=False).offsets
        if len(offsets) > max_tokens or start + width >= len(text):
            break
        width *= 2
    if len(offsets) > max_tokens:
        guess = bisect.bisect_left(ends, start + offsets[max_tokens][1], lo)
    else:
        guess = n

    def overflows(i: int) -> bool:
        return estimate_tokens(text[start : ends[i]]) > max_tokens

    # Bracket the answer: ``fits`` is an index known to fit (or
    # ``lo - 1``), ``over`` one known to overflow (or ``n``).
    if guess < n and not overflows(guess):
        fits, over, step = guess, n, 1
        while fits + step < n:
            if overflows(fits + step):
                over = fits + step
                break
            fits += step
            step *= 2
    else:
        fits, over, step = lo - 1, guess, 1
        while over - step >= lo:
            if not overflows(over - step):
                fits = over - step
                break
            over -= step
            step *= 2
    while over - fits > 1:
        mid = (fits + over) // 2
        if overflows(mid):
            over = mid
        else:
            fits = mid
    return over


def _leading_trim(source: str, start: int, end: int) -> int:
    """Return how many leading whitespace chars of ``source[start:end]`` to drop.

    All leading whitespace is dropped, except a run of tabs that opens a
    line (it follows a newline, or the start of ``source``) directly
    before the first non-whitespace char. The xlsx extractor writes empty
    leading cells as empty tab-separated fields, so those tabs carry the
    column of the row's first value and a chunk edge must not shift it
    (#433). Tabs that do not open a line (a split mid-row) are dropped:
    the column of the value after them is unknown there. Nor can a chunk
    keep a run of tabs too long to fit in it (about 16 tabs per token, so
    over ~7,800 empty leading cells at the default 500-token ceiling):
    ``_split_by_tokens`` drops the tab-only slices and the value's slice
    starts mid-run, so that value still loses its column. One linear scan
    over the leading whitespace, then one back over the tabs it ends with.
    """
    first = start
    while first < end and source[first].isspace():
        first += 1
    if first == end:
        return end - start
    tabs_from = first
    while tabs_from > start and source[tabs_from - 1] == "\t":
        tabs_from -= 1
    if tabs_from < first and (tabs_from == 0 or source[tabs_from - 1] == "\n"):
        return tabs_from - start
    return first - start


def _make_subspan(parent: _Span, source: str, local_start: int, local_end: int) -> _Span | None:
    """Build a child span from ``parent`` using local offsets.

    Whitespace is trimmed from the rendered text but the stored offsets
    keep pointing at real content — leading/trailing whitespace is
    stripped by advancing / retreating the offsets, not by mutating them
    blindly. Tabs that open a line are kept (see ``_leading_trim``).
    Returns ``None`` if the resulting slice is empty.
    """
    if local_end <= local_start:
        return None
    slice_text = parent.text[local_start:local_end]
    if not slice_text.strip():
        return None
    lead = _leading_trim(source, parent.start + local_start, parent.start + local_end)
    trail = len(slice_text) - len(slice_text.rstrip())
    trimmed = slice_text[lead : len(slice_text) - trail]
    start = parent.start + local_start + lead
    end = parent.start + local_end - trail
    # Defensive check: offsets must round-trip through ``source`` even when
    # Python runs with optimization flags that remove assert statements.
    if source[start:end] != trimmed:
        raise ValueError("subspan offsets drifted")
    return _Span(text=trimmed, start=start, end=end)


def _pack_spans(
    spans: list[_Span],
    source: str,
    *,
    target_tokens: int,
    max_tokens: int,
    overlap_tokens: int,
) -> list[list[_Span]]:
    """Greedy pack spans into chunks, closing at ``target_tokens``.

    When a chunk closes, overlap spans are carried forward from its tail
    so the next chunk starts with context rather than a hard cut.

    ``target_tokens`` is checked against the spans' own tokens.
    ``max_tokens`` is checked against the spans plus the source between
    them, since the rendered chunk holds both: a paragraph separator, or
    whitespace the splitters dropped between sub-spans, which has no size
    bound (#550). Each gap is counted once, on its own; ``_fit_rendered``
    then checks the joined text, whose count can differ.
    """
    groups: list[list[_Span]] = []
    current: list[_Span] = []
    current_tokens = 0
    # Tokens of ``current``'s spans plus the gaps between them.
    current_rendered = 0
    # Tokens of the source between each span and the span before it,
    # keyed by span start (spans never share a start).
    gap_tokens: dict[int, int] = {}
    # True while ``current`` holds only the overlap seed of the last close.
    seed_only = False

    def close() -> list[_Span]:
        """Close ``current``, seed the next group with its overlap tail."""
        nonlocal current, current_tokens, current_rendered, seed_only
        if not current:
            return []
        groups.append(current)
        overlap = _overlap_tail(current, overlap_tokens)
        current = list(overlap)
        seed_only = bool(current)
        current_tokens = sum(estimate_tokens(s.text) for s in current)
        current_rendered = current_tokens + sum(gap_tokens[s.start] for s in current[1:])
        return overlap

    prev_end: int | None = None
    for span in spans:
        span_tokens = estimate_tokens(span.text)
        gap = 0 if prev_end is None else estimate_tokens(source[prev_end : span.start])
        gap_tokens[span.start] = gap
        prev_end = span.end
        # Close only a group with new material: closing a bare overlap
        # seed would emit a chunk that duplicates the previous one's tail.
        if current and not seed_only and current_rendered + gap + span_tokens > max_tokens:
            close()
        # The overlap seed ``close()`` leaves behind is not bounded by
        # the overlap budget (a carried span may be larger than it), so
        # it can still overflow next to this span (#208). Drop seed
        # spans from the front until the span fits.
        while current and current_rendered + gap + span_tokens > max_tokens:
            dropped_tokens = estimate_tokens(current.pop(0).text)
            current_tokens -= dropped_tokens
            current_rendered -= dropped_tokens + (gap_tokens[current[0].start] if current else 0)
        current_rendered += span_tokens + (gap if current else 0)
        current.append(span)
        seed_only = False
        current_tokens += span_tokens
        if current_tokens >= target_tokens:
            close()

    # Flush trailing content. If the only thing left is the overlap tail
    # from the previous close, drop it — that would emit a tail-only
    # duplicate chunk with no new material.
    if current:
        is_overlap_only = (
            groups and len(current) <= len(groups[-1]) and all(s in groups[-1] for s in current)
        )
        if not is_overlap_only:
            groups.append(current)

    return groups


def _overlap_tail(group: list[_Span], overlap_tokens: int) -> list[_Span]:
    """Return the suffix of ``group`` whose total tokens fit the overlap budget."""
    if overlap_tokens <= 0 or not group:
        return []
    tail: list[_Span] = []
    total = 0
    for span in reversed(group):
        span_tokens = estimate_tokens(span.text)
        if tail and total + span_tokens > overlap_tokens:
            break
        tail.insert(0, span)
        total += span_tokens
        if total >= overlap_tokens:
            break
    # Never carry the entire chunk forward as overlap — that produces a
    # duplicate chunk with no progress.
    if len(tail) == len(group):
        tail = tail[1:]
    return tail


def _fit_rendered(source: str, group: list[_Span], max_tokens: int) -> list[list[_Span]]:
    """Cut ``group`` into consecutive runs whose rendered text fits ``max_tokens``.

    BPE counts are not additive: a span and the gap after it can take more
    tokens joined than counted apart, so a group the packer accepted can
    still render past the ceiling. Such a group is cut at a prefix that
    fits, found by binary search over the span count: O(log n) renders per
    cut, where dropping one span at a time would cost one per span. The
    packer bounds the group, so each render is bounded too. A single span
    is never cut: ``_enforce_max_tokens`` already bounded it.
    """
    runs: list[list[_Span]] = []
    while len(group) > 1 and estimate_tokens(_render_group(source, group)[0]) > max_tokens:
        fits, overflows = 1, len(group)
        while overflows - fits > 1:
            mid = (fits + overflows) // 2
            if estimate_tokens(_render_group(source, group[:mid])[0]) <= max_tokens:
                fits = mid
            else:
                overflows = mid
        runs.append(group[:fits])
        group = group[fits:]
    runs.append(group)
    return runs


def _render_group(source: str, group: list[_Span]) -> tuple[str, int, int]:
    """Render a chunk's text and the matching trimmed offsets.

    Slicing the source (rather than rejoining span text) preserves the
    exact whitespace between spans. Trailing/leading whitespace at the
    edges of the slice (except tabs that open a line, see
    ``_leading_trim``) is trimmed atomically so the offsets stay
    honest: the contract ``source[char_start:char_end] == text`` must
    hold for every chunk so downstream tools can map a chunk back to
    its position in the normalized body.

    Returns ``("", 0, 0)`` when the group is empty.
    """
    if not group:
        return "", 0, 0
    raw_start = group[0].start
    raw_end = group[-1].end
    raw = source[raw_start:raw_end]
    if not raw.strip():
        return "", raw_start, raw_start
    lead = _leading_trim(source, raw_start, raw_end)
    trail = len(raw) - len(raw.rstrip())
    return raw[lead : len(raw) - trail], raw_start + lead, raw_end - trail


def _chunk_id(message_pk: str, index: int, text: str) -> str:
    """Deterministic chunk id: stable under re-runs with identical input."""
    digest = hashlib.sha256()
    digest.update(message_pk.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(str(index).encode("ascii"))
    digest.update(b"\x00")
    digest.update(text.encode("utf-8"))
    return digest.hexdigest()
