"""Test setup for the evidence-run code (#1759): the package, and the
indexer's ``src`` (the probe reuses its parser and policy), on the path."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEXER = ROOT.parent / "indexer"
for path in (str(ROOT), str(INDEXER)):
    if path not in sys.path:
        sys.path.insert(0, path)
