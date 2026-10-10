"""HTML attachment extractor, and the HTML conversion of message bodies.

Renders HTML down to the plain text the chunker expects with
``html2text`` (``ignore_links``, ``ignore_images``, no body wrap), so an
HTML attachment and an HTML message body produce comparable text for
retrieval.

``html2text`` has no per-input bound of its own (up to about 0.75 s of
CPU and 14 MB of memory per MB of HTML in the image, more for crafted
markup; ``docs/architecture.md``, "HTML conversion"), so every
conversion the indexer runs goes through one limited path (PLAN.md
decision 42, #1294): ``convert`` runs the documents in the extractor
child (``extractor_child.py`` running ``html_child.py``), started
through ``_runner.run_child``, whose launcher lowers the child's address
space (``RLIMIT_AS``) and CPU time (``RLIMIT_CPU``) before it starts,
under a wall-clock timeout. Two callers use it:

* ``extract``, the ``html`` attachment extractor: one document, the
  payload's bytes, decoded as UTF-8 with replacement in the child. An
  error in the child, a limit hit, a timeout or output that breaks the
  protocol is recorded ``failed`` by the dispatcher.
* ``convert_bodies``, the parser's conversion of a message's
  ``text/html`` body parts: every part of one message in one child
  (``parser._extract_body_and_attachments``), so a message costs one
  launch whatever its number of HTML parts. The parser decides what a
  failure does to the message.

The documents cross to the child as one payload file, their byte
lengths as the child's options. Each converted text comes back in the
runner's text frame prefixed by its length in characters and a colon
(``html_child``), so one frame carries them all; a prefix that does not
parse, a text past the end, or more or fewer texts than documents is
``ChildOutputError``, never text. The texts share one budget,
``_MAX_TEXT_CHARS``: the text that crosses it is cut there and the
documents after it are not converted, reported as the ``CAP_TEXT`` cap.

Each text comes back stripped, as the dispatcher and the parser strip
it, so surrounding whitespace never counts against the budget.
A lone surrogate, which UTF-8 cannot hold (a UTF-7 body can decode to
one), is replaced by ``?`` before a body crosses
(``replace_lone_surrogates``), and the parser logs the count; in process
it was kept, and the chunker's UTF-8 encode then failed the message.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence

import html2text

from . import CHILD_DEGRADATION_KEYS, apply_child_degradation, warn_extractor_cap
from ._runner import (
    ChildError,
    ChildOutputError,
    ToolCrashError,
    ToolExitError,
    ToolTimeoutError,
    run_child,
)

log = logging.getLogger("indexer.extractor.html")

# Characters of text the child returns for one call, every document's
# together; text past it is cut (``CAP_TEXT``). Real HTML mail converts
# to far less, though a body at the 50 MB parse cap can reach it (50 MB
# of plain paragraphs is about 51,000,000 characters). It bounds the
# child's output, which the parent reads whole.
_MAX_TEXT_CHARS = 10_000_000

# The cap the child reports when that budget cut the text.
CAP_TEXT = "html_text_chars"
_CAPS = frozenset({CAP_TEXT})

# Bytes of the child's output read: the text budget at UTF-8's worst
# case of four bytes a character, plus the length prefixes (at most
# ``parser.MAX_BODY_TEXT_PARTS`` of them) and the frames. Output past it
# cannot come from a working child.
_MAX_OUTPUT_BYTES = 4 * _MAX_TEXT_CHARS + 64 * 1024

# Address space (``RLIMIT_AS``), CPU seconds (``RLIMIT_CPU``) and
# wall-clock seconds the child may use, past the CPU limit so a CPU-bound
# child meets that first. Sized from a plain measurement in the indexer
# image (``docs/architecture.md``, "HTML conversion").
CHILD_MAX_ADDRESS_SPACE_BYTES = 1024 * 1024 * 1024
CHILD_MAX_CPU_SECONDS = 60
CHILD_TIMEOUT_SECONDS = 75.0

# The runner's exceptions for a run that produced no usable text: the
# child raised (by type name), was killed by a signal (the CPU limit, a
# crash), exited with an error (the address-space limit when the child
# cannot report it), ran past the timeout, or broke the protocol.
# Launching the child can also raise ``OSError`` (no process or scratch
# space left), which is the host's trouble, not the document's.
CHILD_FAILURES: tuple[type[Exception], ...] = (
    ChildError,
    ChildOutputError,
    ToolCrashError,
    ToolExitError,
    ToolTimeoutError,
)

# Characters of a length prefix the parent reads before its colon.
_MAX_PREFIX_DIGITS = 18

# A surrogate code point, which in a ``str`` is always a lone one.
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def html_to_text(source: str) -> str:
    """Render ``source`` to text with a fresh converter, the one
    configuration every HTML conversion uses.

    ``HTML2Text`` keeps parser state between ``handle`` calls, so a
    shared converter let one document's unclosed ``<style>`` blank every
    later one until some document closed it (#216).
    """
    h2t = html2text.HTML2Text()
    h2t.ignore_links = True
    h2t.ignore_images = True
    h2t.body_width = 0
    return h2t.handle(source)


def convert(documents: Sequence[bytes]) -> tuple[list[str], bool]:
    """Convert ``documents`` (UTF-8, decoded with replacement) in the
    extractor child. Returns their texts in order and whether the text
    budget cut them; when it did, the last text returned is cut and the
    documents after it have none. Raises one of ``CHILD_FAILURES``."""
    result = run_child(
        "html",
        b"".join(documents),
        options=[str(len(document)) for document in documents],
        max_address_space_bytes=CHILD_MAX_ADDRESS_SPACE_BYTES,
        max_cpu_seconds=CHILD_MAX_CPU_SECONDS,
        timeout_seconds=CHILD_TIMEOUT_SECONDS,
        max_output_bytes=_MAX_OUTPUT_BYTES,
        caps=_CAPS,
        counts=CHILD_DEGRADATION_KEYS,
    )
    apply_child_degradation(log, "html", result.counts)
    cut = CAP_TEXT in result.caps
    texts = split_texts(result.text, len(documents))
    if len(texts) != len(documents) and not (cut and texts):
        raise ChildOutputError
    return texts, cut


def split_texts(framed: str, count: int) -> list[str]:
    """The texts in the child's ``framed`` text, each prefixed by its
    length in characters and a colon, at most ``count`` of them. Raises
    ``ChildOutputError`` when the framing is broken."""
    texts: list[str] = []
    pos = 0
    while pos < len(framed):
        if len(texts) == count:
            raise ChildOutputError
        colon = framed.find(":", pos, pos + _MAX_PREFIX_DIGITS + 1)
        digits = framed[pos:colon] if colon > pos else ""
        if not (digits.isascii() and digits.isdigit()):
            raise ChildOutputError
        end = colon + 1 + int(digits)
        if end > len(framed):
            raise ChildOutputError
        texts.append(framed[colon + 1 : end])
        pos = end
    return texts


def replace_lone_surrogates(text: str) -> tuple[str, int]:
    """``text`` with each lone surrogate, which UTF-8 cannot carry,
    replaced by ``?``, and how many there were. One linear pass."""
    return _LONE_SURROGATE.subn("?", text)


def convert_bodies(bodies: Sequence[str]) -> tuple[list[str], bool]:
    """Convert a message's HTML body parts in one child (``convert``).
    The bodies hold no lone surrogate (``replace_lone_surrogates``)."""
    return convert([body.encode("utf-8") for body in bodies])


def extract(
    payload: bytes,
    *,
    ocr_enabled: bool = True,  # noqa: ARG001
    max_ocr_pages: int = 20,  # noqa: ARG001
    ocr_timeout_seconds: float | None = None,  # noqa: ARG001
    max_pdf_pages: int | None = None,  # noqa: ARG001
    on_progress: Callable[[], None] | None = None,  # noqa: ARG001
) -> tuple[str, str]:
    """Convert an HTML attachment to text in the extractor child.
    Returns (text, "html")."""
    texts, cut = convert([payload])
    if cut:
        warn_extractor_cap(log, CAP_TEXT, "html text cut at %d chars", _MAX_TEXT_CHARS)
    return texts[0], "html"
