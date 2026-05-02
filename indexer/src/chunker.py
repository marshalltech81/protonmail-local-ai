"""
Chunker for per-message retrieval units.

The thread-level embedding path truncates each message's contribution to 2000
chars and packs multiple messages into one vector. That is fine for coarse
thread discovery but loses the ability to retrieve the exact passage that
answers a question when the relevant text falls outside that window or is
diluted by unrelated replies in the same thread.

This module splits a single message body into paragraph-packed chunks that
can be stored, FTS-indexed, and embedded individually. Output is a pure
function of the input: same body, same ``message_pk`` → byte-identical
``MessageChunk`` list across runs. That determinism is what makes an
idempotent "replace chunks for this message" write cheap — the caller
can diff on ``chunk_id`` and avoid needless embed work.

Input contract:

- ``body_text`` is expected to already be the text the caller wants
  indexed. Quote stripping, signature trimming, HTML-to-text conversion,
  and any other cleanup live upstream. The chunker does not second-guess
  the body it is handed.
- ``char_start`` / ``char_end`` are offsets into the *normalized* body the
  chunker produced (CRLF → LF, runs of 3+ blank lines collapsed to 2).
  Offsets are stable across runs for the same input but are not offsets
  into the raw ``.eml`` source — map back through the same normalization
  if that is needed.

"""

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from tokenizers import Tokenizer

# Path to the bundled HuggingFace tokenizer.json for
# ``nomic-ai/nomic-embed-text-v1.5`` — the model behind the project's
# default ``OLLAMA_EMBED_MODEL``. Used by ``estimate_tokens`` so chunk
# size budgets reflect real BPE token counts instead of a 4-chars-per-
# token heuristic that under-counted CJK / URL / Base64 / code text by
# 4-6× and produced chunks past the model's 2048-token context window.
_TOKENIZER_PATH = Path(__file__).parent / "data" / "nomic-embed-text" / "tokenizer.json"

# Paragraph: one or more non-blank lines separated from the next paragraph
# by one or more entirely-blank lines. A "blank" line is empty or
# whitespace-only.
_PARAGRAPH_RE = re.compile(r"[^\n]+(?:\n(?![ \t]*\n)[^\n]*)*")

# Sentence-boundary-ish split used only when a single paragraph exceeds
# ``max_tokens``. Conservative: matches runs ending in ``.``/``!``/``?``
# followed by whitespace or end-of-string. This is a fallback, not a
# general-purpose sentence splitter.
_SENTENCE_END_RE = re.compile(r"[.!?]+(?=\s|$)")


