"""Child process of the OOXML extractors (#1040).

Run by ``ooxml.run_child`` as
``python -I ooxml_child.py <docx|pptx|xlsx> <payload file>``, through
the runner's launcher (``_launcher.py``), which has already lowered the
address-space and CPU limits the format passes. ``-I`` keeps this
directory off ``sys.path``, so ``main`` adds the indexer's root (the
directory holding the ``src`` package) and imports the format's module
from the package, as the indexer does; the package import also runs
``defusedxml.defuse_stdlib()`` and sets PIL's pixel cap, as in the
indexer.

The module's ``extract_text`` does the whole extraction: its pre-open
budgets, its library's open, and the budgeted walk. It returns the text
and the names of the budgets that cut it.

Output on stdout, one header line and then the text as UTF-8:

* the comma-separated cap names, then the text; or
* ``!`` and the type name of the exception the extraction raised, and
  nothing after it. Only the type name: an exception's message can
  quote the document.

Nothing is written to stderr on purpose (the runner discards it). An
error outside the extraction (the payload file cannot be read, the
output cannot be written) ends the child with a non-zero status and no
header, which the parent records as ``failed``.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

# The formats this child runs, each a module of the extractors package.
MODULES = frozenset({"docx", "pptx", "xlsx"})

# The package this file belongs to (``src.extractors``), and the
# directory that holds it.
_HERE = Path(__file__).resolve().parent
_PACKAGE = f"{_HERE.parent.name}.{_HERE.name}"
_ROOT = _HERE.parent.parent

# Exit status for an unknown format.
_EXIT_USAGE = 2


def run(module: str, payload: bytes) -> bytes:
    """The child's stdout for ``module``'s extraction of ``payload``."""
    try:
        extractor = importlib.import_module(f"{_PACKAGE}.{module}")
        text, caps = extractor.extract_text(payload)
    except Exception as exc:  # noqa: BLE001 — reported by type name only
        return f"!{type(exc).__name__}\n".encode("ascii", errors="replace")
    return encode_output(text, caps)


def encode_output(text: str, caps: list[str]) -> bytes:
    """The cap names on one line, then the text."""
    return (",".join(caps) + "\n").encode("ascii") + text.encode("utf-8", errors="replace")


def main(argv: list[str]) -> int:  # pragma: no cover — runs only in the child
    if len(argv) != 3 or argv[1] not in MODULES:
        return _EXIT_USAGE
    sys.path.insert(0, str(_ROOT))
    with open(argv[2], "rb") as handle:
        payload = handle.read()
    sys.stdout.buffer.write(run(argv[1], payload))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover — runs only in the child
    sys.exit(main(sys.argv))
