"""#1236: the per-message extraction budget and its durable deferral.

One pass of one message may start a bounded amount of extraction work
(process launches and seconds) on attachments the cache and the batch
cannot serve. Past it the remaining attachments are deferred: marked on
their occurrence, never written to the payload cache, their stored
chunks kept, and the message is continued with ``queue.defer`` at the
extract stage, atomically with the pass's results. A continuation
resolves only the pending occurrences and always admits one dispatch.

The extractor here stands in for the dispatcher and starts one real
process per call through ``_runner.run_tool``, so the launch counts the
tests assert are the processes this interpreter actually started.
"""

from __future__ import annotations

import functools
import logging
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest
from src import attachment_indexing, main
from src.attachment_indexing import (
    STATUS_DEFERRED,
    ExtractionBudget,
    apply_attachment_writes,
    prepare_attachment_writes,
)
from src.database import EMBEDDING_DIM, Database
from src.extractors import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    ExtractionResult,
    _runner,
    _stamp_extractor,
    extraction_module,
)
from src.queue import (
    ERROR_CLASS_RETRYABLE,
    EXTRACTION_DEFERRED_ERROR,
    REASON_INITIAL_SCAN,
    REASON_REPARSE,
    STAGE_EXTRACT,
    IndexingQueue,
)
from src.threader import Threader
from src.timings import TimingAggregator

from tests.conftest import make_message, make_mock_embedder, make_thread

MARKER = "SYNTHETIC_DEFER_MARKER"
_UNIT_VECTOR = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
_TRUE = shutil.which("true")


def _launch_one_process(payload: bytes) -> None:
    """Start one real process the way every extractor child is started."""
    assert _TRUE is not None
    _runner.run_tool(
        [_TRUE],
        payload,
        timeout_seconds=30,
        max_output_bytes=16,
        max_address_space_bytes=4 * 1024 * 1024 * 1024,
        max_cpu_seconds=10,
        suffix=".bin",
    )


class LaunchingExtractor:
    """Stands in for ``extractors.extract``: starts ``launches`` real
    processes per call and returns text naming the payload, or a
    ``failed`` result for a payload in ``fail``. ``clock`` (a dict with
    ``t``) is advanced ``seconds`` per call when given."""

    def __init__(self, *, fail=(), launches=1, clock=None, seconds=0.0):
        self.calls: list[bytes] = []
        self.fail = set(fail)
        self.launches = launches
        self.clock = clock
        self.seconds = seconds

    def __call__(self, *, content_type, filename, payload, **_kwargs) -> ExtractionResult:
        for _ in range(self.launches):
            _launch_one_process(payload)
        if self.clock is not None:
            self.clock["t"] += self.seconds
        self.calls.append(payload)
        module = extraction_module(content_type, filename, payload) or "text"
        stamp = _stamp_extractor(module, module)
        if payload in self.fail:
            return ExtractionResult(
                status=STATUS_FAILED, extractor=stamp, text=None, error="SyntheticError"
            )
        return ExtractionResult(
            status=STATUS_SUCCESS,
            extractor=stamp,
            text=f"words of {payload.decode()} under {content_type} {MARKER}",
            error=None,
            text_complete=True,
        )


def _attachment(payload: bytes, *, filename="note.txt", content_type="text/plain"):
    import hashlib

    from src.parser import Attachment

    return Attachment(
        filename=filename,
        content_type=content_type,
        size=len(payload),
        payload=payload,
        content_hash=hashlib.sha256(payload).hexdigest(),
    )


def _prepare_kwargs(**overrides):
    base = dict(
        claimant_id="msg@x",
        chunk_target_tokens=350,
        chunk_max_tokens=500,
        chunk_overlap_tokens=60,
        ocr_enabled=True,
        max_bytes=10_000_000,
        max_ocr_pages=20,
    )
    base.update(overrides)
    return base


def _setup_db(tmp_path) -> Database:
    db = Database(tmp_path / "mail.db")
    db.upsert_thread(
        make_thread(messages=[make_message(message_id="msg@x")], thread_id="thread-x"),
        [0.0] * EMBEDDING_DIM,
    )
    return db


