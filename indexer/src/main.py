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

from .attachment_indexing import process_attachment
from .chunker import chunk_message, mean_vector
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
from .timings import StageTimings, TimingAggregator, format_summary

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


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    """Read a positive int from the environment with a clamp + fallback.

    Used for the chunker token budgets so a typo or empty string falls back
    to the default rather than raising at startup. Mirrors the lenient parse
    used elsewhere (queue, reconciler) so operators get the same behavior
    across knobs.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("invalid %s=%r; falling back to %d", name, raw, default)
        return default
    return max(minimum, value)


# Chunker token budgets — see ``chunker.chunk_message`` for semantics. The
# defaults match the chunker's own defaults, sized for ``nomic-embed-text``
# at 768 dim with an ~8k token context window (target=350 tokens leaves
# generous headroom).
CHUNK_TARGET_TOKENS = _int_env("INDEXER_CHUNK_TARGET_TOKENS", 350)
CHUNK_MAX_TOKENS = _int_env("INDEXER_CHUNK_MAX_TOKENS", 500)
CHUNK_OVERLAP_TOKENS = _int_env("INDEXER_CHUNK_OVERLAP_TOKENS", 60, minimum=0)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# Attachment extraction — see ``src/extractors/`` for
# per-format implementations and ``.env.example`` for the operator-
# facing reference. Defaults are conservative: OCR enabled (most
# valuable for scanned receipts and screenshots), 10 MB attachment
# cap (skips huge backup zips), 20-page OCR cap (bounds CPU on
# scanned books).
INDEXER_ATTACHMENT_EXTRACTION_ENABLED = _bool_env("INDEXER_ATTACHMENT_EXTRACTION_ENABLED", True)
INDEXER_OCR_ENABLED = _bool_env("INDEXER_OCR_ENABLED", True)
INDEXER_ATTACHMENT_MAX_BYTES = _int_env("INDEXER_ATTACHMENT_MAX_BYTES", 10_000_000, minimum=1)
INDEXER_OCR_MAX_PAGES = _int_env("INDEXER_OCR_MAX_PAGES", 20, minimum=1)


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
) -> tuple[bool, str, str | None, StageTimings]:
    """Run the parse → thread → embed → upsert pipeline for ``path``.

    Returns ``(succeeded, stage, error_message, timings)`` so the caller
    can record the specific failure stage in ``indexing_jobs.last_stage``
    and feed the per-stage durations into a rolling aggregator. A
    ``None`` ``Message`` from the parser is treated as a terminal
    success (no Message-ID, nothing retries will fix) — the queue row
    is deleted rather than retried indefinitely. Timings reflect only
    stages that ran; stages skipped due to an earlier failure stay 0.
    """
    parse_ms = thread_ms = embed_ms = db_write_ms = 0.0

    t0 = time.perf_counter()
    try:
        message = parse_email(path, maildir_root=MAILDIR_PATH)
    except Exception as e:  # defensive: parse_email catches internally
        parse_ms = (time.perf_counter() - t0) * 1000
        return False, "parse", repr(e), StageTimings(parse_ms=parse_ms)
    parse_ms = (time.perf_counter() - t0) * 1000
    if message is None:
        return True, "parse", None, StageTimings(parse_ms=parse_ms)

    t0 = time.perf_counter()
    try:
        thread = threader.assign_thread(message)
    except Exception as e:
        thread_ms = (time.perf_counter() - t0) * 1000
        return (
            False,
            "thread",
            repr(e),
            StageTimings(parse_ms=parse_ms, thread_ms=thread_ms),
        )
    thread_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    try:
        chunk_writes = []
        # Chunk each newly-arrived message individually. ``thread.messages``
        # is the new arrivals only — existing messages already have chunks
        # on disk and re-chunking them would burn embed cycles for no
        # gain (chunk ids are deterministic from message_pk + index +
        # text, so the diff would be empty anyway). For new threads,
        # ``thread.messages`` is the full thread (one message); for
        # updates, it is the single newly-arrived reply.
        for msg in thread.messages:
            chunks = chunk_message(
                message_pk=msg.message_id,
                body_text=strip_for_embedding(msg.body_text or ""),
                target_tokens=CHUNK_TARGET_TOKENS,
                max_tokens=CHUNK_MAX_TOKENS,
                overlap_tokens=CHUNK_OVERLAP_TOKENS,
            )
            stored_ids = db.get_chunk_ids_for_message(msg.message_id)
            new_chunks = [c for c in chunks if c.chunk_id not in stored_ids]
            embeddings_by_chunk_id = {
                chunk.chunk_id: embedder.embed(chunk.text) for chunk in new_chunks
            }
            chunk_writes.append((msg, chunks, embeddings_by_chunk_id))

        # Seed the thread row with the current vector if one exists. The
        # final vector is replaced after the chunk writes inside the same
        # transaction, so thread/chunk/vector state commits or rolls back
        # together.
        chunk_embeddings = db.get_thread_chunk_embeddings(thread.thread_id)
        has_incoming_chunks = any(chunks for _, chunks, _ in chunk_writes)
        if chunk_embeddings:
            embedding = mean_vector(chunk_embeddings)
        elif has_incoming_chunks:
            # Temporary in-transaction value. The thread vector is replaced
            # after chunk rows are written and before the transaction commits.
            embedding = [0.0] * EMBEDDING_DIM
        else:
            fallback = thread.subject.strip() if thread.subject else "(empty thread)"
            embedding = embedder.embed(fallback)
    except Exception as e:
        embed_ms = (time.perf_counter() - t0) * 1000
        return (
            False,
            "embed",
            repr(e),
            StageTimings(parse_ms=parse_ms, thread_ms=thread_ms, embed_ms=embed_ms),
        )
    embed_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    try:
        with db.transaction():
            db.upsert_thread(thread, embedding)
            for msg, chunks, embeddings_by_chunk_id in chunk_writes:
                db.replace_message_chunks(
                    message_id=msg.message_id,
                    thread_id=thread.thread_id,
                    chunks=chunks,
                    embeddings_by_chunk_id=embeddings_by_chunk_id,
                )

                # Per-message attachment processing. Benign extractor
                # outcomes (unsupported, empty, too_large, failed parse) are
                # recorded as status rows by process_attachment. Hard
                # infrastructure failures (Ollama, SQLite) propagate so the
                # outer transaction rolls back and the queue retries the
                # whole message rather than committing a half-indexed
                # attachment.
                if INDEXER_ATTACHMENT_EXTRACTION_ENABLED:
                    for occurrence_index, attachment in enumerate(msg.attachments):
                        process_attachment(
                            attachment=attachment,
                            message_id=msg.message_id,
                            thread_id=thread.thread_id,
                            db=db,
                            embedder=embedder,
                            chunk_target_tokens=CHUNK_TARGET_TOKENS,
                            chunk_max_tokens=CHUNK_MAX_TOKENS,
                            chunk_overlap_tokens=CHUNK_OVERLAP_TOKENS,
                            ocr_enabled=INDEXER_OCR_ENABLED,
                            max_bytes=INDEXER_ATTACHMENT_MAX_BYTES,
                            max_ocr_pages=INDEXER_OCR_MAX_PAGES,
                            occurrence_index=occurrence_index,
                        )

            updated_chunk_embeddings = db.get_thread_chunk_embeddings(thread.thread_id)
            if updated_chunk_embeddings:
                db.replace_thread_vector(thread.thread_id, mean_vector(updated_chunk_embeddings))
    except Exception as e:
        db_write_ms = (time.perf_counter() - t0) * 1000
        return (
            False,
            "db_write",
            repr(e),
            StageTimings(
                parse_ms=parse_ms,
                thread_ms=thread_ms,
                embed_ms=embed_ms,
                db_write_ms=db_write_ms,
            ),
        )
    db_write_ms = (time.perf_counter() - t0) * 1000

    log.info(f"Indexed: {message.subject[:60]}")
    return (
        True,
        "db_write",
        None,
        StageTimings(
            parse_ms=parse_ms,
            thread_ms=thread_ms,
            embed_ms=embed_ms,
            db_write_ms=db_write_ms,
        ),
    )


def drain_queue(
    queue: IndexingQueue,
    db: Database,
    embedder: Embedder,
    threader: Threader,
    *,
    max_batch: int | None = None,
    timing_aggregator: TimingAggregator | None = None,
) -> int:
    """Process queued jobs until nothing is due, or ``max_batch`` have run.

    Returns the number of jobs attempted. Each iteration claims the
    next due row, runs the pipeline, and records success (delete row)
    or failure (increment attempts, schedule backoff, or dead-letter).
    Running with ``max_batch`` lets the main loop interleave reconciler
    passes and health-file refreshes with queue work so neither starves
    the other. ``timing_aggregator`` is fed every per-file timing so
    operators can see p50/p95 of each stage in the periodic summary.
    """
    attempted = 0
    while max_batch is None or attempted < max_batch:
        row = queue.claim_next()
        if row is None:
            break
        filepath = row["filepath"]
        succeeded, stage, error, timings = _index_one_file(Path(filepath), db, embedder, threader)
        if timing_aggregator is not None:
            timing_aggregator.record(timings)
        if succeeded:
            queue.mark_succeeded(filepath)
        else:
            queue.mark_failed(filepath, stage=stage, error=error or "")
        attempted += 1
    return attempted


HEALTH_REFRESH_EVERY = 25
# Emit a p50/p95/max timing summary at most this often. The aggregator
# itself has an independent rolling window — this constant only controls
# how often the line is logged, not how many samples back the percentiles
# look. Keeping it equal to ``HEALTH_REFRESH_EVERY`` lines summaries up
# with the same cadence as the health-file refresh.
TIMING_LOG_EVERY = 25


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
    timing_aggregator = TimingAggregator(window=200)
    while True:
        row = queue.claim_next()
        if row is None:
            break
        filepath = row["filepath"]
        succeeded, stage, error, timings = _index_one_file(Path(filepath), db, embedder, threader)
        timing_aggregator.record(timings)
        if succeeded:
            queue.mark_succeeded(filepath)
        else:
            queue.mark_failed(filepath, stage=stage, error=error or "")
        processed += 1
        if processed % HEALTH_REFRESH_EVERY == 0:
            touch_health_file()
        if processed % TIMING_LOG_EVERY == 0:
            line = format_summary(timing_aggregator.summary())
            if line:
                log.info(line)
    # Always emit a final summary at the end of the initial scan, even
    # if the count was not a multiple of ``TIMING_LOG_EVERY`` — the
    # operator wants to see the cost of the scan they just ran.
    final_line = format_summary(timing_aggregator.summary())
    if final_line:
        log.info(final_line)
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
    timing_aggregator = TimingAggregator(window=200)
    drained_since_log = 0
    try:
        while True:
            touch_health_file()
            # Drain any queued indexing jobs before yielding to the
            # reconciler so newly-arrived mail is visible in search
            # quickly. ``max_batch`` caps each pass so a large initial
            # enqueue (or a burst from an mbsync sync) does not starve
            # the reconciler or the health-file refresh.
            try:
                drained = drain_queue(
                    queue,
                    db,
                    embedder,
                    threader,
                    max_batch=HEALTH_REFRESH_EVERY,
                    timing_aggregator=timing_aggregator,
                )
                drained_since_log += drained
                if drained_since_log >= TIMING_LOG_EVERY:
                    line = format_summary(timing_aggregator.summary())
                    if line:
                        log.info(line)
                    drained_since_log = 0
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
