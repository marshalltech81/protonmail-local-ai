"""Legacy Word ``.doc`` extractor (#935).

A legacy ``.doc`` is an OLE2 compound file, which python-docx cannot
read. The dispatcher routes an OLE2 payload labelled ``.doc`` /
``application/msword`` here; a ``.doc``-labelled payload that is not
OLE2 (an OOXML file mislabelled as ``.doc``) still goes to ``docx``.

The text comes from ``catdoc`` (Debian's ``catdoc`` package), run by
``_runner.run_tool``: no shell, address-space and CPU limits set before
catdoc starts (#995), a wall-clock timeout, its output read up to four
bytes per character of the dispatcher's ``max_extracted_chars``, never
past ``_MAX_OUTPUT_BYTES`` (``_runner.raw_output_cap``, #1308), and its
stderr discarded. ``-d utf-8`` fixes
the output charset whatever the locale, and ``-w`` turns off catdoc's
line wrapping so a paragraph stays one line for the chunker. Output past
the byte cap is not indexed: the text before it is kept and the cap is
reported through ``warn_extractor_cap``, with the bound that cut as a
fixed token (``bound=chars`` for the configured character cap,
``bound=ceiling`` for ``_MAX_OUTPUT_BYTES``); a cut at the character
cap's bound is recorded on the result, so raising the setting
re-extracts it (#1418).
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable

from . import CAP_EXTRACTED_CHARS, record_cap_cut, warn_extractor_cap
from ._runner import (
    BOUND_CHARS,
    ToolNotFoundError,
    raw_output_bound,
    raw_output_cap,
    run_tool,
)

log = logging.getLogger("indexer.extractor.doc")

# Address space (``RLIMIT_AS``) and CPU seconds (``RLIMIT_CPU``) catdoc
# may use (#995). Plainly measured in the indexer image (catdoc 0.95,
# ``-d utf-8 -w``): the 9 KB fixture and LibreOffice-written documents
# of 8.8 MB and 21.9 MB (4 and 10 MB of text) take 0.002, 0.05 and
# 0.11 s of CPU, and run to completion under 4 to 5 MiB of address
# space (below about 3 MiB catdoc cannot map libc and exits 127).
# catdoc streams the document rather than holding it, so 64 MiB is over
# ten times the largest need, and the CPU limit about 90 times the
# largest time. Past either limit catdoc exits with an error or is
# killed: a failed row.
CHILD_MAX_ADDRESS_SPACE_BYTES = 64 * 1024 * 1024
CHILD_MAX_CPU_SECONDS = 10

# Wall-clock seconds a catdoc run may take, past its CPU limit so a
# CPU-bound run meets that limit first. catdoc reads a
# document in a single pass; the generated fixtures take a few
# milliseconds, so this is reached only by a tool that hangs.
TOOL_TIMEOUT_SECONDS = 60.0

# The most bytes of a tool's output read, whatever the dispatcher's
# ``max_extracted_chars`` (``_runner.raw_output_cap``, #1308): the other
# extractors' 10,000,000-character text budget at four bytes a
# character. An extraction's peak in the indexer is about five bytes per
# output byte (measured there), so about 200 MiB at this ceiling.
_MAX_OUTPUT_BYTES = 40 * 1024 * 1024


def extract(
    payload: bytes,
    *,
    ocr_enabled: bool = True,  # noqa: ARG001
    max_ocr_pages: int = 20,  # noqa: ARG001
    ocr_timeout_seconds: float | None = None,  # noqa: ARG001
    max_pdf_pages: int | None = None,  # noqa: ARG001
    on_progress: Callable[[], None] | None = None,  # noqa: ARG001
    max_extracted_chars: int | None = None,
) -> tuple[str, str]:
    """Extract text from a legacy ``.doc`` payload. Returns (text, "doc")."""
    text = catdoc_text(
        "catdoc",
        ["-d", "utf-8", "-w"],
        payload,
        suffix=".doc",
        module="doc",
        max_extracted_chars=max_extracted_chars,
    )
    return text, "doc"


def catdoc_text(
    tool: str,
    options: list[str],
    payload: bytes,
    *,
    suffix: str,
    module: str,
    max_extracted_chars: int | None,
) -> str:
    """Run one of catdoc's tools on ``payload`` and return its text, read
    up to the output byte cap for ``max_extracted_chars``
    (``_runner.raw_output_cap``). A cut at the configured character cap's
    bound records that cap on the result (#1418); a cut at the ceiling
    records nothing."""
    max_output_bytes = raw_output_cap(max_extracted_chars, ceiling=_MAX_OUTPUT_BYTES)
    bound = raw_output_bound(max_extracted_chars, ceiling=_MAX_OUTPUT_BYTES)
    binary = shutil.which(tool)
    if binary is None:
        raise ToolNotFoundError
    output = run_tool(
        [binary, *options],
        payload,
        timeout_seconds=TOOL_TIMEOUT_SECONDS,
        max_output_bytes=max_output_bytes,
        max_address_space_bytes=CHILD_MAX_ADDRESS_SPACE_BYTES,
        max_cpu_seconds=CHILD_MAX_CPU_SECONDS,
        suffix=suffix,
    )
    if output.truncated:
        warn_extractor_cap(
            log,
            f"{module}_output_bytes",
            "%s output cut at %d bytes (bound=%s)",
            tool,
            max_output_bytes,
            bound,
        )
        if bound == BOUND_CHARS and max_extracted_chars is not None:
            record_cap_cut(CAP_EXTRACTED_CHARS, max_extracted_chars)
    # A cut can split a UTF-8 sequence; catdoc writes valid UTF-8 otherwise.
    return output.data.decode("utf-8", errors="replace")