# --- the budget, at the dispatch boundary --------------------------------


class TestBudgetAtDispatch:
    def test_launch_budget_counts_real_launches_and_defers_past_it(self, tmp_path, monkeypatch):
        extractor = LaunchingExtractor()
        monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
        db = _setup_db(tmp_path)
        budget = ExtractionBudget(max_launches=2, max_seconds=1000)
        before = _runner.process_launches()
        statuses = [
            prepare_attachment_writes(
                attachment=_attachment(f"payload-{i}".encode()),
                db=db,
                budget=budget,
                occurrence_index=i,
                **_prepare_kwargs(),
            ).status
            for i in range(5)
        ]
        assert statuses == [STATUS_SUCCESS, STATUS_SUCCESS, *[STATUS_DEFERRED] * 3]
        # The work done: two dispatches, two real processes started.
        assert len(extractor.calls) == 2
        assert _runner.process_launches() - before == 2
        assert (budget.dispatched, budget.launches, budget.deferred) == (2, 2, 3)

    def test_the_first_dispatch_is_admitted_and_its_overrun_is_one_attachment(
        self, tmp_path, monkeypatch
    ):
        """An admitted attachment runs to its own bounds: one that starts
        five processes against a budget of one ends the pass four
        launches past it, and the next attachment is deferred."""
        extractor = LaunchingExtractor(launches=5)
        monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
        db = _setup_db(tmp_path)
        budget = ExtractionBudget(max_launches=1, max_seconds=1000)
        before = _runner.process_launches()
        plans = [
            prepare_attachment_writes(
                attachment=_attachment(f"big-{i}".encode()),
                db=db,
                budget=budget,
                occurrence_index=i,
                **_prepare_kwargs(),
            )
            for i in range(2)
        ]
        assert [p.status for p in plans] == [STATUS_SUCCESS, STATUS_DEFERRED]
        assert _runner.process_launches() - before == 5 == budget.launches
        assert budget.launches - budget.max_launches == 4

    def test_seconds_budget_and_its_overrun(self, tmp_path, monkeypatch):
        clock = {"t": 0.0}
        extractor = LaunchingExtractor(clock=clock, seconds=2.0)
        monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
        db = _setup_db(tmp_path)
        budget = ExtractionBudget(max_launches=1000, max_seconds=5.0, clock=lambda: clock["t"])
        statuses = [
            prepare_attachment_writes(
                attachment=_attachment(f"slow-{i}".encode()),
                db=db,
                budget=budget,
                occurrence_index=i,
                **_prepare_kwargs(),
            ).status
            for i in range(5)
        ]
        # 0 -> 2 -> 4 s: the third is admitted at 4 s and ends at 6 s.
        assert statuses == [STATUS_SUCCESS] * 3 + [STATUS_DEFERRED] * 2
        assert budget.seconds == 6.0
        assert budget.seconds - budget.max_seconds <= extractor.seconds

    def test_cache_and_batch_hits_are_never_deferred_or_charged(self, tmp_path, monkeypatch):
        extractor = LaunchingExtractor()
        monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
        db = _setup_db(tmp_path)
        cached = _attachment(b"cached-payload")
        plan = prepare_attachment_writes(attachment=cached, db=db, **_prepare_kwargs())
        with db.transaction():
            plan.embeddings_by_chunk_id = {c.chunk_id: _UNIT_VECTOR for c in plan.chunks}
            apply_attachment_writes(plan=plan, claimant_id="msg@x", thread_id="thread-x", db=db)
        budget = ExtractionBudget(max_launches=1, max_seconds=1000)
        batch: dict = {}
        first = prepare_attachment_writes(
            attachment=_attachment(b"fresh"),
            db=db,
            budget=budget,
            batch_extractions=batch,
            **_prepare_kwargs(),
        )
        hit = prepare_attachment_writes(
            attachment=cached, db=db, budget=budget, occurrence_index=1, **_prepare_kwargs()
        )
        reused = prepare_attachment_writes(
            attachment=_attachment(b"fresh"),
            db=db,
            budget=budget,
            batch_extractions=batch,
            occurrence_index=2,
            **_prepare_kwargs(),
        )
        assert [first.status, hit.status, reused.status] == [STATUS_SUCCESS] * 3
        assert hit.cached and reused.cached
        assert budget.dispatched == 1
        assert len(extractor.calls) == 2  # the setup extraction and ``first``


