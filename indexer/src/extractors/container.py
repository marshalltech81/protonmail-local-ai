"""Container identification for OLE2 and ZIP payloads (#1416).

A payload that starts with the OLE2 signature or a ZIP signature is
identified by its directory before any extractor runs, whatever its
label (owner decision on #1416, 2026-10-10): the dispatcher calls
``identify``, which reads the container's directory in the extractor
child (``container_child``) through ``_runner.run_child``, under the
limits below, and returns one fixed token naming what the container
holds. The dispatcher maps it to one extractor, or to a fixed
``unsupported`` error; a limit hit, a timeout, a crash or an error in
the child is a ``failed`` row. Nothing is extracted to decide: the
extractor chosen runs once, afterwards.

The child costs a launcher and a child Python start-up, the extractors
package and olefile's import, counted against the per-message
extraction budget like any launch (#1236).
"""

from __future__ import annotations

from ._runner import run_child

# The tokens ``container_child`` returns.
DOC = "doc"
XLS = "xls"
PPT = "ppt"
DOCX = "docx"
XLSX = "xlsx"
PPTX = "pptx"
OOXML_UNKNOWN = "ooxml"
OLE2_OTHER = "ole2-other"
OLE2_ENCRYPTED = "ole2-encrypted"
AMBIGUOUS = "ambiguous"
ZIP_OTHER = "zip-other"
TOKENS = frozenset(
    {
        DOC,
        XLS,
        PPT,
        DOCX,
        XLSX,
        PPTX,
        OOXML_UNKNOWN,
        OLE2_OTHER,
        OLE2_ENCRYPTED,
        AMBIGUOUS,
        ZIP_OTHER,
    }
)

# The fixed 8-byte signature every OLE2 compound file starts with.
OLE2_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# Address space (``RLIMIT_AS``) and CPU seconds (``RLIMIT_CPU``) the
# child may use, and the wall-clock seconds it may run, past the CPU
# limit so a CPU-bound child meets that first. Plainly measured in the
# indexer image (CPython 3.14, olefile 0.47; peak RSS, CPU time, and the
# smallest address-space limit the child succeeds under):
#
# * a 12 KB OLE2 file with three streams: 31 MiB, 0.07 s, 48 MiB;
# * an OLE2 file padded to 32 MiB (the default payload cap), and a ZIP
#   with one 32 MiB stored member: 64 MiB, 0.06 s, 80 MiB;
# * an OLE2 directory of 49,951 entries (at the entry budget): 92 MiB,
#   0.22 s, 112 MiB; a ZIP of 50,000 members: 65 MiB, 0.12 s, 80 MiB;
# * crafted: a 250,000-entry directory, a looping directory chain and a
#   header declaring 2^31 FAT sectors are rejected by the guard in
#   0.06 s (the last ran until the CPU limit before the ``loadfat``
#   check); a 5,000-entry sibling chain raises ``RecursionError`` in
#   0.06 s; a central directory of 300,000 members (26 MB) is read in
#   0.54 s at 223 MiB before the entry guard rejects it, and one of the
#   whole 32 MiB payload (292 MiB unlimited) fails at the address-space
#   limit (``MemoryError``) in 0.6 s.
#
# 256 MiB is over twice the largest benign need at the default payload
# cap; 5 CPU seconds is over six times the slowest crafted read and 20
# times the slowest benign one.
CHILD_MAX_ADDRESS_SPACE_BYTES = 256 * 1024 * 1024
CHILD_MAX_CPU_SECONDS = 5
CONTAINER_TIMEOUT_SECONDS = 10.0

# The child writes one short token and its frames.
_MAX_OUTPUT_BYTES = 1024


class ContainerTokenError(Exception):
    """The child returned text that is not one of ``TOKENS``."""

    def __init__(self) -> None:
        super().__init__("container identification returned an unknown token")


def identify(payload: bytes) -> str:
    """The token naming what the OLE2 or ZIP ``payload`` holds (one of
    ``TOKENS``). Raises the runner's fixed-text errors, ``ChildError``
    with the child's exception type name, or ``ContainerTokenError``."""
    result = run_child(
        "container",
        payload,
        max_address_space_bytes=CHILD_MAX_ADDRESS_SPACE_BYTES,
        max_cpu_seconds=CHILD_MAX_CPU_SECONDS,
        timeout_seconds=CONTAINER_TIMEOUT_SECONDS,
        max_output_bytes=_MAX_OUTPUT_BYTES,
        caps=frozenset(),
    )
    if result.text not in TOKENS:
        raise ContainerTokenError
    return result.text
