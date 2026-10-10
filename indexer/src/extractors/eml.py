"""Attached-email extractor (``message/rfc822``, ``application/eml``,
``.eml``), #922.

The payload is the attached email: for a ``message/rfc822`` part, its
serialized form (``parser._serialized_body``), and for an ``.eml`` or
``application/eml`` file, its decoded bytes. The text is, for the
attached email and then each ``message/rfc822`` email nested in it,
attached or inline (depth first, in document order):

* a ``[Attached message, depth N]`` line, for a nested one (the
  attachment itself is depth 1 and has none);
* its ``Subject``, ``From``, ``To``, ``Cc`` and ``Date`` as labelled
  lines (``Subject: ...``), the first of each, decoded with the parser's
  header decoder;
* its body, chosen exactly as the parser chooses a message's body
  (``parser._extract_body_and_attachments``), without quote stripping.

An inline nested email under a ``multipart/alternative`` or
``multipart/related`` takes part in that container's choice: it is
rendered where it sits only when chosen (``parser.BodyWalk``).

The inner From is a claim inside a claim (#1235): it is searchable
attachment text only, never a participant, an authority input or a
direction. Attachments of these emails are not extracted here: inside
a ``message/rfc822`` part the enclosing message's parse records each as
an occurrence of its own (inside an ``.eml`` file, which the parser does
not walk, they are not indexed at all). That includes a nested ``.eml``
or ``application/eml`` file, which is a leaf attachment, not a
``message/rfc822`` part; only the latter is rendered as a nested
message. ``message/delivery-status`` is not routed here
(``extractors._MIME_DISPATCH``).

Nothing new parses the input: the standard library parses the payload
and the parser's own walk and decoders read it. Every cost dimension is
bounded in one place, the child process the extraction runs in
(decision 42, #1290), and by budgets shared by all the messages of one
payload:

* bytes: the dispatcher's ``max_bytes``;
* parts walked and text parts decoded: ``_MAX_PARTS`` and
  ``_MAX_TEXT_PARTS`` (the parser's per-message caps), across every
  message rendered;
* nesting: ``_MAX_DEPTH`` levels of attached email, and the
  transfer-encoded bytes nested ones may decode,
  ``_MAX_DECODED_BYTES``; a nested email left out for either, or whose
  transfer encoding does not decode, is a cap;
* headers: five, the first occurrence of each, ``_MAX_HEADER_CHARS``
  characters each;
* text: ``_MAX_TEXT_CHARS`` characters in all;
* the standard library's parse, html2text and anything else, which
  have no per-input bound of their own: the child's address-space, CPU
  and wall-clock limits. A ``RecursionError`` (a payload nested past
  Python's recursion limit, from about 1,000 levels) or ``MemoryError``
  there is reported by type and recorded ``failed``.

Each budget that cut the text is reported to the parent, which logs it
through ``warn_extractor_cap``, so the result is marked incomplete
(#1242). So is a decode that lost bytes: a body text part's
(``eml_body_decode``), a nested email's base64, any body text part or
nested email in quoted-printable, whose loss records nothing to detect
(#1288), and any body text part in another encoding that is not
identity (uuencode and its aliases, or an unknown value), each counted
only when the body keeps that part, and a nested email whose transport
text lost a line to the parse. So is a
part declared ``multipart/*`` that the standard library left
undecomposed (no or a missing boundary), whose text is never read,
when the body could keep it (``eml_body_structure``), and so is a
message header line the parse dropped: a first line starting with
whitespace or a ``From `` line after the first, or the first line of a
body text part the body keeps (``eml_header_lines``). A nested email in any other transfer encoding
(uuencode and its aliases included) is not decoded: only its label is
rendered, as an ``eml_nested_messages`` cut. The
decoders' fallbacks (headers, part filenames, body charsets) replace
characters rather than drop text: they are counted
(``eml_*_degraded``, #1314) and do not mark the text incomplete (#1315).
"""