# --- the occurrence's deferral mark ---------------------------------------


def _occurrence(db, occurrence_id) -> tuple:
    return tuple(
        db._conn.execute(
            "SELECT text_complete, text_extractor, extraction_deferred_at IS NOT NULL "
            "FROM attachments WHERE attachment_occurrence_id = ?",
            (occurrence_id,),
        ).fetchone()
    )


class TestDeferredOccurrence:
    def test_a_deferred_occurrence_keeps_its_chunks_and_writes_no_cache_row(
        self, tmp_path, monkeypatch
    ):
        extractor = LaunchingExtractor()
        monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
        db = _setup_db(tmp_path)
        attachment = _attachment(b"kept-payload")
        plan = prepare_attachment_writes(attachment=attachment, db=db, **_prepare_kwargs())
        plan.embeddings_by_chunk_id = {c.chunk_id: _UNIT_VECTOR for c in plan.chunks}
        with db.transaction():
            apply_attachment_writes(plan=plan, claimant_id="msg@x", thread_id="thread-x", db=db)
        chunks = db.get_chunk_ids_for_message("msg@x", attachment_id=attachment.content_hash)
        stamp = _occurrence(db, plan.occurrence_id)[1]
        assert chunks
        # The cached result is gone (as when a version bump stales it).
        db._conn.execute("DELETE FROM attachment_extractions")
        db._conn.commit()

        budget = ExtractionBudget(max_launches=1, max_seconds=1000)
        budget.charge(1, 0.0)
        deferred = prepare_attachment_writes(
            attachment=attachment, db=db, budget=budget, **_prepare_kwargs()
        )
        assert deferred.status == STATUS_DEFERRED
        assert (deferred.chunks, deferred.extraction_to_persist) == ([], None)
        with db.transaction():
            apply_attachment_writes(plan=deferred, claimant_id="msg@x", thread_id="thread-x", db=db)
        assert _occurrence(db, plan.occurrence_id) == (0, stamp, 1)
        assert db._conn.execute("SELECT COUNT(*) FROM attachment_extractions").fetchone()[0] == 0
        assert (
            db.get_chunk_ids_for_message("msg@x", attachment_id=attachment.content_hash) == chunks
        )

        resolved = prepare_attachment_writes(attachment=attachment, db=db, **_prepare_kwargs())
        with db.transaction():
            apply_attachment_writes(plan=resolved, claimant_id="msg@x", thread_id="thread-x", db=db)
        assert _occurrence(db, plan.occurrence_id) == (1, stamp, 0)


# --- the pipeline: continuation over passes --------------------------------


def _write_message(path: Path, message_id: str, attachments: list[tuple[bytes, str, str]]):
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Subject"] = f"Many parts {MARKER}"
    msg["Message-ID"] = f"<{message_id}>"
    msg["Date"] = "Mon, 01 Jan 2024 12:00:00 +0000"
    msg.set_content("Body text.")
    for payload, ctype, filename in attachments:
        maintype, subtype = ctype.split("/")
        msg.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(msg))


def _parts(name: str, n: int) -> list[tuple[bytes, str, str]]:
    return [(f"{name}-part-{i}".encode(), "text/plain", f"{MARKER}-{i}.txt") for i in range(n)]


