"""
Quoted-reply and signature stripping for embedding input.

Each reply carries the quoted history before it, so by the tenth message
a thread's chunks — and the thread vector averaged from them — are
dominated by whatever text got quoted most (often a boilerplate
contract, signature, or the original question) and drift away from the
actual content of the latest replies. Stripping quotes and signatures
before a message body is chunked and embedded keeps each chunk vector
aligned with the substantive content of its message.

The stored ``body_text`` (FTS index input) is left alone: users
legitimately search quoted text and signatures, so this transform only
applies at the embedding boundary.

``segment_for_embedding`` is the form the indexer chunks: the same line
rules, returned as runs tagged with a chunk kind (body / quote /
signature / forwarded) so that segmentation happens before chunking and
no chunk spans kinds (#646).

Heuristics are intentionally narrow. This is a domain-fraught problem
and an aggressive stripper that eats real body content is worse than a
conservative one that occasionally leaves quoted text in. Everything
here is a simple line-based rule; no ML, no language detection.
"""

import io
import re
from dataclasses import dataclass

from .chunker import ChunkKind

# Full-line markers that cut the rest of the message. Matched against the
# line body after stripping the trailing newline — but NOT after stripping
# trailing whitespace, because the RFC 3676 signature delimiter is
# literally ``"-- "`` (two dashes, space, newline) and a ``.rstrip()``
# pass would collapse it into ``"--"`` and miss real signatures.
#
# Each marker also names the kind of the text it starts (see
# ``segment_for_embedding``).
_HARD_CUT_PATTERNS: tuple[tuple[re.Pattern[str], ChunkKind], ...] = (
    # RFC 3676 signature separator. The trailing space is significant.
    (re.compile(r"^-- $"), "signature"),
    # Outlook / Exchange forward or reply header block.
    (re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE), "quote"),
    # Gmail / Apple Mail forward header.
    (re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}\s*$", re.IGNORECASE), "forwarded"),
    # Apple Mail forward preamble.
    (re.compile(r"^Begin forwarded message:\s*$", re.IGNORECASE), "forwarded"),
)

# Reply-header lines like "On Mon, Jan 1, 2024 at 10:00 AM Alice wrote:".
# Treated as a SKIP-LINE (continue past it) rather than a hard cut —
# inline replies place the user's new text *between* the quoted
# blocks that follow this header, so cutting here drops those answers
# entirely. The ``>``-line filter still removes the quoted history.
#
# Coverage spans the major Western-language Gmail/Apple Mail
# attribution shapes so the indexer pipeline doesn't drop quoted
# history cleanly in English while leaking the same noise in other
# languages.
#
# Anchoring on ``<addr@host>`` (the email address in angle brackets)
# is the load-bearing safeguard against false-positive stripping of
# real user prose: a German sentence like ``"Am Montag schrieb der
# Manager folgendes:"`` is a routine sentence-end colon, and the
# verbs ``schrieb``/``schreef``/``a écrit``/``escribió``/``ha
# scritto`` are everyday past-tense forms — without the bracket
# requirement, an aggressive end-of-line match silently drops the
# user's own content. Gmail/Apple Mail always include the address;
# clients that omit it leak the attribution line into the indexed
# body (minor noise) but no user content is dropped. English
# ``wrote:`` is rarer in prose so the bracket requirement is mainly
# defense-in-depth there.
#
# Two-line wrapped variants (Gmail wraps the attribution when the
# address pushes it past ~78 chars) are handled by
# ``_WRAPPED_REPLY_HEADER_PATTERNS`` in a pre-pass that removes the
# wrapped span before the loop runs.
_EMAIL_RE_FRAGMENT = r"<[^<>\s]+@[^<>\s]+>"
_REPLY_HEADER_PATTERNS: tuple[re.Pattern[str], ...] = (
    # English: "On <date> ... Alice <alice@example.com> wrote:"
    re.compile(rf"^On\b.*{_EMAIL_RE_FRAGMENT}.*\bwrote:\s*$"),
    # German: "Am <date> schrieb Alice <alice@example.com>:" — verb
    # comes BEFORE the address. Without ``<email>:`` at end-of-line
    # this matched any German sentence beginning with ``Am`` and
    # ending with ``schrieb ... :``.
    re.compile(rf"^Am\b.*\bschrieb\b.*{_EMAIL_RE_FRAGMENT}\s*:\s*$"),
    # French: "Le <date>, Alice <alice@example.com> a écrit :"
    # (French convention puts a space before the colon; accept both
    # forms). Address comes BEFORE the verb.
    re.compile(rf"^Le\b.*{_EMAIL_RE_FRAGMENT}.*\ba écrit\s*:\s*$"),
    # Spanish: "El <date>, Alice <alice@example.com> escribió:"
    re.compile(rf"^El\b.*{_EMAIL_RE_FRAGMENT}.*\bescribió\s*:\s*$"),
    # Italian: "Il giorno <date> Alice <alice@example.com> ha scritto:"
    re.compile(rf"^Il\b.*{_EMAIL_RE_FRAGMENT}.*\bha scritto\s*:\s*$"),
    # Dutch: "Op <date> schreef Alice <alice@example.com>:" — verb
    # comes BEFORE the address, like German.
    re.compile(rf"^Op\b.*\bschreef\b.*{_EMAIL_RE_FRAGMENT}\s*:\s*$"),
)