from __future__ import annotations

import email
import email.errors
import email.message
import logging
from collections import Counter
from collections.abc import Callable

from .. import parser
from . import (
    CHILD_DEGRADATION_KEYS,
    _runner,
    apply_child_degradation,
    note_eml_degraded,
    warn_extractor_cap,
)

log = logging.getLogger("indexer.extractor.eml")

# Address space (``RLIMIT_AS``), CPU seconds (``RLIMIT_CPU``) and
# wall-clock seconds the child may use, past the CPU limit so a CPU-bound
# child meets that first. Plainly measured in the indexer image, child
# peak RSS and CPU time, payloads at the 32 MB ``max_bytes`` default:
#
# * a small attached email: 32 MB and 0.08 s (starting the child);
# * a 27 MB email with ten base64 attachments: 226 MB and 0.3 s;
# * a 32 MB plain body: 384 MB and 0.16 s; 32 MB of invalid UTF-8:
#   448 MB and 0.5 s;
# * a 32 MB HTML body: 444 MB and 18 s; 32 MB of unclosed ``<b>``:
#   384 MB and 23 s; 200 HTML parts of 160 KB: 256 MB and 24 s
#   (html2text, which has no per-input bound of its own);
# * crafted structure: 800,000 empty parts 688 MB and 5.2 s, 4 million
#   header fields 995 MB and 3.4 s, 70,000 sibling attached emails
#   619 MB and 4.0 s (the standard library's parse);
# * nesting: 700 levels 33 MB and 0.13 s; 1,500 levels raise
#   ``RecursionError`` in the parse, reported by type.
#
# Every case above completes under these limits. 1 GiB is about 2.3
# times the largest peak for a payload that is not crafted structure;
# 60 s is 2.5 times the slowest case.
CHILD_MAX_ADDRESS_SPACE_BYTES = 1024 * 1024 * 1024
CHILD_MAX_CPU_SECONDS = 60
CHILD_TIMEOUT_SECONDS = 75.0

# Headers rendered, in order, and the characters kept of each. A real
# header is far shorter.
HEADERS: tuple[str, ...] = ("Subject", "From", "To", "Cc", "Date")
_MAX_HEADER_CHARS = 2000

# Characters of text returned, labels and separators included, as the
# other extractors' text budgets.
_MAX_TEXT_CHARS = 10_000_000

# Shared by every message rendered from one payload.
_MAX_PARTS = parser.MAX_WALKED_PARTS
_MAX_TEXT_PARTS = parser.MAX_BODY_TEXT_PARTS
_MAX_DEPTH = parser.MAX_ATTACHED_MESSAGE_DEPTH
_MAX_DECODED_BYTES = parser.MAX_DECODED_ATTACHMENT_BYTES

# Bytes of the child's output read: the text budget at UTF-8's worst
# case of four bytes a character, plus the frames.
_MAX_OUTPUT_BYTES = 4 * _MAX_TEXT_CHARS + 64 * 1024

# Transfer encodings a nested email is read in: identity, or decoded as
# the parser decodes them. Any other (uuencode and its aliases included)
# is left unread and counted as a cut.
_READ_ENCODINGS = frozenset({"", "7bit", "8bit", "binary", "base64", "quoted-printable"})

# The budgets the child may report as having cut the text.
_CAP_NAMES = frozenset(
    {
        "eml_header_chars",
        "eml_text_chars",
        "eml_parts",
        "eml_text_parts",
        "eml_nested_messages",
        "eml_body_decode",
        "eml_body_structure",
        "eml_header_lines",
    }
)


