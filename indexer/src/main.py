"""
Indexer entry point.
Watches the Maildir for new/changed emails, parses and threads them,
generates embeddings via Ollama, and writes to the SQLite index.

When ``INDEXER_DELETION_ENABLED=true`` the indexer also runs a reconciler
that records tombstones for mbsync-flagged (``T``) Maildir files and reaps
them after a grace window. See ``src/reconciler.py``.
"""

import logging
import os
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .database import Database
from .embedder import Embedder
from .parser import parse_email
from .reconciler import Reconciler, ReconcilerConfig, load_config_from_env
from .threader import Threader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("indexer")

MAILDIR_PATH = Path(os.environ.get("MAILDIR_PATH", "/maildir"))
SQLITE_PATH = Path(os.environ.get("SQLITE_PATH", "/data/mail.db"))
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://ollama:11434")
EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
# /tmp default is safe: container tmpfs, non-root user, overridable via env.
INDEXER_HEALTH_FILE = Path(os.environ.get("INDEXER_HEALTH_FILE", "/tmp/indexer-health"))  # nosec B108


def touch_health_file() -> None:
    INDEXER_HEALTH_FILE.touch(exist_ok=True)


class MaildirHandler(FileSystemEventHandler):
    """Watches Maildir for new email files and triggers indexing."""

    def __init__(
        self,
        db: Database,
        embedder: Embedder,
        threader: Threader,
        reconciler: Reconciler | None = None,
    ):
        self.db = db
        self.embedder = embedder
        self.threader = threader
        self.reconciler = reconciler

    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        # Only process files in cur/ or new/ subdirectories
        if path.parent.name in ("cur", "new"):
            self._index_file(path)

    def on_moved(self, event):
        # Maildir flag changes are renames within the same directory. The
        # reconciler inspects the destination to either record or clear a
        # tombstone based on the new flag set. Only runs when deletion
        # reconciliation is enabled.
        if event.is_directory or self.reconciler is None:
            return
        try:
            self.reconciler.handle_moved(str(event.src_path), str(event.dest_path))
        except Exception as e:
            log.error(f"reconciler on_moved failed: {e}")

    def _index_file(self, path: Path):
        try:
            message = parse_email(path)
            if not message:
                return
            thread = self.threader.assign_thread(message)
            embedding = self.embedder.embed(thread.text_for_embedding())
            self.db.upsert_thread(thread, embedding)
            log.info(f"Indexed: {message.subject[:60]}")
        except Exception as e:
            log.error(f"Failed to index {path}: {e}")


def initial_index(db: Database, embedder: Embedder, threader: Threader):
    """Index all existing emails on startup."""
    log.info("Running initial index scan...")
    count = 0
    for folder in MAILDIR_PATH.iterdir():
        for subdir in ("cur", "new"):
            subpath = folder / subdir
            if not subpath.exists():
                continue
            for filepath in subpath.iterdir():
                if filepath.is_file() and not db.is_indexed(str(filepath)):
                    try:
                        message = parse_email(filepath)
                        if not message:
                            continue
                        thread = threader.assign_thread(message)
                        embedding = embedder.embed(thread.text_for_embedding())
                        db.upsert_thread(thread, embedding)
                        count += 1
                    except Exception as e:
                        log.error(f"Failed to index {filepath}: {e}")
    log.info(f"Initial index complete: {count} messages processed.")


def _log_reconciler_config(cfg: ReconcilerConfig) -> None:
    if not cfg.enabled:
        log.info("Deletion reconciliation: disabled (set INDEXER_DELETION_ENABLED=true to enable)")
        return
    log.info(
        "Deletion reconciliation: enabled "
        "(grace=%dd, sweep=%ds, max_batch=%.1f%%, force=%s, unlink=%s)",
        cfg.grace_days,
        cfg.sweep_interval_secs,
        cfg.max_batch_pct * 100,
        cfg.force,
        cfg.unlink_on_reap,
    )


def main():
    log.info("Starting indexer...")
    log.info(f"  Maildir: {MAILDIR_PATH}")
    log.info(f"  SQLite:  {SQLITE_PATH}")
    log.info(f"  Ollama:  {OLLAMA_HOST} ({EMBED_MODEL})")

    db = Database(SQLITE_PATH)
    embedder = Embedder(OLLAMA_HOST, EMBED_MODEL)
    threader = Threader(db)
    touch_health_file()

    reconciler_config = load_config_from_env(os.environ)
    _log_reconciler_config(reconciler_config)
    reconciler: Reconciler | None = None
    if reconciler_config.enabled:
        reconciler = Reconciler(db, embedder, threader, reconciler_config)

    # Wait for Ollama to be ready
    embedder.wait_for_ready()

    # Index existing emails
    initial_index(db, embedder, threader)
    touch_health_file()

    # Startup reconciliation sweep — detect tombstones and path renames that
    # landed while the indexer was offline. Safe to run every startup: it only
    # writes to pending_deletions and updates stored filepaths.
    if reconciler is not None:
        try:
            reconciler.sweep()
            reconciler.reap()
        except Exception as e:
            log.error(f"startup reconciliation failed: {e}")

    # Watch for new emails
    handler = MaildirHandler(db, embedder, threader, reconciler=reconciler)
    observer = Observer()
    observer.schedule(handler, str(MAILDIR_PATH), recursive=True)
    observer.start()
    log.info("Watching Maildir for new emails...")

    last_reconcile = time.monotonic()
    try:
        while True:
            touch_health_file()
            if reconciler is not None:
                now = time.monotonic()
                if now - last_reconcile >= reconciler_config.sweep_interval_secs:
                    try:
                        reconciler.sweep()
                        reconciler.reap()
                    except Exception as e:
                        log.error(f"periodic reconciliation failed: {e}")
                    last_reconcile = now
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