# Longest line tested against ``_REPLY_HEADER_PATTERNS``. A long
# attribution (weekday, full date, time zone, long display name and
# address) is about 200 chars; the worst-case match on a hostile line at
# this length is well under a millisecond.
_MAX_REPLY_HEADER_CHARS = 300

# Two-line wrapped reply headers. Gmail wraps the attribution when the
# address makes it longer than ~78 chars, pushing the verb (and the
# trailing colon) onto a line of its own. A pre-pass removes the
# wrapped span entirely so the line loop downstream sees a clean body.
# Anchoring on both the lead word AND the verb-only continuation keeps
# false-positive risk low: a body that happens to start a sentence
# with "On" and has "wrote:" on the next line is vanishingly rare,
# and the continuation must be just the verb plus colon (modulo
# whitespace) to match.
#
# ``\r?`` between lines accommodates CRLF-line-ending bodies (RFC 5322
# requires CRLF on the wire; some parsers preserve it through to
# ``body_text``). The trailing ``[ \t]*\r?$`` must ALSO consume the
# optional ``\r`` before ``\n`` — without it, ``$`` cannot anchor on
# CRLF lines because ``[ \t]*`` does not match ``\r`` and the
# multiline ``$`` only matches immediately before ``\n``. The
# single-line patterns above use ``\s*$`` (which includes ``\r``) so
# this asymmetry was previously silent.
_WRAPPED_REPLY_HEADER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^On\b[^\r\n]*\r?\n[ \t]*wrote:[ \t]*\r?$", re.MULTILINE),
    re.compile(r"^Am\b[^\r\n]*\r?\n[ \t]*schrieb\b[^\r\n]*:[ \t]*\r?$", re.MULTILINE),
    re.compile(r"^Le\b[^\r\n]*\r?\n[ \t]*a écrit\s*:[ \t]*\r?$", re.MULTILINE),
    re.compile(r"^El\b[^\r\n]*\r?\n[ \t]*escribió\b[^\r\n]*:[ \t]*\r?$", re.MULTILINE),
    re.compile(r"^Il\b[^\r\n]*\r?\n[ \t]*ha scritto\b[^\r\n]*:[ \t]*\r?$", re.MULTILINE),
    re.compile(r"^Op\b[^\r\n]*\r?\n[ \t]*schreef\b[^\r\n]*:[ \t]*\r?$", re.MULTILINE),
)

# Outlook-style forward/reply block header. Newer Outlook omits the
# "-----Original Message-----" dashed delimiter and emits a bare
# ``From:/Sent:/To:/Subject:`` block at the top of the quoted history.
#
# Match the FULL four-line shape: ``From:`` line, then ``Sent:`` or
# ``Date:`` line (with any number of blank lines between — Outlook
# double-spacing is common), then ``Subject:`` within a few lines.
# Requiring ``Subject:`` is the load-bearing safeguard: a body that
# happens to contain ``From: someone\nDate: 2024-01-01`` as agenda /
# calendar / "From the desk of" prose has no following ``Subject:``
# header and is correctly NOT treated as a quoted block. Earlier
# versions matched on just ``From: + Sent|Date:`` and silently
# truncated agenda bodies. ``To:``/``Cc:`` lines are tolerated as
# intermediate filler so the most common four-header shape
# (From/Sent/To/Subject) still matches.
_OUTLOOK_BLOCK_PATTERN: re.Pattern[str] = re.compile(
    r"^From:\s.*\r?\n"
    r"(?:[ \t]*\r?\n)*"
    r"(?:Sent|Date):\s.*\r?\n"
    r"(?:[^\r\n]*\r?\n){0,4}"
    r"Subject:\s",
    re.MULTILINE,
)