def extract(
    payload: bytes,
    *,
    ocr_enabled: bool = True,  # noqa: ARG001
    max_ocr_pages: int = 20,  # noqa: ARG001
    ocr_timeout_seconds: float | None = None,  # noqa: ARG001
    max_pdf_pages: int | None = None,  # noqa: ARG001
    on_progress: Callable[[], None] | None = None,
) -> tuple[str, str]:
    """The attached email's text, extracted in the child process.
    Returns (text, "eml")."""
    result = _runner.run_child(
        "eml",
        payload,
        max_address_space_bytes=CHILD_MAX_ADDRESS_SPACE_BYTES,
        max_cpu_seconds=CHILD_MAX_CPU_SECONDS,
        timeout_seconds=CHILD_TIMEOUT_SECONDS,
        max_output_bytes=_MAX_OUTPUT_BYTES,
        caps=_CAP_NAMES,
        counts=CHILD_DEGRADATION_KEYS,
        on_progress=on_progress,
    )
    # The child's degradation (#1314): the header-decoding fallbacks
    # (``eml_*_degraded``) and anything else recorded there.
    apply_child_degradation(log, "eml", result.counts)
    for cap in result.caps:
        warn_extractor_cap(log, cap, "attached email text cut at a budget")
    return result.text, "eml"


class _Text:
    """The text being built, cut at ``_MAX_TEXT_CHARS``."""

    def __init__(self) -> None:
        self.pieces: list[str] = []
        self.left = _MAX_TEXT_CHARS
        self.full = False

    def add(self, piece: str) -> None:
        if self.full:
            return
        if len(piece) > self.left:
            piece = piece[: self.left]
            self.full = True
        self.pieces.append(piece)
        self.left -= len(piece)


def extract_text(payload: bytes) -> tuple[str, list[str]]:
    """The attached email's text and the names of the budgets that cut
    it. Runs in the child process (``extractor_child``)."""
    root = email.message_from_bytes(payload)
    caps: dict[str, None] = {}
    text = _Text()
    walk = parser.BodyWalk(parts_left=_MAX_PARTS, text_parts_left=_MAX_TEXT_PARTS)
    decodable = [_MAX_DECODED_BYTES]
    # Depth first: a nested email's text follows its parent's body and
    # precedes the parent's next nested email.
    # ``None``: a nested email in a transfer encoding no decoder here
    # reads, shown by its label alone.
    stack: list[tuple[email.message.Message | None, int]] = [(root, 1)]
    # The decoders' fallbacks, whose lines the child cannot log, are
    # counted in ``walk.degraded`` and sent to the parent (#1314).
    degraded = walk.degraded
    # The walk reads each nested email where it meets it, in document
    # order under one decoded-bytes budget, and renders an inline one the
    # body chose under an alternative or related in place, with the same
    # label and headers (review round 12).
    walk.max_depth = _MAX_DEPTH
    walk.decode = lambda part: _read_nested(part, decodable)
    walk.render = lambda inner, depth: _section_head(inner, depth, caps, degraded)
    while stack and not text.full:
        msg, depth = stack.pop()
        # A section per message: its label and header lines, then its
        # body after a blank line; sections apart by a blank line.
        if msg is None:
            text.add(("\n\n" if text.pieces else "") + f"[Attached message, depth {depth}]")
            continue
        head = _section_head(msg, depth, caps, degraded)
        if head:
            text.add(("\n\n" if text.pieces else "") + head)
        counted: Counter[str] = Counter()
        start = len(walk.nested)
        walk.depth = depth
        body, _ = parser._extract_body_and_attachments(msg, caps=counted, walk=walk)
        if counted["mime_parts"]:
            caps["eml_parts"] = None
        if counted["body_parts"]:
            caps["eml_text_parts"] = None

        if body:
            text.add(("\n\n" if text.pieces else "") + body)
        # Already read by the walk; pushed in reverse, so the first is
        # rendered first.
        found = walk.nested[start:]
        del walk.nested[start:]
        stack.extend(reversed(found))
    if walk.decode_lost_parts:
        caps["eml_body_decode"] = None
    if walk.structure_lost_parts:
        caps["eml_body_structure"] = None
    if walk.header_lost_parts:
        caps["eml_header_lines"] = None
    if walk.nested_lost_parts:
        caps["eml_nested_messages"] = None
    if text.full:
        caps["eml_text_chars"] = None
    if degraded.total():
        note_eml_degraded(
            headers=degraded[parser.HEADER_DEGRADED],
            filenames=degraded[parser.FILENAME_DEGRADED],
            charsets=degraded[parser.CHARSET_DEGRADED],
        )
    return "".join(text.pieces), list(caps)