class Pipeline:
    def __init__(self, tmp_path, monkeypatch, extractor, *, launches=3, seconds=1000.0):
        self.tmp_path = tmp_path
        self.maildir = tmp_path / "maildir"
        monkeypatch.setattr(main, "MAILDIR_PATH", self.maildir)
        monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
        monkeypatch.setattr(
            main,
            "ExtractionBudget",
            functools.partial(ExtractionBudget, max_launches=launches, max_seconds=seconds),
        )
        self.extractor = extractor
        self.open()

    def open(self) -> None:
        """Open the database and queue (again: a restart)."""
        self.db = Database(self.tmp_path / "mail.db")
        self.queue = IndexingQueue(self.db, max_attempts=3, base_backoff_seconds=0)

    def restart(self) -> None:
        self.db.close()
        self.open()

    def add(self, name: str, attachments, *, reason=REASON_INITIAL_SCAN) -> str:
        path = self.maildir / "INBOX" / "cur" / f"{name}.eml"
        _write_message(path, f"{name}@example.com", attachments)
        self.queue.enqueue(str(path), reason)
        return str(path)

    def drain(self, *, batch_size=8, passes=1) -> int:
        return main._drain_queue_batched(
            self.db,
            make_mock_embedder(_UNIT_VECTOR),
            Threader(self.db),
            self.queue,
            batch_size=batch_size,
            max_passes=passes,
            timing_aggregator=TimingAggregator(window=4),
        )

    def job(self, path: str):
        return self.db._conn.execute(
            "SELECT reason, status, attempts, last_stage, last_error, last_error_class "
            "FROM indexing_jobs WHERE filepath = ?",
            (path,),
        ).fetchone()

    def occurrences(self) -> list[tuple]:
        return [
            tuple(r)
            for r in self.db._conn.execute(
                "SELECT text_complete, extraction_deferred_at IS NOT NULL FROM attachments "
                "ORDER BY filename"
            )
        ]

    def deferred(self) -> int:
        return self.db._conn.execute(
            "SELECT COUNT(*) FROM attachments WHERE extraction_deferred_at IS NOT NULL"
        ).fetchone()[0]

    def cache_rows(self) -> int:
        return self.db._conn.execute("SELECT COUNT(*) FROM attachment_extractions").fetchone()[0]

    def attachment_chunks(self) -> set[str]:
        return {
            r[0]
            for r in self.db._conn.execute(
                "SELECT chunk_id FROM message_chunks WHERE attachment_id IS NOT NULL"
            )
        }


