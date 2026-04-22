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

from .database import EMBEDDING_DIM, Database
from .embedder import Embedder
from .parser import parse_email
from .reconciler import Reconciler, ReconcilerConfig, load_config_from_env, sweep_paths
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
        # Two distinct move scenarios land here:
        # 1. Flag changes: mbsync renames files in-place within the same
        #    directory when flags change (e.g. ``msg:2,S`` → ``msg:2,SR``
        #    when the message is replied to). The source path is already
        #    in ``indexed_files``. Re-parsing and re-embedding would waste
        #    an Ollama round-trip and leave stale rows behind — the
        #    reconciler (or fall-through ``update_filepath``) just has to
        #    move the stored filepath to the new name.
        # 2. Maildir delivery: a message is first written under ``tmp/``
        #    and renamed into ``new/`` or ``cur/``. The source path is not
        #    in ``indexed_files`` (it was a temp file), so this is a new
        #    message that must be indexed (``on_created`` does not fire
        #    for rename destinations).
        if event.is_directory:
            return

        src_path = str(event.src_path)
        dest_path_obj = Path(event.dest_path)
        dest_path = str(dest_path_obj)

        if self.db.is_indexed(src_path):
            # Case 1: rename of an existing indexed message.
            if self.reconciler is not None:
                try:
                    self.reconciler.handle_moved(src_path, dest_path)
                except Exception as e:
                    log.error(f"reconciler on_moved failed: {e}")
            else:
                # Default deployment has no reconciler; still move the
                # indexed_files / message_thread_map filepath forward so
                # future lookups find the current on-disk name.
                try:
                    self.db.update_filepath(src_path, dest_path)
                except Exception as e:
                    log.error(f"update_filepath failed on rename: {e}")
            return

        # Case 2: new delivery.
        if dest_path_obj.parent.name in ("cur", "new") and not self.db.is_indexed(dest_path):
            self._index_file(dest_path_obj)

    def _index_file(self, path: Path):
        try:
            message = parse_email(path, maildir_root=MAILDIR_PATH)
            if not message:
                return
            thread = self.threader.assign_thread(message)
            # Embed the merged accumulated body so the vector reflects the
            # whole thread rather than only the newly-arrived message.
            # thread.text_for_embedding() would only see thread.messages,
            # which holds just the new message for existing threads.
            body = self.db.build_merged_body(thread)
            embedding = self.embedder.embed(body)
            self.db.upsert_thread(thread, embedding, body=body)
            log.info(f"Indexed: {message.subject[:60]}")
        except Exception as e:
            log.error(f"Failed to index {path}: {e}")


HEALTH_REFRESH_EVERY = 25


def _iter_maildir_messages(root: Path):
    """Yield every message file under ``root`` whose parent is ``cur`` or
    ``new``, at any nesting depth. mbsync ``SubFolders Verbatim`` can
    produce ``Clients/ABC/cur/msg`` — a flat ``iterdir`` over ``root``
    would miss every nested folder's mail."""
    for filepath in root.rglob("*"):
        if filepath.is_file() and filepath.parent.name in ("cur", "new"):
            yield filepath


def initial_index(db: Database, embedder: Embedder, threader: Threader):
    """Index all existing emails on startup.

    Refreshes the health file every ``HEALTH_REFRESH_EVERY`` processed
    messages so that long initial indexes (large mailboxes, slow Ollama
    embeddings) do not exceed ``HEALTH_MAX_AGE_SECONDS`` in the healthcheck
    and cause the container to be reported unhealthy mid-scan.
    """
    log.info("Running initial index scan...")
    count = 0
    for filepath in _iter_maildir_messages(MAILDIR_PATH):
        if db.is_indexed(str(filepath)):
            continue
        try:
            message = parse_email(filepath, maildir_root=MAILDIR_PATH)
            if not message:
                continue
            thread = threader.assign_thread(message)
            body = db.build_merged_body(thread)
            embedding = embedder.embed(body)
            db.upsert_thread(thread, embedding, body=body)
            count += 1
            if count % HEALTH_REFRESH_EVERY == 0:
                touch_health_file()
        except Exception as e:
            log.error(f"Failed to index {filepath}: {e}")
    log.info(f"Initial index complete: {count} messages processed.")


def _validate_embedding_dim(embedder: Embedder) -> None:
    """Probe the running embedding model once at startup and verify its
    output dimension matches the schema-reserved ``EMBEDDING_DIM``.

    Switching ``OLLAMA_EMBED_MODEL`` to a model with a different output
    dimension (e.g. ``mxbai-embed-large`` at 1024) would otherwise fail
    on the first ``upsert_thread`` with a cryptic sqlite-vec error. Fail
    fast at startup with a clear, actionable message instead.
    """
    probe = embedder.embed("dimension probe")
    if len(probe) != EMBEDDING_DIM:
        raise SystemExit(
            f"OLLAMA_EMBED_MODEL={EMBED_MODEL!r} produces {len(probe)}-dim "
            f"embeddings, but the SQLite schema reserves {EMBEDDING_DIM}-dim "
            f"(threads_vec FLOAT[{EMBEDDING_DIM}]). Either switch to a model "
            f"that outputs {EMBEDDING_DIM}-dim vectors (e.g. nomic-embed-text), "
            f"or migrate the schema."
        )


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
        reconciler = Reconciler(
            db, embedder, threader, reconciler_config, maildir_root=MAILDIR_PATH
        )

    # Wait for Ollama to be ready
    embedder.wait_for_ready()

    # Verify the running model matches the schema's reserved vector dim.
    _validate_embedding_dim(embedder)

    # Index existing emails
    initial_index(db, embedder, threader)
    touch_health_file()

    # Always-on startup rename sweep. mbsync renames files in place for
    # any flag change (e.g. seen ``S`` → seen+replied ``SR``) and when
    # promoting from ``new/`` to ``cur/``. Events that land while the
    # indexer is offline would otherwise leave stale filepaths in
    # ``indexed_files``, which makes later lookups on the renamed file
    # miss. sweep_paths() only updates filepath rows; it does not
    # tombstone missing files, so running it unconditionally preserves
    # the opt-in posture of deletion reconciliation.
    try:
        sweep_paths(db)
    except Exception as e:
        log.error(f"startup rename sweep failed: {e}")

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
