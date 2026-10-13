"""One deletion-reconciliation pass past the mass-delete brake.

The one-run ``make force-deletion`` command (#1609), which replaced the
persistent ``INDEXER_DELETION_FORCE`` setting. The Makefile stops the
indexer, so this process is the index's only writer, runs this module in
a one-shot container, then starts the indexer again:

    docker compose run --rm --no-deps indexer python -m src.force_deletion

It runs the indexer's own pass (``main._run_periodic_reconcile``: sweep,
reap, the ``maintenance pass=reconcile`` line, ending ``brake=forced``)
once, with the configured grace window and ``force`` set in memory for
this process only, then the WAL maintenance that scrubs what the reap
deleted. Nothing is stored: the restarted indexer, like every later pass,
has the brake on. Each thread's reap is its own transaction, so a pass
that stops midway leaves the rest tombstoned for the brake or another
run.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import sys

from . import main as indexer
from .account import account_config_from_env
from .reconciler import Reconciler, load_config_from_env

log = logging.getLogger("indexer.force_deletion")


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(
        prog="force-deletion",
        description="Run one deletion-reconciliation pass with the mass-delete brake off.",
    ).parse_args(argv)

    # The same startup checks as the indexer, before the index opens.
    embed_base_url = indexer._validate_embed_config()
    account = account_config_from_env(os.environ, maildir_root=indexer.MAILDIR_PATH)
    config = load_config_from_env(os.environ)
    if not config.enabled:
        log.error(
            "forced deletion refused: deletion reconciliation is off (archive mode, "
            "INDEXER_DELETION_ENABLED=false), so there is nothing to reap"
        )
        return 1
    if not indexer.SQLITE_PATH.exists():
        log.error("forced deletion refused: No index database at %s", indexer.SQLITE_PATH)
        return 1

    db = indexer._open_database(account)
    try:
        embedder = indexer._build_embedder(embed_base_url)
        # A reap may embed a rebuilt thread's subject, so the embedder
        # must answer and be the one the index records.
        embedder.wait_for_ready()
        indexer._check_embedder_identity(db, embedder)
        reconciler = Reconciler(
            db,
            embedder,
            dataclasses.replace(config, force=True),
            maildir_root=indexer.MAILDIR_PATH,
        )
        log.warning(
            "forced deletion: one reconciliation pass with the mass-delete brake off "
            "(grace=%dd, max_batch=%.1f%%); the indexer's own passes keep the brake",
            config.grace_days,
            config.max_batch_pct * 100,
        )
        ok = indexer._run_periodic_reconcile(reconciler, db)
        # Scrubs what the reap deleted (#670).
        indexer._run_wal_maintenance(db)
    finally:
        db.close()
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover - run by make force-deletion
    sys.exit(main())