class TestContinuation:
    def test_a_message_over_budget_is_continued_until_every_attachment_resolves(
        self, tmp_path, monkeypatch
    ):
        extractor = LaunchingExtractor()
        p = Pipeline(tmp_path, monkeypatch, extractor, launches=3)
        path = p.add("big", _parts("big", 7))

        before = _runner.process_launches()
        p.drain()
        # Pass 1: three dispatches, three real processes, four deferred.
        assert len(extractor.calls) == 3
        assert _runner.process_launches() - before == 3
        job = p.job(path)
        assert tuple(job) == (
            REASON_INITIAL_SCAN,
            "queued",
            0,
            STAGE_EXTRACT,
            EXTRACTION_DEFERRED_ERROR,
            ERROR_CLASS_RETRYABLE,
        )
        assert p.deferred() == 4
        assert p.cache_rows() == 3
        assert sorted(p.occurrences()) == [(0, 1)] * 4 + [(1, 0)] * 3
        assert len(p.attachment_chunks()) == 3

        p.drain()
        assert len(extractor.calls) == 6
        assert p.deferred() == 1
        p.drain()
        # Done: each payload extracted exactly once over three passes.
        assert sorted(extractor.calls) == sorted(b for b, _, _ in _parts("big", 7))
        assert p.job(path) is None
        assert p.deferred() == 0
        assert p.occurrences() == [(1, 0)] * 7
        assert p.cache_rows() == 7
        assert len(p.attachment_chunks()) == 7

    def test_a_single_attachment_over_the_budget_still_progresses(self, tmp_path, monkeypatch):
        """Each pass admits its first dispatch, so an attachment that
        alone exceeds the budget resolves one per pass."""
        extractor = LaunchingExtractor(launches=3)
        p = Pipeline(tmp_path, monkeypatch, extractor, launches=1)
        path = p.add("heavy", _parts("heavy", 3))
        for expected in (1, 2, 3):
            before = _runner.process_launches()
            p.drain()
            assert len(extractor.calls) == expected
            assert _runner.process_launches() - before == 3
        assert p.job(path) is None

    def test_progress_past_cache_expiry_and_a_restart(self, tmp_path, monkeypatch):
        """A continuation never reopens an occurrence whose result applied,
        even after its cached row aged past every expiry (a failed row's
        retry window) and across a restart."""
        parts = _parts("aging", 6)
        failing = {parts[0][0], parts[1][0]}
        extractor = LaunchingExtractor(fail=failing)
        p = Pipeline(tmp_path, monkeypatch, extractor, launches=3)
        path = p.add("aging", parts)
        p.drain()
        assert len(extractor.calls) == 3
        old = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        p.db._conn.execute("UPDATE attachment_extractions SET extracted_at = ?", (old,))
        p.db._conn.commit()
        p.restart()
        p.drain()
        assert p.job(path) is None
        # Six dispatches in all: the aged failed rows were not re-run.
        assert sorted(extractor.calls) == sorted(b for b, _, _ in parts)
        assert p.deferred() == 0

    def test_a_fresh_enqueue_is_a_full_pass(self, tmp_path, monkeypatch):
        """New intent to index the file (a watcher event) resets the job,
        so the next pass resolves every occurrence by the usual cache
        rules (an aged failed row is retried), still within the budget."""
        parts = _parts("fresh", 4)
        extractor = LaunchingExtractor(fail={parts[0][0]})
        p = Pipeline(tmp_path, monkeypatch, extractor, launches=2)
        path = p.add("fresh", parts)
        p.drain()
        old = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        p.db._conn.execute("UPDATE attachment_extractions SET extracted_at = ?", (old,))
        p.db._conn.commit()
        p.queue.enqueue(path, REASON_INITIAL_SCAN)
        p.drain()
        # The aged failed row was retried, and one deferred part resolved.
        assert extractor.calls[2:] == [parts[0][0], parts[2][0]]
        assert p.deferred() == 1

    def test_rollback_writes_no_marks_and_no_continuation(self, tmp_path, monkeypatch):
        extractor = LaunchingExtractor()
        p = Pipeline(tmp_path, monkeypatch, extractor, launches=2)
        path = p.add("rollback", _parts("rollback", 4))
        real = Database.replace_thread_vector

        def failing(self, *args, **kwargs):
            raise sqlite3.OperationalError(f"disk I/O error {MARKER}")

        monkeypatch.setattr(Database, "replace_thread_vector", failing)
        p.drain()
        monkeypatch.setattr(Database, "replace_thread_vector", real)
        job = p.job(path)
        # Charged once, by the failure; no continuation was recorded.
        assert (job["attempts"], job["last_stage"], job["last_error"]) == (
            1,
            "db_write",
            "OperationalError",
        )
        assert p.deferred() == 0
        assert p.cache_rows() == 0
        assert p.attachment_chunks() == set()
        assert p.db._conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
        p.drain()
        assert p.deferred() == 2
        assert p.job(path)["last_stage"] == STAGE_EXTRACT

    def test_a_reparse_continuation_keeps_its_scheduling_class(self, tmp_path, monkeypatch):
        extractor = LaunchingExtractor()
        p = Pipeline(tmp_path, monkeypatch, extractor, launches=1)
        path = p.add("reparse", _parts("reparse", 2), reason=REASON_REPARSE)
        p.drain()
        assert p.job(path)["reason"] == REASON_REPARSE
        assert main._reparse_progress.reparsed == 0
        p.drain()
        assert p.job(path) is None
        assert main._reparse_progress.reparsed == 1

    def test_a_version_bump_reopens_exactly_its_occurrences(self, tmp_path, monkeypatch):
        """The startup sweep keeps the deferral marks; an occurrence an
        older extractor version produced is cleared and re-extracted once,
        the others are not."""
        parts = _parts("bump", 5)
        extractor = LaunchingExtractor()
        p = Pipeline(tmp_path, monkeypatch, extractor, launches=2)
        path = p.add("bump", parts)
        p.drain()
        assert p.deferred() == 3
        # The first part's result came from an older ``text`` version.
        first_hash = p.db._conn.execute(
            "SELECT attachment_id FROM attachments ORDER BY filename LIMIT 1"
        ).fetchone()[0]
        p.db._conn.execute(
            "UPDATE attachments SET text_extractor = 'text@1' WHERE attachment_id = ?",
            (first_hash,),
        )
        p.db._conn.execute(
            "UPDATE attachment_extractions SET extractor = 'text@1' WHERE attachment_id = ?",
            (first_hash,),
        )
        p.db._conn.commit()
        marks = p.db._conn.execute(
            "SELECT attachment_occurrence_id, extraction_deferred_at FROM attachments "
            "WHERE extraction_deferred_at IS NOT NULL ORDER BY 1"
        ).fetchall()
        assert main._clear_stale_text_completeness(p.db) == 1
        main._requeue_stale_extractions(p.db, p.queue)
        assert [
            tuple(r)
            for r in p.db._conn.execute(
                "SELECT attachment_occurrence_id, extraction_deferred_at FROM attachments "
                "WHERE extraction_deferred_at IS NOT NULL ORDER BY 1"
            )
        ] == [tuple(r) for r in marks]
        while p.job(path) is not None:
            p.drain()
        # Five parts once each, and the bumped one once more.
        assert sorted(extractor.calls) == sorted([b for b, _, _ in parts] + [parts[0][0]])
        assert p.occurrences() == [(1, 0)] * 5

    def test_a_lost_continuation_is_requeued_at_startup(self, tmp_path, monkeypatch):
        """A pass with attachment extraction switched off marks the
        message succeeded and leaves its deferral marks; once extraction
        is on again the startup sweep re-queues it, and it completes."""
        extractor = LaunchingExtractor()
        p = Pipeline(tmp_path, monkeypatch, extractor, launches=2)
        path = p.add("lost", _parts("lost", 4))
        p.drain()
        assert p.deferred() == 2
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_EXTRACTION_ENABLED", False)
        p.drain()
        assert p.job(path) is None
        assert p.deferred() == 2
        # Off, the sweep re-queues nothing; on, it re-queues the message.
        assert main._requeue_stale_extractions(p.db, p.queue) == 0
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_EXTRACTION_ENABLED", True)
        assert main._requeue_stale_extractions(p.db, p.queue) == 1
        while p.job(path) is not None:
            p.drain()
        assert p.deferred() == 0
        assert sorted(extractor.calls) == sorted(b for b, _, _ in _parts("lost", 4))

    def test_the_sweep_leaves_a_queued_continuation_alone(self, tmp_path, monkeypatch):
        extractor = LaunchingExtractor()
        p = Pipeline(tmp_path, monkeypatch, extractor, launches=2)
        path = p.add("queued", _parts("queued", 4))
        p.drain()
        assert main._requeue_stale_extractions(p.db, p.queue) == 0
        assert p.job(path)["last_stage"] == STAGE_EXTRACT

    def test_shared_payload_chunks_survive_a_deferral(self, tmp_path, monkeypatch):
        """The same bytes under two labels that run different extractors
        share one chunk slice (#928). While one occurrence is deferred,
        the other's write keeps the deferred one's chunks."""
        payload = b"<p>shared payload</p>"
        attachments = [
            (payload, "text/plain", "a.txt"),
            (payload, "text/html", "b.html"),
        ]
        extractor = LaunchingExtractor()
        p = Pipeline(tmp_path, monkeypatch, extractor, launches=5)
        path = p.add("shared", attachments)
        p.drain()
        assert p.job(path) is None
        both = p.attachment_chunks()
        assert len(both) == 2
        # Both results are stale now (as after a version bump), and the
        # budget admits one dispatch per pass.
        p.db._conn.execute("DELETE FROM attachment_extractions")
        p.db._conn.execute("UPDATE attachments SET text_complete = NULL")
        p.db._conn.commit()
        monkeypatch.setattr(
            main, "ExtractionBudget", functools.partial(ExtractionBudget, max_launches=1)
        )
        p.queue.enqueue(path, REASON_INITIAL_SCAN)
        p.drain()
        assert p.deferred() == 1
        assert p.attachment_chunks() == both
        p.drain()
        assert p.job(path) is None
        assert p.attachment_chunks() == both

    def test_a_textless_copy_does_not_clear_a_deferred_copys_chunks(self, tmp_path, monkeypatch):
        """The same, when the dispatched copy now yields no text: its
        empty write would clear the shared slice. It is kept while the
        other copy is deferred, and settled once that copy resolves."""
        payload = b"<p>shared payload</p>"
        attachments = [
            (payload, "text/plain", "a.txt"),
            (payload, "text/html", "b.html"),
        ]
        extractor = LaunchingExtractor()
        p = Pipeline(tmp_path, monkeypatch, extractor, launches=5)
        path = p.add("shared", attachments)
        p.drain()
        both = p.attachment_chunks()
        p.db._conn.execute("DELETE FROM attachment_extractions")
        p.db._conn.execute("UPDATE attachments SET text_complete = NULL")
        p.db._conn.commit()
        monkeypatch.setattr(
            main, "ExtractionBudget", functools.partial(ExtractionBudget, max_launches=1)
        )
        extractor.fail = {payload}
        p.queue.enqueue(path, REASON_INITIAL_SCAN)
        p.drain()
        assert p.deferred() == 1
        assert p.attachment_chunks() == both
        # The deferred copy now fails too: nothing holds text, the slice
        # is cleared.
        p.drain()
        assert p.job(path) is None
        assert p.attachment_chunks() == set()


