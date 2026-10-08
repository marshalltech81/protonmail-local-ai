"""Run an OOXML extractor (DOCX, PPTX, XLSX) in a child process (#1040).

Every budget these extractors apply before their library opens a
package (the DOCX and PPTX pre-open budgets, the XLSX eager-part
budget) trusts the member sizes the ZIP central directory declares. On
Python 3.14 a member read through ``zipfile`` decompresses the
member's whole compressed stream (up to 2 GiB per call) before it cuts
the result to the declared size, so a member that understates its size
passes every budget and is still expanded in memory. A read through
the public API never sees the understatement, and the sender also
controls the CRC, so it cannot be caught in-process.

The whole extraction therefore runs in a child Python process
(``ooxml_child.py``), started through ``_runner.run_tool``: the
launcher lowers the child's address space (``RLIMIT_AS``) and CPU time
(``RLIMIT_CPU``) before it starts, and the runner adds a wall-clock
timeout. The pre-open budgets, the eager member reads and the walk all
run in the child, under the limits each format passes
(``CHILD_MAX_ADDRESS_SPACE_BYTES``, ``CHILD_MAX_CPU_SECONDS`` and
``CHILD_TIMEOUT_SECONDS`` in ``docx.py``, ``pptx.py`` and ``xlsx.py``,
sized from plain measurements in the indexer image). Starting the
child and importing its library costs about 0.2 s per extraction in
the image, which keeps no compiled bytecode.

The child writes one header line, then the text (``ooxml_child``):

* a comma-separated list of the walk budgets that cut the text, each
  checked here against the format's own list and logged through
  ``warn_extractor_cap`` by the caller; or
* ``!`` and the type name of the exception the extraction raised, with
  no text. A type name in the format's ``permanent`` map is raised here
  as that class, so the dispatcher records it ``unsupported`` as before
  (#931, #1032); any other is raised as ``OoxmlChildError``, which the
  dispatcher records ``failed`` under that type name, as it recorded
  the exception in-process.

Output that is not in this format (no header line, an unknown cap or
type name, text after a type name, bytes that are not UTF-8) or that
the runner cut at its byte cap is rejected as ``OoxmlOutputError``,
never returned as text. A timeout, a death by signal (a crash, or the
CPU limit) and a non-zero exit raise the runner's fixed-text errors.
All of these are recorded ``failed`` by type name, with a rate-limited
WARNING from the dispatcher. A ``MemoryError`` or ``RecursionError``
in the child is the child's limit, not host pressure: it is reported
by type and recorded ``failed``, where in-process the dispatcher
re-raised it. The address-space limit can also surface as a parser's
own error type: lxml reports a failed allocation as ``XMLSyntaxError``.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from pathlib import Path

from ._runner import run_tool

_CHILD = Path(__file__).with_name("ooxml_child.py")

# Bytes of the child's output read. Each format's text budget is
# 10,000,000 characters at up to four bytes each. The XLSX walk charges
# its separators to that budget; the DOCX and PPTX walks do not charge
# the blank lines between lines and the spaces between table cells,
# which their block, paragraph and cell budgets hold under 4 MB.
_MAX_OUTPUT_BYTES = 48 * 1024 * 1024

# What a reported type name must look like: a Python identifier.
_TYPE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")


class OoxmlOutputError(Exception):
    """The child's output was cut at the byte cap or not in its format."""

    def __init__(self) -> None:
        super().__init__("ooxml child output malformed or over its cap")


class OoxmlChildError(Exception):
    """The extraction raised in the child. ``type_name`` is the
    exception's type name, which the dispatcher records; the message is
    fixed, as the exception's own could quote the document."""

    def __init__(self, type_name: str) -> None:
        super().__init__("ooxml extraction raised in the child process")
        self.type_name = type_name


def run_child(
    module: str,
    payload: bytes,
    *,
    max_address_space_bytes: int,
    max_cpu_seconds: int,
    timeout_seconds: float,
    caps: frozenset[str],
    permanent: Mapping[str, type[Exception]],
) -> tuple[str, list[str]]:
    """Run ``module``'s extraction of ``payload`` in the child under the
    format's limits; the wall-clock timeout is past the CPU limit, so a
    CPU-bound child meets that first. Returns the text and the names of
    the budgets that cut it (each in ``caps``)."""
    output = run_tool(
        [sys.executable, "-I", str(_CHILD), module],
        payload,
        timeout_seconds=timeout_seconds,
        max_output_bytes=_MAX_OUTPUT_BYTES,
        max_address_space_bytes=max_address_space_bytes,
        max_cpu_seconds=max_cpu_seconds,
        suffix=f".{module}",
    )
    if output.truncated:
        raise OoxmlOutputError
    header, newline, body = output.data.partition(b"\n")
    if not newline:
        raise OoxmlOutputError
    try:
        line = header.decode("ascii")
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise OoxmlOutputError from None
    if line.startswith("!"):
        type_name = line[1:]
        if text or not _TYPE_NAME.fullmatch(type_name):
            raise OoxmlOutputError
        error = permanent.get(type_name)
        if error is not None:
            raise error()
        raise OoxmlChildError(type_name)
    reported = line.split(",") if line else []
    if not set(reported) <= caps:
        raise OoxmlOutputError
    return text, list(dict.fromkeys(reported))
