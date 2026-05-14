"""
Quoted-reply and signature stripping for embedding input.

Thread-level embeddings accumulate every reply's quoted history, so by the
tenth message the vector is dominated by whatever text got quoted most —
often a boilerplate contract, signature, or the original question — and
drifts away from the actual content of the latest replies. Stripping
quotes and signatures before the text reaches ``Embedder.embed`` keeps
the vector aligned with the substantive content of each message.

The stored ``body_text`` (FTS index input) is left alone: users
legitimately search quoted text and signatures, so this transform only
applies at the embedding boundary.

Heuristics are intentionally narrow. This is a domain-fraught problem
and an aggressive stripper that eats real body content is worse than a
conservative one that occasionally leaves quoted text in. Everything
here is a simple line-based rule; no ML, no language detection.
"""

import re

# Full-line markers that cut the rest of the message. Matched against the
# line body after stripping the trailing newline — but NOT after stripping
# trailing whitespace, because the RFC 3676 signature delimiter is
# literally ``"-- "`` (two dashes, space, newline) and a ``.rstrip()``
# pass would collapse it into ``"--"`` and miss real signatures.
_HARD_CUT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # RFC 3676 signature separator. The trailing space is significant.
    re.compile(r"^-- $"),
    # Outlook / Exchange forward or reply header block.
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    # Gmail / Apple Mail forward header.
    re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}\s*$", re.IGNORECASE),
    # Apple Mail forward preamble.
    re.compile(r"^Begin forwarded message:\s*$", re.IGNORECASE),
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
# languages. Each verb is anchored at end-of-line so a body that
# merely mentions the verb in prose is not falsely cut. Two-line
# wrapped variants (Gmail wraps the attribution when the address
# pushes it past ~78 chars) are handled by ``_WRAPPED_REPLY_HEADER_PATTERNS``
# in a pre-pass that joins them onto one line before the loop runs.
_REPLY_HEADER_PATTERNS: tuple[re.Pattern[str], ...] = (
    # English: "On <date>, <name> wrote:"
    re.compile(r"^On\b.*\bwrote:\s*$"),
    # German: "Am <date> schrieb <name>:"
    re.compile(r"^Am\b.*\bschrieb\b.*:\s*$"),
    # French: "Le <date>, <name> a écrit :" (French convention uses a
    # space before the colon; accept both forms).
    re.compile(r"^Le\b.*\ba écrit\s*:\s*$"),
    # Spanish: "El <date>, <name> escribió:"
    re.compile(r"^El\b.*\bescribió\s*:\s*$"),
    # Italian: "Il giorno <date> <name> ha scritto:"
    re.compile(r"^Il\b.*\bha scritto\s*:\s*$"),
    # Dutch: "Op <date> schreef <name>:"
    re.compile(r"^Op\b.*\bschreef\b.*:\s*$"),
)

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
# ``\r?`` accommodates CRLF-line-ending bodies (RFC 5322 requires CRLF
# on the wire; some parsers preserve it through to ``body_text``).
_WRAPPED_REPLY_HEADER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^On\b[^\r\n]*\r?\n[ \t]*wrote:[ \t]*$", re.MULTILINE),
    re.compile(r"^Am\b[^\r\n]*\r?\n[ \t]*schrieb\b[^\r\n]*:[ \t]*$", re.MULTILINE),
    re.compile(r"^Le\b[^\r\n]*\r?\n[ \t]*a écrit\s*:[ \t]*$", re.MULTILINE),
    re.compile(r"^El\b[^\r\n]*\r?\n[ \t]*escribió\b[^\r\n]*:[ \t]*$", re.MULTILINE),
    re.compile(r"^Il\b[^\r\n]*\r?\n[ \t]*ha scritto\b[^\r\n]*:[ \t]*$", re.MULTILINE),
    re.compile(r"^Op\b[^\r\n]*\r?\n[ \t]*schreef\b[^\r\n]*:[ \t]*$", re.MULTILINE),
)

# Outlook-style forward/reply block header. Newer Outlook omits the
# "-----Original Message-----" dashed delimiter and emits a bare
# ``From:/Sent:/To:/Subject:`` block at the top of the quoted history.
# Match a ``From:`` line followed (after at most one blank line) by a
# ``Sent:`` or ``Date:`` line — a distinctive shape that prose
# mentioning "From: someone" without a following timestamp line won't
# trip. The match position becomes a hard cut for the rest of the body.
_OUTLOOK_BLOCK_PATTERN: re.Pattern[str] = re.compile(
    r"^From:\s.*\r?\n(?:[ \t]*\r?\n)?(?:Sent|Date):\s",
    re.MULTILINE,
)


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

    # Preserve the input for the empty-fallback below: the pre-passes
    # below mutate a working copy, and returning the mutated form on
    # fallback would defeat the purpose (an Outlook-only quote would
    # fall back to an empty string, the exact case the fallback exists
    # to prevent).
    original = body_text

    # Pre-pass 1 — collapse two-line wrapped reply headers by removing
    # the span entirely; the line loop downstream would otherwise see
    # the first half of the wrapped header as junk content because
    # ``_REPLY_HEADER_PATTERNS`` only matches single-line attributions.
    for pattern in _WRAPPED_REPLY_HEADER_PATTERNS:
        body_text = pattern.sub("", body_text)

    # Pre-pass 2 — Outlook ``From:/Sent:/...`` block. Truncate body_text
    # at the start of the block so the line loop never sees the quoted
    # history. Mirrors the behavior of the dashed Outlook delimiter in
    # ``_HARD_CUT_PATTERNS``.
    outlook_match = _OUTLOOK_BLOCK_PATTERN.search(body_text)
    if outlook_match is not None:
        body_text = body_text[: outlook_match.start()]

    kept: list[str] = []
    for raw_line in body_text.splitlines():
        # Hard-cut markers end the loop entirely (signature delimiter,
        # forward preamble). These mark the structural end of the
        # new-content portion: anything below is reliably not the
        # user's reply.
        if _is_hard_cut(raw_line):
            break
        # Reply-header lines like "On ... wrote:" are skipped but do
        # NOT cut — the user's inline answers may live between the
        # quoted blocks that follow. Checked before the ``>`` rule so a
        # marker line still drops even if a client prefixes it with a
        # quote character.
        if _is_reply_header(raw_line):
            continue
        # Quoted-reply lines. Accept any amount of leading whitespace
        # before the ``>`` — some mail clients indent quoted blocks.
        if _is_quoted_line(raw_line):
            continue
        kept.append(raw_line)

    stripped = "\n".join(kept).strip()
    if not stripped:
        # Reply with no detectable "new content" — fall back to the
        # original body so the embedding is never seeded from an empty
        # string. An empty embedding input collapses the vector toward
        # the model's default response and poisons similarity ranking
        # for the whole thread.
        return original
    return stripped


def _is_hard_cut(line: str) -> bool:
    return any(pattern.match(line) for pattern in _HARD_CUT_PATTERNS)


def _is_reply_header(line: str) -> bool:
    return any(pattern.match(line) for pattern in _REPLY_HEADER_PATTERNS)


def _is_quoted_line(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith(">")
