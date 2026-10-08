"""Queue every indexed message for an in-place reparse (#1078).

A migration that adds parser-produced per-message data queues the
reparse itself (``queue.REPARSE_ENQUEUE_SQL``); this is the same
statement by hand, for recovery (for example after a reparse's
dead-lettered jobs were cleared another way). Run inside the indexer
container while the indexer is up; its drain loop re-parses the jobs on
the next pass, without embedding calls:

    make reparse

Files that already have a job keep it: a pending or retrying one is
parsed in full by its own run, and a dead-lettered one stays dead until
``make requeue-dead``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .database import Database
from .queue import IndexingQueue


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reparse",
        description="Queue every indexed message to be parsed again in place.",
    )
    parser.parse_args(argv)

    db_path = Path(os.environ.get("SQLITE_PATH", "/data/mail.db"))
    if not db_path.exists():
        print(f"No index database at {db_path}; nothing to reparse.", file=sys.stderr)
        return 1

    db = Database(db_path)
    try:
        queued = IndexingQueue(db).enqueue_reparse()
    finally:
        db.close()
    print(
        f"Queued {queued} indexed message(s) for reparse. Messages that already "
        "had a job keep it; dead-lettered ones stay dead until make requeue-dead."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