# Most segments ``segment_for_embedding`` returns for a body with no body
# text before merging the rest by kind. Real quote-only or forward-only
# messages have a handful of runs; the merge adds at most one segment
# per non-body kind.
_MAX_FALLBACK_SEGMENTS = 32


@dataclass(frozen=True)
class Segment:
    """A run of a message body that is all one kind of text."""

    kind: ChunkKind
    text: str


def strip_for_embedding(body_text: str) -> str:
    """Return ``body_text`` with quoted replies and signatures removed.

    Intended for the embedding path only. The output is a best-effort
    approximation of the "new content" portion of the message:

    - lines beginning with ``>`` (any depth) are dropped;
    - reply-header lines (``On ... wrote:``) are dropped but the loop
      continues past them so inline answers between quoted blocks
      survive;
    - anything from the first hard-cut marker onward (signature
      delimiter, forward preamble) is dropped;
    - surrounding whitespace is trimmed from the result.

    When the stripped result is empty (a reply that is literally just
    "On ... wrote:" followed by the quoted thread, or a top-posted
    reply that is entirely below a forward marker), the original
    ``body_text`` is returned so the embedding has *something* to go on
    rather than an empty string, which degrades the nearest-neighbor
    search for the thread as a whole.
    """
    if not body_text:
        return body_text
    text, cut = _pre_pass(body_text)
    stripped = _body_lines(text, cut)
    if not stripped:
        # Reply with no detectable "new content" — fall back to the
        # original body so the embedding is never seeded from an empty
        # string. An empty embedding input collapses the vector toward
        # the model's default response and poisons similarity ranking
        # for the whole thread.
        return body_text
    return stripped


def segment_for_embedding(body_text: str) -> list[Segment]:
    """Return the kind-tagged segments of ``body_text`` the indexer chunks.

    Segmentation happens before chunking, so a chunk never spans kinds
    (the chunker chunks each segment on its own). Each line gets a kind
    from the rules ``strip_for_embedding`` applies:

    - ``quote``: ``>`` lines, reply-header lines, and everything from an
      Outlook reply block or a ``-----Original Message-----`` line on;
    - ``signature``: from the RFC 3676 ``-- `` delimiter on;
    - ``forwarded``: from a forward preamble on;
    - ``body``: every other line before the first of those markers.

    After a marker, ``>`` and reply-header lines are still ``quote``,
    a later marker switches the kind, and any other line keeps the
    current marker's kind.

    When the message has body text, the result is one ``body`` segment
    holding exactly ``strip_for_embedding``'s output: the other kinds
    are left out, as before. Only a message with no body text (the
    case ``strip_for_embedding`` falls back on the whole original) is
    chunked as its non-body segments, one per run of a kind, so its
    chunks say what they hold. In that case the two-line reply headers
    removed by the wrapped-header pre-pass are not part of any segment.
    """
    if not body_text:
        return []
    text, cut = _pre_pass(body_text)
    # The body scan stops at the first marker, as before kinds, so a
    # message with body text costs no more than it did; only a message
    # with none is classified to its end.
    stripped = _body_lines(text, cut)
    if stripped:
        return [Segment("body", stripped)]
    return _fallback_segments(text, cut)


def _pre_pass(body_text: str) -> tuple[str, int]:
    """Return ``body_text`` after the pre-passes, and where quoted history starts.

    Pre-pass 1 removes two-line wrapped reply headers entirely; the line
    loop would otherwise see the first half of the wrapped header as
    junk content because ``_REPLY_HEADER_PATTERNS`` only matches
    single-line attributions. Pre-pass 2 finds an Outlook
    ``From:/Sent:/...`` block: everything from its start (the returned
    offset, or the text's length when there is none) is quoted history,
    mirroring the dashed Outlook delimiter in ``_HARD_CUT_PATTERNS``.
    The block starts a line (``^`` after a ``\n``), so splitting the
    text before and after it yields the same lines as splitting the
    whole.
    """
    for pattern in _WRAPPED_REPLY_HEADER_PATTERNS:
        body_text = pattern.sub("", body_text)
    outlook_match = _OUTLOOK_BLOCK_PATTERN.search(body_text)
    cut = len(body_text) if outlook_match is None else outlook_match.start()
    return body_text, cut


