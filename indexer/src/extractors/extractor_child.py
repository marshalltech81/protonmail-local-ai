"""The extractor child process (#1040, #1291).

Run by ``_runner.run_child`` as
``python -I extractor_child.py <module> [<option> ...] <payload file>``,
through the runner's launcher (``_launcher.py``), which has already
lowered the address-space and CPU limits the extractor passes. ``-I``
keeps this directory off ``sys.path``, so ``main`` adds the indexer's
root (the directory holding the ``src`` package) and imports the module
that does the extraction from the package, as the indexer does; the
package import also runs ``defusedxml.defuse_stdlib()`` and sets PIL's
pixel cap, as in the indexer.

The module's ``extract_text`` does the whole extraction (for the OOXML
formats: the pre-open budgets, the library's open and the budgeted
walk; for ``xls``: xlrd's open and the walk in ``xls_child``; for
``image``: the decode and Tesseract in ``image_child``, which takes the
page cap and OCR timeout as its options, #1292; for ``container``: the
directory read in ``container_child``, which returns a fixed token
instead of text, #1416). It returns the text
and the names of the budgets that cut it.

Output on stdout is the runner's framed protocol (``_runner``): for a
module in ``REPORTS_PROGRESS``, a ``P`` line written and flushed as each
page is read; then ``N launches <count>``, the processes this child
started (Tesseract per image frame, counted from the same audit event
as in the indexer, #1236); then a ``C <name>`` line per budget, an
``N <key> <count>`` line per degradation the extraction recorded
(#1314), then ``T <length>`` and the text as UTF-8; or ``E <type name>``
after the launch count when the extraction raised. Only the type name: an exception's message can quote the
document. A ``MemoryError`` or ``RecursionError`` is reported the same
way: here it is the child's own limit.

The degradation an extraction records (``note_text_lost``,
``record_ocr_pages_skipped`` and the counters
``drain_extractor_counts`` reports, each through the package's
``note_*`` and ``warn_extractor_cap`` helpers) lives in this process's
memory, and the lines those helpers log go to the discarded stderr.
``run`` therefore clears the attempt's state first, as the dispatcher
does, and after the extraction sends what was recorded as ``N`` frames
under the package's fixed keys (``CHILD_DEGRADATION_KEYS``); the parent
re-applies them and logs one line for them
(``apply_child_degradation``). No log record, format string or
argument crosses.

Nothing is written to stderr on purpose (the runner discards it). An
error outside the extraction (the payload file cannot be read, the
output cannot be written) ends the child with a non-zero status and no
result frame, which the parent records as ``failed``.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

# The module names this child runs, each mapped to the module of the
# extractors package whose ``extract_text`` does the work.
MODULES = {
    "docx": "docx",
    "pptx": "pptx",
    "xlsx": "xlsx",
    "xls": "xls_child",
    "eml": "eml",
    "image": "image_child",
    "container": "container_child",
}

# The modules whose ``extract_text`` takes an ``on_progress`` callback.
REPORTS_PROGRESS = frozenset({"image"})

# The progress frame.
PROGRESS_FRAME = b"P\n"

# The package this file belongs to (``src.extractors``), and the
# directory that holds it.
_HERE = Path(__file__).resolve().parent
_PACKAGE = f"{_HERE.parent.name}.{_HERE.name}"
_ROOT = _HERE.parent.parent

# Exit status for an unknown module.
_EXIT_USAGE = 2


def run(
    module: str,
    payload: bytes,
    options: Sequence[str] = (),
    on_progress: Callable[[], None] | None = None,
) -> bytes:
    """The child's result frames for ``module``'s extraction of
    ``payload`` with ``options``. A module in ``REPORTS_PROGRESS`` calls
    ``on_progress`` per page, which writes the progress frames."""
    kwargs = {"on_progress": on_progress} if module in REPORTS_PROGRESS else {}
    package = importlib.import_module(_PACKAGE)
    package.reset_attempt()
    try:
        extractor = importlib.import_module(f"{_PACKAGE}.{MODULES[module]}")
        text, caps = extractor.extract_text(payload, *options, **kwargs)
    except Exception as exc:  # noqa: BLE001 — reported by type name only
        return error_frame(type(exc).__name__)
    return result_frames(text, caps, package.child_degradation())


def launches_frame(launches: int) -> bytes:
    """The count frame for the processes this child started (#1236)."""
    return f"N launches {launches}\n".encode("ascii")


def error_frame(type_name: str) -> bytes:
    """The frame for an extraction that raised ``type_name``."""
    return f"E {type_name}\n".encode("ascii", errors="replace")


def result_frames(text: str, caps: list[str], counts: dict[str, int] | None = None) -> bytes:
    """A frame per cap name and per count, then the text frame. A lone
    surrogate, which UTF-8 cannot hold, is written as ``?``."""
    body = text.encode("utf-8", errors="replace")
    head = (
        "".join(f"C {cap}\n" for cap in caps)
        + "".join(f"N {key} {n}\n" for key, n in (counts or {}).items())
        + f"T {len(body)}\n"
    )
    return head.encode("ascii") + body


def _write_progress() -> None:  # pragma: no cover — runs only in the child
    """Send a progress frame now, so the parent sees it while the child
    runs."""
    sys.stdout.buffer.write(PROGRESS_FRAME)
    sys.stdout.buffer.flush()


def main(argv: list[str]) -> int:  # pragma: no cover — runs only in the child
    if len(argv) < 3 or argv[1] not in MODULES:
        return _EXIT_USAGE
    sys.path.insert(0, str(_ROOT))
    with open(argv[-1], "rb") as handle:
        payload = handle.read()
    # Imported first, so its audit hook counts every process run starts.
    runner = importlib.import_module(f"{_PACKAGE}._runner")
    before = runner.process_launches()
    result = run(argv[1], payload, argv[2:-1], _write_progress)
    launches = runner.process_launches() - before
    sys.stdout.buffer.write(launches_frame(launches) + result)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover — runs only in the child
    sys.exit(main(sys.argv))
