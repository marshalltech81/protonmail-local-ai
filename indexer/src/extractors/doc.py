"""Legacy Word ``.doc`` extractor (#935).

A legacy ``.doc`` is an OLE2 compound file, which python-docx cannot
read. The dispatcher routes an OLE2 payload labelled ``.doc`` /
``application/msword`` here; a ``.doc``-labelled payload that is not
OLE2 (an OOXML file mislabelled as ``.doc``) still goes to ``docx``.

The text comes from ``catdoc`` (Debian's ``catdoc`` package), run by
``_runner.run_tool``: no shell, a wall-clock timeout, its output read up
to ``_MAX_OUTPUT_BYTES`` and its stderr discarded. ``-d utf-8`` fixes
the output charset whatever the locale, and ``-w`` turns off catdoc's
line wrapping so a paragraph stays one line for the chunker. Output past
the byte cap is not indexed: the text before it is kept and the cap is
reported through ``warn_extractor_cap``.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable

from . import warn_extractor_cap
from ._runner import ToolNotFoundError, run_tool

log = logging.getLogger("indexer.extractor.doc")

# Wall-clock seconds a catdoc / catppt run may take. catdoc reads a
# document in a single pass; the generated fixtures take a few
# milliseconds, so this is reached only by a tool that hangs.
TOOL_TIMEOUT_SECONDS = 60.0

# Bytes of a tool's output read, about four times the dispatcher's
# default ``max_extracted_chars`` (2,000,000) of mostly one-byte UTF-8,
# so the dispatcher's cap still decides the stored length.
_MAX_OUTPUT_BYTES = 8 * 1024 * 1024


def extract(
    payload: bytes,
    *,
    ocr_enabled: bool = True,  # noqa: ARG001
    max_ocr_pages: int = 20,  # noqa: ARG001
    ocr_timeout_seconds: float | None = None,  # noqa: ARG001
    max_pdf_pages: int | None = None,  # noqa: ARG001
    on_progress: Callable[[], None] | None = None,  # noqa: ARG001
) -> tuple[str, str]:
    """Extract text from a legacy ``.doc`` payload. Returns (text, "doc")."""
    return catdoc_text("catdoc", ["-d", "utf-8", "-w"], payload, suffix=".doc", module="doc"), "doc"


def catdoc_text(tool: str, options: list[str], payload: bytes, *, suffix: str, module: str) -> str:
    """Run one of catdoc's tools on ``payload`` and return its text."""
    binary = shutil.which(tool)
    if binary is None:
        raise ToolNotFoundError
    output = run_tool(
        [binary, *options],
        payload,
        timeout_seconds=TOOL_TIMEOUT_SECONDS,
        max_output_bytes=_MAX_OUTPUT_BYTES,
        suffix=suffix,
    )
    if output.truncated:
        warn_extractor_cap(
            log,
            f"{module}_output_bytes",
            "%s output cut at %d bytes",
            tool,
            _MAX_OUTPUT_BYTES,
        )
    # A cut can split a UTF-8 sequence; catdoc writes valid UTF-8 otherwise.
    return output.data.decode("utf-8", errors="replace")
