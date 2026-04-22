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
from .queue import (
    REASON_INITIAL_SCAN,
    REASON_ON_CREATED,
    REASON_ON_MOVED,
    IndexingQueue,
)
from .queue import load_config_from_env as load_queue_config_from_env
from .quoting import strip_for_embedding
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
    """Watches Maildir for new email files and enqueues them for indexing.

    The callback path only enqueues — the actual parse / embed / upsert
    pipeline runs in the main loop via ``drain_queue``. Enqueue is a
    single SQLite write, so the watchdog's internal thread no longer
    blocks on a slow Ollama round-trip and a Watchdog event storm
    cannot overflow whatever buffer ``watchdog`` uses internally while
    an embed is in flight.
    """

    def __init__(
        self,
        db: Database,
        queue: IndexingQueue,
        reconciler: Reconciler | None = None,
    ):
        self.db = db
        self.queue = queue
        self.reconciler = reconciler

    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        # Only enqueue files in cur/ or new/ subdirectories
        if path.parent.name in ("cur", "new"):
            self.queue.enqueue(str(path), REASON_ON_CREATED)

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

        # Case 2: new delivery — enqueue for the worker.
        if dest_path_obj.parent.name in ("cur", "new") and not self.db.is_indexed(dest_path):
            self.queue.enqueue(dest_path, REASON_ON_MOVED)


def _index_one_file(
    path: Path,
    db: Database,
    embedder: Embedder,
    threader: Threader,
) -> tuple[bool, str, str | None]:
    """Run the parse → thread → embed → upsert pipeline for ``path``.

    Returns ``(succeeded, stage, error_message)`` so the caller can
    record the specific failure stage in ``indexing_jobs.last_stage``.
    A ``None`` ``Message`` from the parser is treated as a terminal
    success (no Message-ID, nothing retries will fix) — the queue row
    is deleted rather than retried indefinitely.
    """
    try:
        message = parse_email(path, maildir_root=MAILDIR_PATH)
    except Exception as e:  # defensive: parse_email catches internally
        return False, "parse", repr(e)
    if message is None:
        return True, "parse", None

    try:
        thread = threader.assign_thread(message)
    except Exception as e:
        return False, "thread", repr(e)

    try:
        # The stored body_text feeds FTS (users legitimately search
        # quoted text), but the embedding input is stripped of quoted
        # replies and signatures so the vector tracks the substantive
        # content of each reply rather than accumulated quoted history.
        body = db.build_merged_body(thread)
        embedding = embedder.embed(strip_for_embedding(body))
    except Exception as e:
        return False, "embed", repr(e)

    try:
        db.upsert_thread(thread, embedding, body=body)
    except Exception as e:
        return False, "db_write", repr(e)

    log.info(f"Indexed: {message.subject[:60]}")
    return True, "db_write", None


def drain_queue(
    queue: IndexingQueue,
    db: Database,
    embedder: Embedder,
    threader: Threader,
    *,
    max_batch: int | None = None,
) -> int:
    """Process queued jobs until nothing is due, or ``max_batch`` have run.

    Returns the number of jobs attempted. Each iteration claims the
    next due row, runs the pipeline, and records success (delete row)
    or failure (increment attempts, schedule backoff, or dead-letter).
    Running with ``max_batch`` lets the main loop interleave reconciler
    passes and health-file refreshes with queue work so neither starves
    the other.
    """
    attempted = 0
    while max_batch is None or attempted < max_batch:
        row = queue.claim_next()
        if row is None:
            break
        filepath = row["filepath"]
        succeeded, stage, error = _index_one_file(Path(filepath), db, embedder, threader)
        if succeeded:
            queue.mark_succeeded(filepath)
        else:
            queue.mark_failed(filepath, stage=stage, error=error or "")
        attempted += 1
    return attempted


HEALTH_REFRESH_EVERY = 25


def _iter_maildir_messages(root: Path):
    """Yield every message file under ``root`` whose parent is ``cur`` or
    ``new``, at any nesting depth. mbsync ``SubFolders Verbatim`` can
    produce ``Clients/ABC/cur/msg`` — a flat ``iterdir`` over ``root``
    would miss every nested folder's mail."""
    for filepath in root.rglob("*"):
        if filepath.is_file() and filepath.parent.name in ("cur", "new"):
            yield filepath


def initial_index(
    db: Database,
    embedder: Embedder,
    threader: Threader,
    queue: IndexingQueue,
):
    """Enqueue every unindexed Maildir message and drain the queue.

    Refreshes the health file every ``HEALTH_REFRESH_EVERY`` processed
    messages so that long initial indexes (large mailboxes, slow Ollama
    embeddings) do not exceed ``HEALTH_MAX_AGE_SECONDS`` in the
    healthcheck and cause the container to be reported unhealthy
    mid-scan.

    Routing the initial scan through the queue — rather than indexing
    files inline — means a crash or Ollama outage mid-scan leaves the
    untouched work durably queued instead of dropped. The next restart
    resumes from ``indexing_jobs`` rather than rescanning the whole
    Maildir and relying on ``is_indexed`` to filter.
    """
    log.info("Running initial index scan...")
    enqueued = 0
    for filepath in _iter_maildir_messages(MAILDIR_PATH):
        if db.is_indexed(str(filepath)):
            continue
        queue.enqueue(str(filepath), REASON_INITIAL_SCAN)
        enqueued += 1
    log.info(f"Initial index: enqueued {enqueued} message(s).")

    processed = 0
    while True:
        row = queue.claim_next()
        if row is None:
            break
        filepath = row["filepath"]
        succeeded, stage, error = _index_one_file(Path(filepath), db, embedder, threader)
        if succeeded:
            queue.mark_succeeded(filepath)
        else:
            queue.mark_failed(filepath, stage=stage, error=error or "")
        processed += 1
        if processed % HEALTH_REFRESH_EVERY == 0:
            touch_health_file()
    log.info(f"Initial index complete: {processed} job(s) processed.")


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

    queue_cfg = load_queue_config_from_env(os.environ)
    queue = IndexingQueue(
        db,
        max_attempts=queue_cfg["max_attempts"],
        base_backoff_seconds=queue_cfg["base_backoff_seconds"],
    )
    log.info(
        "Indexing queue: max_attempts=%d base_backoff=%ds",
        queue_cfg["max_attempts"],
        queue_cfg["base_backoff_seconds"],
    )
    queue_depth = queue.stats()
    if queue_depth["queued"] or queue_depth["dead"]:
        log.info(
            "Queue carry-over from previous run: queued=%d dead=%d",
            queue_depth["queued"],
            queue_depth["dead"],
        )

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
    initial_index(db, embedder, threader, queue)
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
    handler = MaildirHandler(db, queue, reconciler=reconciler)
    observer = Observer()
    observer.schedule(handler, str(MAILDIR_PATH), recursive=True)
    observer.start()
    log.info("Watching Maildir for new emails...")

    last_reconcile = time.monotonic()
    try:
        while True:
            touch_health_file()
            # Drain any queued indexing jobs before yielding to the
            # reconciler so newly-arrived mail is visible in search
            # quickly. ``max_batch`` caps each pass so a large initial
            # enqueue (or a burst from an mbsync sync) does not starve
            # the reconciler or the health-file refresh.
            try:
                drain_queue(queue, db, embedder, threader, max_batch=HEALTH_REFRESH_EVERY)
            except Exception as e:
                log.error(f"queue drain failed: {e}")
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