class TestFairness:
    def test_continuous_arrivals_and_a_continuation_take_turns(self, tmp_path, monkeypatch):
        extractor = LaunchingExtractor()
        p = Pipeline(tmp_path, monkeypatch, extractor, launches=2)
        big = p.add("big", _parts("big", 6))
        order: list[str] = []
        real_phase1 = main._phase1_commit_thread

        def recording(row, *args, **kwargs):
            order.append(Path(row["filepath"]).stem)
            return real_phase1(row, *args, **kwargs)

        monkeypatch.setattr(main, "_phase1_commit_thread", recording)
        for i in range(4):
            p.drain(batch_size=1)
            p.add(f"arrival{i}", _parts(f"arrival{i}", 1))
        while p.queue.stats()["queued"]:
            p.drain(batch_size=1)
        # Due-time order: the continuation goes behind rows already due,
        # each arrival behind the continuation queued before it.
        assert order == [
            "big",
            "big",
            "arrival0",
            "big",
            "arrival1",
            "arrival2",
            "arrival3",
        ]
        assert p.job(big) is None

    def test_two_continuations_alternate(self, tmp_path, monkeypatch):
        extractor = LaunchingExtractor()
        p = Pipeline(tmp_path, monkeypatch, extractor, launches=1)
        first = p.add("first", _parts("first", 3))
        second = p.add("second", _parts("second", 3))
        order: list[bytes] = []
        while p.queue.stats()["queued"]:
            before = len(extractor.calls)
            p.drain(batch_size=1)
            order.extend(extractor.calls[before:])
        assert [b.split(b"-")[0] for b in order] == [b"first", b"second"] * 3
        assert p.job(first) is None and p.job(second) is None


