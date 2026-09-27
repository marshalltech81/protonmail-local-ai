"""Return dead-lettered indexing jobs to the queue.

Operator rescue path once the cause of a dead-letter is fixed (a
parser bug shipped a fix, an extraction dependency was upgraded, an
embedder config was corrected). Run inside the indexer container while
the indexer is up; its drain loop picks the requeued jobs up on the
next pass:

    make requeue-dead                 # every dead row
    make requeue-dead CLASS=retryable # only exhausted retries

Classes are recorded in ``indexing_jobs.last_error_class`` (see
``queue.ERROR_CLASSES``). Rows dead-lettered before that column existed
have no class and are requeued only without ``--class``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .database import Database
from .queue import ERROR_CLASSES, IndexingQueue


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="requeue-dead",
        description="Requeue dead-lettered indexing jobs with a fresh retry budget.",
    )
    parser.add_argument(
        "--class",
        dest="error_class",
        choices=ERROR_CLASSES,
        help="only requeue jobs whose last failure had this class",
    )
    args = parser.parse_args(argv)

    db_path = Path(os.environ.get("SQLITE_PATH", "/data/mail.db"))
    if not db_path.exists():
        print(f"No index database at {db_path}; nothing to requeue.", file=sys.stderr)
        return 1

    db = Database(db_path)
    try:
        requeued = IndexingQueue(db).requeue_dead(error_class=args.error_class)
    finally:
        db.close()
    scope = f" (class={args.error_class})" if args.error_class else ""
    print(f"Requeued {requeued} dead-lettered job(s){scope}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
