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

The whole extraction therefore runs in the extractor child
(``extractor_child.py``), started through ``_runner.run_child``: the
launcher lowers the child's address space (``RLIMIT_AS``) and CPU time
(``RLIMIT_CPU``) before it starts, and the runner adds a wall-clock
timeout, kills the child's process group when the run ends and removes
its scratch directory (#1291). The pre-open budgets, the eager member
reads and the walk all run in the child, under the limits each format
passes (``CHILD_MAX_ADDRESS_SPACE_BYTES``, ``CHILD_MAX_CPU_SECONDS``
and ``CHILD_TIMEOUT_SECONDS`` in ``docx.py``, ``pptx.py`` and
``xlsx.py``, sized from plain measurements in the indexer image).
Starting the child and importing its library costs about 0.08 s per
extraction in the image, which ships compiled bytecode for the
standard library, the dependencies and ``src`` (#1230; about 0.2 s
without it).

The child's result crosses the pipe in the runner's framed protocol
(``_runner``): the names of the walk budgets that cut the text, each
checked against the format's own list and logged through
``warn_extractor_cap`` by the caller, and the text; or the type name of
the exception the extraction raised. A type name in the format's
``permanent`` map is raised as that class, so the dispatcher records it
``unsupported`` as before (#931, #1032); any other is raised as
``ChildError``, which the dispatcher records ``failed`` under that type
name, as it recorded the exception in-process.

Output that breaks the protocol or that the runner cut at its byte cap
is rejected as ``ChildOutputError``, never returned as text. A timeout,
a death by signal (a crash, or the CPU limit) and a non-zero exit raise
the runner's fixed-text errors. All of these are recorded ``failed`` by
type name, with a rate-limited WARNING from the dispatcher. A
``MemoryError`` or ``RecursionError`` in the child is the child's
limit, not host pressure: it is reported by type and recorded
``failed``, where in-process the dispatcher re-raised it. The
address-space limit can also surface as a parser's own error type:
lxml reports a failed allocation as ``XMLSyntaxError``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from . import _runner

# Bytes of the child's output read: each format's text budget
# (10,000,000 characters) at UTF-8's worst case of four bytes each,
# plus the frames. The XLSX walk charges its separators to that budget;
# the DOCX and PPTX walks do not charge the blank lines between lines
# and the spaces between table cells, which their block, paragraph and
# cell budgets hold under 4 MB. Output past it cannot come from a
# working child.
_MAX_OUTPUT_BYTES = 48 * 1024 * 1024


def run_child(
    module: str,
    payload: bytes,
    *,
    max_address_space_bytes: int,
    max_cpu_seconds: int,
    timeout_seconds: float,
    caps: frozenset[str],
    permanent: Mapping[str, type[Exception]],
    on_progress: Callable[[], None] | None = None,
) -> tuple[str, list[str]]:
    """Run ``module``'s extraction of ``payload`` in the child under the
    format's limits. Returns the text and the names of the budgets that
    cut it (each in ``caps``)."""
    result = _runner.run_child(
        module,
        payload,
        max_address_space_bytes=max_address_space_bytes,
        max_cpu_seconds=max_cpu_seconds,
        timeout_seconds=timeout_seconds,
        max_output_bytes=_MAX_OUTPUT_BYTES,
        caps=caps,
        permanent=permanent,
        on_progress=on_progress,
    )
    return result.text, result.caps