# --- visibility ------------------------------------------------------------


class TestVisibility:
    def test_deferral_and_recovery_are_logged_with_counts_and_no_mail(
        self, tmp_path, monkeypatch, caplog
    ):
        caplog.set_level(logging.INFO)
        extractor = LaunchingExtractor()
        p = Pipeline(tmp_path, monkeypatch, extractor, launches=2)
        path = p.add("visible", _parts("visible", 5))
        attachment_indexing.attachment_outcomes.drain()

        p.drain()
        main._log_attachment_outcomes(force=True)
        monkeypatch.setattr(main, "_last_queue_heartbeat", None)
        main._maybe_log_queue_heartbeat(p.queue)
        lines = [r for r in caplog.records if r.getMessage().startswith("attachments n=")]
        assert [r.levelno for r in lines] == [logging.WARNING]
        assert "n=5 success=2 " in lines[0].getMessage()
        assert " deferred=3 " in lines[0].getMessage()
        assert " deferred_messages=1 deferred_resumed=0 " in lines[0].getMessage()
        heartbeat = [r.getMessage() for r in caplog.records if r.getMessage().startswith("queue:")]
        assert " extraction_deferred=1 " in heartbeat[-1]
        assert p.job(path)["last_error"] == EXTRACTION_DEFERRED_ERROR

        caplog.clear()
        while p.job(path) is not None:
            p.drain()
        main._log_attachment_outcomes(force=True)
        monkeypatch.setattr(main, "_last_queue_heartbeat", None)
        main._maybe_log_queue_heartbeat(p.queue)
        monkeypatch.setattr(main, "_last_queue_heartbeat", None)
        main._maybe_log_queue_heartbeat(p.queue)
        line = next(r for r in caplog.records if r.getMessage().startswith("attachments n="))
        # Passes 2 and 3: the served occurrences are not counted again.
        assert "n=4 success=3 " in line.getMessage()
        assert " deferred=1 " in line.getMessage()
        assert " deferred_messages=1 deferred_resumed=3 " in line.getMessage()
        recovered = [
            r
            for r in caplog.records
            if r.getMessage().startswith("attachment extraction caught up")
        ]
        assert [r.levelno for r in recovered] == [logging.INFO]
        assert MARKER not in caplog.text
        errors = [r[0] for r in p.db._conn.execute("SELECT last_error FROM indexing_jobs")]
        assert not any(MARKER in (e or "") for e in errors)