def mean_vector(vectors: list[list[float]]) -> list[float]:
    """Element-wise mean of equal-length float vectors.

    Lives here rather than in ``main.py`` so the reconciler's reap path
    can reuse it to compute a survivor-only thread vector after a partial
    reap. Pure Python so the indexer stays free of numpy at runtime —
    embedding dim is small (768) and per-thread fan-out is bounded.

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


@dataclass(frozen=True)
class MessageChunk:
    """One retrieval unit produced from a single message body."""

    chunk_id: str
    chunk_index: int
    text: str
    char_start: int
    char_end: int
    token_est: int


@dataclass(frozen=True)
class _Span:
    """A contiguous slice of the normalized body with known offsets."""

    text: str
    start: int
    end: int


@lru_cache(maxsize=1)
def _load_tokenizer() -> Tokenizer:
    """Load the bundled nomic-embed-text tokenizer once per process.

    Cached because ``Tokenizer.from_file`` parses ~700 KB of JSON and
    builds the BPE merge tables; doing that per ``estimate_tokens`` call
    would dominate the chunker's runtime. The lazy load also keeps unit
    tests that never call ``estimate_tokens`` (``mean_vector`` / dataclass
    construction tests) free from any I/O.
    """
    return Tokenizer.from_file(str(_TOKENIZER_PATH))


def estimate_tokens(text: str) -> int:
    """Return the real BPE token count for ``text``.

    Uses the bundled nomic-embed-text tokenizer so chunk-size budgets
    line up with the embed model's 2048-token context window. The
    previous char-count heuristic under-counted CJK / URL / Base64 /
    dense code text by 4-6×, which produced chunks the embed model
    rejected with HTTP 500 ("input length exceeds the context length").

    Special tokens (``[CLS]`` / ``[SEP]``) are not added — Ollama's
    embedding endpoint adds those on the server side, so counting them
    here would double-count by two tokens per chunk.
    """
    if not text:
        return 0
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
    target_tokens: int = 350,
    max_tokens: int = 500,
    overlap_tokens: int = 60,
) -> list[MessageChunk]:
    """Split a message body into ordered ``MessageChunk`` entries.

    ``target_tokens`` is the preferred chunk size; a chunk closes once it
    reaches this budget. ``max_tokens`` is the hard ceiling that triggers
    sentence- and word-level splitting of oversized paragraphs.
    ``overlap_tokens`` is the approximate size of the tail carried from
    the previous chunk into the next — overlap is always carried as whole
    paragraph-spans, never mid-sentence.
    """
    if not (0 < target_tokens <= max_tokens):
        raise ValueError("target_tokens must be > 0 and <= max_tokens")
    if overlap_tokens < 0 or overlap_tokens >= target_tokens:
        raise ValueError("overlap_tokens must be >= 0 and < target_tokens")

    normalized = normalize_body(body_text)
    if not normalized:
        return []

    spans = _paragraph_spans(normalized)
    # Split oversized paragraphs up front so the packer only ever sees
    # spans it can fit under ``max_tokens``. Keeps packing logic simple.
    spans = _enforce_max_tokens(spans, normalized, max_tokens)

    packed = _pack_spans(
        spans,
        target_tokens=target_tokens,
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
    )

    chunks: list[MessageChunk] = []
    for index, group in enumerate(packed):
        text, char_start, char_end = _render_group(normalized, group)
        if not text:
            continue
        chunks.append(
            MessageChunk(
                chunk_id=_chunk_id(message_pk, index, text),
                chunk_index=index,
                text=text,
                char_start=char_start,
                char_end=char_end,
                token_est=estimate_tokens(text),
            )
        )
    return chunks


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
        if estimate_tokens(span.text) <= max_tokens:
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

    sub_spans: list[_Span] = []
    segment_start = cut_points[0]
    for i in range(1, len(cut_points)):
        candidate = text[segment_start : cut_points[i]]
        if estimate_tokens(candidate) > max_tokens and cut_points[i - 1] > segment_start:
            sub = _make_subspan(span, source, segment_start, cut_points[i - 1])
            if sub is not None:
                sub_spans.append(sub)
            segment_start = cut_points[i - 1]

    tail = _make_subspan(span, source, segment_start, len(text))
    if tail is not None:
        sub_spans.append(tail)

    # A single sentence may still be too large. Recurse into words for
    # those only. Everything else is already safely under the ceiling.
    final: list[_Span] = []
    for sub in sub_spans:
        if estimate_tokens(sub.text) <= max_tokens:
            final.append(sub)
        else:
            final.extend(_split_by_word(sub, source, max_tokens))
    return final if final else [span]


def _split_by_word(span: _Span, source: str, max_tokens: int) -> list[_Span]:
    """Last-resort splitter for single sentences larger than ``max_tokens``.

    Budgets by the rendered slice length, not by summing per-word token
    estimates — the rendered chunk includes the whitespace between words,
    so a per-word sum would underestimate the chunk's true token count
    and push the packer back over ``max_tokens``.
    """
    text = span.text
    words = list(re.finditer(r"\S+", text))
    if not words:
        return [span]

    sub_spans: list[_Span] = []
    segment_start = words[0].start()
    for i, word in enumerate(words):
        candidate = text[segment_start : word.end()]
        # Only close once the current segment already contains at least
        # one earlier word — otherwise a single runaway word would land
        # in an empty segment and never be emitted.
        has_earlier_word = i > 0 and words[i - 1].start() >= segment_start
        if estimate_tokens(candidate) > max_tokens and has_earlier_word:
            prev_end = words[i - 1].end()
            sub = _make_subspan(span, source, segment_start, prev_end)
            if sub is not None:
                sub_spans.append(sub)
            segment_start = word.start()

    tail = _make_subspan(span, source, segment_start, len(text))
    if tail is not None:
        sub_spans.append(tail)
    return sub_spans if sub_spans else [span]


def _make_subspan(parent: _Span, source: str, local_start: int, local_end: int) -> _Span | None:
    """Build a child span from ``parent`` using local offsets.

    Whitespace is trimmed from the rendered text but the stored offsets
    keep pointing at real content — leading/trailing whitespace is
    stripped by advancing / retreating the offsets, not by mutating them
    blindly. Returns ``None`` if the resulting slice is empty.
    """
    if local_end <= local_start:
        return None
    slice_text = parent.text[local_start:local_end]
    lead = len(slice_text) - len(slice_text.lstrip())
    trail = len(slice_text) - len(slice_text.rstrip())
    trimmed = slice_text.strip()
    if not trimmed:
        return None
    start = parent.start + local_start + lead
    end = parent.start + local_end - trail
    # Defensive check: offsets must round-trip through ``source`` even when
    # Python runs with optimization flags that remove assert statements.
    if source[start:end] != trimmed:
        raise ValueError("subspan offsets drifted")
    return _Span(text=trimmed, start=start, end=end)


def _pack_spans(
    spans: list[_Span],
    *,
    target_tokens: int,
    max_tokens: int,
    overlap_tokens: int,
) -> list[list[_Span]]:
    """Greedy pack spans into chunks, closing at ``target_tokens``.

    When a chunk closes, overlap spans are carried forward from its tail
    so the next chunk starts with context rather than a hard cut.
    """
    groups: list[list[_Span]] = []
    current: list[_Span] = []
    current_tokens = 0

    def close() -> list[_Span]:
        """Close ``current``, seed the next group with its overlap tail."""
        nonlocal current, current_tokens
        if not current:
            return []
        groups.append(current)
        overlap = _overlap_tail(current, overlap_tokens)
        current = list(overlap)
        current_tokens = sum(estimate_tokens(s.text) for s in current)
        return overlap

    for span in spans:
        span_tokens = estimate_tokens(span.text)
        if current and current_tokens + span_tokens > max_tokens:
            close()
        current.append(span)
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


def _render_group(source: str, group: list[_Span]) -> tuple[str, int, int]:
    """Render a chunk's text and the matching trimmed offsets.

    Slicing the source (rather than rejoining span text) preserves the
    exact whitespace between spans. Trailing/leading whitespace at the
    edges of the slice is trimmed atomically so the offsets stay
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
    lead = len(raw) - len(raw.lstrip())
    trail = len(raw) - len(raw.rstrip())
    return raw.strip(), raw_start + lead, raw_end - trail


def _chunk_id(message_pk: str, index: int, text: str) -> str:
    """Deterministic chunk id: stable under re-runs with identical input."""
    digest = hashlib.sha256()
    digest.update(message_pk.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(str(index).encode("ascii"))
    digest.update(b"\x00")
    digest.update(text.encode("utf-8"))
    return digest.hexdigest()