def _body_lines(text: str, cut: int) -> str:
    """Return the ``body`` lines of ``text[:cut]``, joined and trimmed."""
    kept: list[str] = []
    for raw_line in text[:cut].splitlines():
        # Hard-cut markers (signature delimiter, forward preamble) end
        # the loop: they mark the structural end of the new-content
        # portion, and anything below is reliably not the user's reply.
        if _hard_cut_kind(raw_line) is not None:
            break
        # Reply-header lines like "On ... wrote:" are skipped but do
        # NOT cut — the user's inline answers may live between the
        # quoted blocks that follow. Checked before the ``>`` rule so a
        # marker line still drops even if a client prefixes it with a
        # quote character. Quoted-reply lines accept any amount of
        # leading whitespace before the ``>`` (some clients indent).
        if _is_reply_header(raw_line) or _is_quoted_line(raw_line):
            continue
        kept.append(raw_line)
    return "\n".join(kept).strip()


# Line terminators ``str.splitlines`` splits on. A line it returns with
# ``keepends=True`` ends in at most one of them (``\r\n`` counts as one),
# so stripping this set removes exactly that terminator.
_LINE_TERMINATORS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"


def _fallback_segments(text: str, cut: int) -> list[Segment]:
    """Classify every line of a body with no body text into kind runs.

    One pass over the lines (line endings kept), each rule a bounded
    per-line check. A blank line (every ``body`` line is blank here)
    joins the run before it, and blank lines before the first run are
    dropped. Every segment is at least one chunk, so a body that
    alternates kinds line by line would otherwise turn into one chunk
    (and one embedding) per line: once ``_MAX_FALLBACK_SEGMENTS - 1``
    runs are closed, later runs are joined per kind as they are found,
    in order of first appearance. Kinds stay separate and no text is
    lost, though their lines are no longer interleaved. Text goes
    straight into one buffer per open run, so what is held is the text
    itself in at most ``_MAX_FALLBACK_SEGMENTS - 1`` closed runs plus
    one buffer per kind, not an entry per line or per run.
    """
    segments: list[Segment] = []
    merged: dict[ChunkKind, io.StringIO] = {}
    run_kind: ChunkKind | None = None
    # Buffer of the open run: its own while runs are still kept apart,
    # its kind's merged buffer once the cap is reached.
    run = io.StringIO()

    halves: tuple[tuple[str, ChunkKind], ...] = ((text[:cut], "body"), (text[cut:], "quote"))
    for half, start_kind in halves:
        mode = start_kind
        for raw_line in half.splitlines(keepends=True):
            line = raw_line.rstrip(_LINE_TERMINATORS)
            # A marker switches the kind of the lines after it; ``>``
            # and reply-header lines are quotes wherever they appear.
            marker_kind = _hard_cut_kind(line)
            if marker_kind is not None:
                mode = kind = marker_kind
            elif _is_reply_header(line) or _is_quoted_line(line):
                kind = "quote"
            else:
                kind = mode
            if line.strip() and kind != run_kind:
                if run_kind is not None and run is not merged.get(run_kind):
                    segments.append(Segment(run_kind, run.getvalue()))
                run_kind = kind
                if len(segments) < _MAX_FALLBACK_SEGMENTS - 1:
                    run = io.StringIO()
                else:
                    if kind not in merged:
                        merged[kind] = io.StringIO()
                    run = merged[kind]
            if run_kind is not None:
                run.write(raw_line)
    if run_kind is not None and run is not merged.get(run_kind):
        segments.append(Segment(run_kind, run.getvalue()))
    return segments + [Segment(kind, buf.getvalue()) for kind, buf in merged.items()]


def _hard_cut_kind(line: str) -> ChunkKind | None:
    for pattern, kind in _HARD_CUT_PATTERNS:
        if pattern.match(line):
            return kind
    return None


def _is_reply_header(line: str) -> bool:
    # The patterns' ``.*`` spans backtrack quadratically on a long line
    # that starts with a lead word and repeats ``<addr>`` fragments
    # (#239). Real attributions are well under the cap, so a longer
    # line is prose, and skipping it bounds the work per line.
    if len(line) > _MAX_REPLY_HEADER_CHARS:
        return False
    return any(pattern.match(line) for pattern in _REPLY_HEADER_PATTERNS)


def _is_quoted_line(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith(">")