# --- database pieces --------------------------------------------------------


class TestDatabase:
    def test_queue_write_commits_with_the_outer_transaction(self, tmp_path):
        db = Database(tmp_path / "mail.db")
        queue = IndexingQueue(db)
        queue.enqueue("/m/a", REASON_INITIAL_SCAN)
        with pytest.raises(RuntimeError), db.transaction():
            queue.defer(
                "/m/a",
                stage=STAGE_EXTRACT,
                error=EXTRACTION_DEFERRED_ERROR,
                error_class=ERROR_CLASS_RETRYABLE,
                delay_seconds=0,
            )
            raise RuntimeError("abort")
        row = db._conn.execute("SELECT last_stage FROM indexing_jobs").fetchone()
        assert row["last_stage"] is None
        with db.transaction():
            queue.defer(
                "/m/a",
                stage=STAGE_EXTRACT,
                error=EXTRACTION_DEFERRED_ERROR,
                error_class=ERROR_CLASS_RETRYABLE,
                delay_seconds=0,
            )
        row = db._conn.execute("SELECT last_stage FROM indexing_jobs").fetchone()
        assert row["last_stage"] == STAGE_EXTRACT
        db.close()

    def test_occurrence_states(self, tmp_path):
        db = _setup_db(tmp_path)
        for occurrence in ("occ-a", "occ-b", "occ-c"):
            db.upsert_attachment(
                claimant_id="msg@x",
                thread_id="thread-x",
                attachment_id=f"h-{occurrence}",
                filename="f.txt",
                content_type="text/plain",
                size_bytes=1,
                occurrence_id=occurrence,
                extractor_module="text",
            )
        db.set_attachment_text_complete("occ-a", True, "text@3")
        db.mark_attachment_extraction_deferred("occ-b")
        assert db.get_attachment_occurrence_states("msg@x") == {
            "occ-a": (True, False),
            "occ-b": (False, True),
            "occ-c": (False, False),
        }
        db.close()
