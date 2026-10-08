"""Attached-email extractor (``message/rfc822``, ``application/eml``,
``.eml``), #922.

The payload is the attached email: for a ``message/rfc822`` part, its
serialized form (``parser._serialized_body``), and for an ``.eml`` or
``application/eml`` file, its decoded bytes. The text is, for the
attached email and then each attached email nested in it (depth first,
in document order):

* a ``[Attached message, depth N]`` line, for a nested one (the
  attachment itself is depth 1 and has none);
* its ``Subject``, ``From``, ``To``, ``Cc`` and ``Date`` as labelled
  lines (``Subject: ...``), the first of each, decoded with the parser's
  header decoder;
* its body, chosen exactly as the parser chooses a message's body
  (``parser._extract_body_and_attachments``), without quote stripping.

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
(#1242).
"""

from __future__ import annotations

import email
import email.errors
import email.message
import logging
from collections import Counter
from collections.abc import Callable

from .. import parser
from . import _runner, warn_extractor_cap

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

# The budgets the child may report as having cut the text.
_CAP_NAMES = frozenset(
    {"eml_header_chars", "eml_text_chars", "eml_parts", "eml_text_parts", "eml_nested_messages"}
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
        on_progress=on_progress,
    )
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
    stack: list[tuple[email.message.Message, int]] = [(root, 1)]
    while stack and not text.full:
        msg, depth = stack.pop()
        # A section per message: its label and header lines, then its
        # body after a blank line; sections apart by a blank line.
        lines = [f"[Attached message, depth {depth}]"] if depth > 1 else []
        lines.extend(_header_lines(msg, caps))
        if lines:
            text.add(("\n\n" if text.pieces else "") + "\n".join(lines))
        counted: Counter[str] = Counter()
        start = len(walk.nested)
        body, _ = parser._extract_body_and_attachments(msg, caps=counted, walk=walk)
        if counted["mime_parts"]:
            caps["eml_parts"] = None
        if counted["body_parts"]:
            caps["eml_text_parts"] = None
        if body:
            text.add(("\n\n" if text.pieces else "") + body)
        found = walk.nested[start:]
        del walk.nested[start:]
        if found and depth + 1 > _MAX_DEPTH:
            caps["eml_nested_messages"] = None
            continue
        # Decoded in document order, so the decoded-bytes budget goes to
        # the first ones; pushed in reverse, so the first is rendered first.
        inner_messages: list[email.message.Message] = []
        for part in found:
            inner, lost = _inner_message(part, decodable)
            if lost:
                caps["eml_nested_messages"] = None
            if inner is not None:
                inner_messages.append(inner)
        stack.extend((inner, depth + 1) for inner in reversed(inner_messages))
    if text.full:
        caps["eml_text_chars"] = None
    return "".join(text.pieces), list(caps)


def _header_lines(msg: email.message.Message, caps: dict[str, None]) -> list[str]:
    """``msg``'s labelled header lines."""
    lines: list[str] = []
    for name in HEADERS:
        raw = msg.get(name)
        if raw is None:
            continue
        if isinstance(raw, str) and len(raw) > _MAX_HEADER_CHARS:
            # Cut before decoding, so a crafted header costs one slice.
            raw = raw[:_MAX_HEADER_CHARS]
            caps["eml_header_chars"] = None
        value = parser._decode_text_header(raw)
        if len(value) > _MAX_HEADER_CHARS:
            value = value[:_MAX_HEADER_CHARS]
            caps["eml_header_chars"] = None
        if value:
            lines.append(f"{name}: {value}")
    return lines


def _inner_message(
    part: email.message.Message, decodable: list[int]
) -> tuple[email.message.Message | None, bool]:
    """The email a nested ``message/rfc822`` part carries, and whether
    one was there but could not be read. A transfer-encoded part (not
    allowed by RFC 2046, but sent) is decoded as the parser decodes it,
    charging ``decodable``."""
    container = part
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
        container = decoded
    children = container.get_payload()
    if isinstance(children, list) and children and isinstance(children[0], email.message.Message):
        return children[0], False
    return None, False