def _section_head(
    msg: email.message.Message, depth: int, caps: dict[str, None], degraded: Counter[str]
) -> str:
    """A message's section head: its depth label (below the root) and
    header lines. A header line the parse dropped is reported (review
    round 10), since its text is not indexed."""
    if any(isinstance(d, parser.DROPPED_HEADER_DEFECTS) for d in msg.defects):
        caps["eml_header_lines"] = None
    lines = [f"[Attached message, depth {depth}]"] if depth > 1 else []
    lines.extend(_header_lines(msg, caps, degraded))
    return "\n".join(lines)


def _header_lines(
    msg: email.message.Message, caps: dict[str, None], degraded: Counter[str]
) -> list[str]:
    """``msg``'s labelled header lines; the decoders' fallbacks are
    counted in ``degraded``."""
    lines: list[str] = []
    for name in HEADERS:
        raw = msg.get(name)
        if raw is None:
            continue
        if isinstance(raw, str) and len(raw) > _MAX_HEADER_CHARS:
            # Cut before decoding, so a crafted header costs one slice.
            raw = raw[:_MAX_HEADER_CHARS]
            caps["eml_header_chars"] = None
        value = parser._decode_text_header(raw, degraded)
        if len(value) > _MAX_HEADER_CHARS:
            value = value[:_MAX_HEADER_CHARS]
            caps["eml_header_chars"] = None
        if value:
            lines.append(f"{name}: {value}")
    return lines


def _read_nested(
    part: email.message.Message, decodable: list[int]
) -> tuple[email.message.Message | None, bool, bool]:
    """``BodyWalk.decode``: the email a nested ``message/rfc822`` part
    carries (``_inner_message``), whether text was or may have been lost,
    and whether an unread one still shows its depth label: one in
    uuencode, its aliases or anything else has no decoder, so its
    transport text is never indexed (review round 3)."""
    encoding = str(part.get("Content-Transfer-Encoding", "")).strip().lower()
    if encoding not in _READ_ENCODINGS:
        return None, True, True
    inner, lost = _inner_message(part, decodable)
    return inner, lost, False


def _inner_message(
    part: email.message.Message, decodable: list[int]
) -> tuple[email.message.Message | None, bool]:
    """The email a nested ``message/rfc822`` part carries, and whether
    one was there but could not be read, or was read with bytes lost
    (a lenient base64 decode). A transfer-encoded part (not
    allowed by RFC 2046, but sent) is decoded as the parser decodes it,
    charging ``decodable``."""
    container = part
    lossy = False
    encoding = str(part.get("Content-Transfer-Encoding", "")).strip().lower()
    if encoding in ("base64", "quoted-printable"):
        try:
            transport = parser._transport_text(part)
        except email.errors.MessageError, UnicodeError:
            return None, True
        decodable[0] -= len(transport)
        if decodable[0] < 0:
            return None, True
        content_type = str(part.get("Content-Type", "message/rfc822"))
        decoded = parser._decode_transport_form(transport, encoding, content_type)
        if decoded is None:
            return None, True
        # A lenient decode can drop bytes and still succeed: the email
        # is rendered, but its text is not whole (base64 review round 1,
        # quoted-printable #1288). A transport line the parse dropped is
        # lost too (review round 8).
        lossy = parser._transport_decode_lost(transport, encoding) or (
            parser._transport_lines_dropped(part)
        )
        container = decoded
    children = container.get_payload()
    if isinstance(children, list) and children and isinstance(children[0], email.message.Message):
        return children[0], lossy
    return None, lossy
