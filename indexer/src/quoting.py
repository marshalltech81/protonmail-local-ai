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
    # Gmail / Apple Mail inline reply header: "On <date>, <name> wrote:"
    # Matched as a single line. Multi-line wrapped variants are left to a
    # future iteration rather than risking a false positive here.
    re.compile(r"^On\b.*\bwrote:\s*$"),
)


def strip_for_embedding(body_text: str) -> str:
    """Return ``body_text`` with quoted replies and signatures removed.

    Intended for the embedding path only. The output is a best-effort
    approximation of the "new content" portion of the message:

    - lines beginning with ``>`` (any depth) are dropped;
    - anything from the first hard-cut marker onward is dropped;
    - surrounding whitespace is trimmed from the result.

    When the stripped result is empty (a reply that is literally just
    "On ... wrote:" followed by the quoted thread), the original
    ``body_text`` is returned so the embedding has *something* to go on
    rather than an empty string, which degrades the nearest-neighbor
    search for the thread as a whole.
    """
    if not body_text:
        return body_text

    kept: list[str] = []
    for raw_line in body_text.splitlines():
        # Hard-cut markers end the loop entirely. Checked before the
        # ``>`` rule so a quoted marker line still cuts (e.g. a nested
        # reply that includes its own "On ... wrote:" block).
        if _is_hard_cut(raw_line):
            break
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
        return body_text
    return stripped


def _is_hard_cut(line: str) -> bool:
    return any(pattern.match(line) for pattern in _HARD_CUT_PATTERNS)


def _is_quoted_line(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith(">")
