"""#1375: attachment occurrences the current parse no longer produces.

When a parser change stops producing an attachment occurrence of a
message already indexed (a corrected filename, a new structural cap),
the next pass of that message deletes the occurrence in its Phase 2c
transaction: its ``attachments`` row, its ``attachments_fts`` row and,
only where no remaining occurrence of the message carries the same
payload, the message's chunk slice of that payload, with the thread's
chunk-vector sum updated as ``S -= old`` (#1356).
"""

from __future__ import annotations

import functools
import hashlib
import logging
import sqlite3
from unittest.mock import MagicMock

import sqlite_vec
from src import attachment_indexing, main, vector_sums
from src.attachment_indexing import ExtractionBudget
from src.chunker import l2_normalize
from src.database import EMBEDDING_DIM, Database
from src.queue import REASON_INITIAL_SCAN, REASON_REPARSE
from src.threader import Threader
from src.timings import TimingAggregator

from tests.test_extraction_budget import (
    MARKER,
    LaunchingExtractor,
    Pipeline,
    _parts,
)


def _vector(text: str) -> list[float]:
    """A distinct unit vector per text, so a sum that kept or lost the
    wrong chunk differs from a recompute."""
    digest = hashlib.sha256(text.encode()).digest()
    return l2_normalize([(b - 127.5) / 128.0 for b in digest] + [0.0] * (EMBEDDING_DIM - 32))


def _embedder() -> MagicMock:
    m = MagicMock()
    m.embed.side_effect = _vector
    m.embed_batch.side_effect = lambda texts, **_kw: [_vector(t) for t in texts]
    return m


class StalePipeline(Pipeline):
    def __init__(self, tmp_path, monkeypatch, *, launches=50):
        super().__init__(tmp_path, monkeypatch, LaunchingExtractor(), launches=launches)
        self.monkeypatch = monkeypatch
        self.embedder = _embedder()

    def drain(self, *, batch_size=8, passes=1) -> int:
        return main._drain_queue_batched(
            self.db,
            self.embedder,
            Threader(self.db),
            self.queue,
            batch_size=batch_size,
            max_passes=passes,
            timing_aggregator=TimingAggregator(window=4),
        )

    def reparse(self, path: str) -> None:
        self.queue.enqueue(path, REASON_REPARSE)
        self.drain()
        assert self.job(path) is None

    def parse_with(self, rewrite) -> None:
        """Run every later parse through ``rewrite(path, msg)``."""
        real_parse = main.parse_email

        def patched(path, *args, **kwargs):
            msg = real_parse(path, *args, **kwargs)
            rewrite(str(path), msg)
            return msg

        self.monkeypatch.setattr(main, "parse_email", patched)

    def rows(self) -> list[tuple]:
        return [
            tuple(r)
            for r in self.db._conn.execute(
                "SELECT claimant_id, attachment_id, filename FROM attachments "
                "ORDER BY claimant_id, filename"
            )
        ]

    def fts_rowids(self) -> set[int]:
        stored = {
            r[0]
            for r in self.db._conn.execute(
                "SELECT fts_rowid FROM attachments WHERE fts_rowid IS NOT NULL"
            )
        }
        assert stored == self.fts_index_rowids()
        return stored

    def fts_index_rowids(self) -> set[int]:
        return {r[0] for r in self.db._conn.execute("SELECT rowid FROM attachments_fts")}

    def slices(self) -> dict[tuple[str, str], set[str]]:
        out: dict[tuple[str, str], set[str]] = {}
        for claimant_id, attachment_id, chunk_id in self.db._conn.execute(
            "SELECT claimant_id, attachment_id, chunk_id FROM message_chunks "
            "WHERE attachment_id IS NOT NULL"
        ):
            out.setdefault((claimant_id, attachment_id), set()).add(chunk_id)
        return out

    def vec_ids(self) -> set[str]:
        return {r[0] for r in self.db._conn.execute("SELECT chunk_id FROM message_chunks_vec")}

    def thread_ids(self) -> list[str]:
        return [r[0] for r in self.db._conn.execute("SELECT thread_id FROM threads")]

    def stored_sums(self, thread_id: str) -> tuple[bytes, int]:
        row = self.db._conn.execute(
            "SELECT sum, count FROM thread_vector_sums WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return bytes(row[0]), row[1]

    def recomputed_sums(self, thread_id: str) -> tuple[bytes, int]:
        total, count = self.db._compute_thread_sums(thread_id)
        return vector_sums.encode(total), count

    def thread_vector(self, thread_id: str) -> bytes:
        return bytes(
            self.db._conn.execute(
                "SELECT embedding FROM threads_vec WHERE thread_id = ?", (thread_id,)
            ).fetchone()[0]
        )


def _count_calls(monkeypatch, cls, name: str) -> list[int]:
    calls = [0]
    real = getattr(cls, name)

    def counted(*args, **kwargs):
        calls[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(cls, name, counted)
    return calls


def _claimant(p: StalePipeline, path: str) -> str:
    return p.db._conn.execute(
        "SELECT claimant_id FROM messages WHERE filepath = ?", (path,)
    ).fetchone()[0]


class TestStaleSibling:
    def test_a_corrected_filename_drops_the_stale_row_and_keeps_the_chunks(
        self, tmp_path, monkeypatch, caplog
    ):
        """The observed shape: an occurrence indexed under an undecoded
        ``?=`` filename, then the same payload under the corrected name
        after the parser fix. The stale row and its FTS row go; the
        payload's chunks are the corrected occurrence's too and stay,
        with nothing embedded or extracted again."""
        p = StalePipeline(tmp_path, monkeypatch)
        names = {"stale": True}

        def rewrite(_path, msg):
            if names["stale"]:
                msg.attachments[0].filename += "?="

        p.parse_with(rewrite)
        path = p.add("sibling", _parts("sibling", 2))
        p.drain()
        claimant = _claimant(p, path)
        stale = p.db._conn.execute(
            "SELECT attachment_occurrence_id, attachment_id, fts_rowid FROM attachments "
            "WHERE filename LIKE '%?='"
        ).fetchone()
        slices_before = p.slices()
        sums_before = p.stored_sums(p.thread_ids()[0])
        embeds_before = p.embedder.embed_batch.call_count
        extractions_before = len(p.extractor.calls)
        # The parser fix: the next parse produces the corrected name, so
        # the occurrence ID differs and the old one is stale.
        names["stale"] = False
        caplog.set_level(logging.INFO)
        attachment_indexing.attachment_outcomes.drain()
        caplog.clear()
        p.reparse(path)

        rows = p.rows()
        assert len(rows) == 2
        assert not any(r[2].endswith("?=") for r in rows)
        assert stale["attachment_occurrence_id"] not in {
            r[0] for r in p.db._conn.execute("SELECT attachment_occurrence_id FROM attachments")
        }
        assert stale["fts_rowid"] not in p.fts_index_rowids()
        assert len(p.fts_rowids()) == 2
        # The corrected row carries the same payload: its slice stays as
        # it was, chunk for chunk, and nothing was embedded or extracted.
        assert (claimant, stale["attachment_id"]) in slices_before
        assert p.slices() == slices_before
        assert p.stored_sums(p.thread_ids()[0]) == sums_before
        assert p.embedder.embed_batch.call_count == embeds_before
        assert len(p.extractor.calls) == extractions_before
        # A stale row beside a surviving sibling loses no text: INFO.
        main._log_attachment_outcomes(force=True)
        lines = [r for r in caplog.records if r.getMessage().startswith("attachments n=")]
        assert [r.levelno for r in lines] == [logging.INFO]
        assert " dropped=1 dropped_text=0 " in lines[0].getMessage()
        # The cache row of the payload is still used, so it stays.
        assert (
            p.db._conn.execute(
                "SELECT COUNT(*) FROM attachment_extractions WHERE attachment_id = ?",
                (stale["attachment_id"],),
            ).fetchone()[0]
            == 1
        )


class TestUniquePayload:
    def test_a_dropped_occurrence_takes_its_slice_and_the_sums_stay_exact(
        self, tmp_path, monkeypatch
    ):
        p = StalePipeline(tmp_path, monkeypatch)
        drop = {"on": False}

        def rewrite(_path, msg):
            if drop["on"]:
                msg.attachments = msg.attachments[:-1]

        p.parse_with(rewrite)
        path = p.add("unique", _parts("unique", 3))
        p.drain()
        claimant = _claimant(p, path)
        thread_id = p.thread_ids()[0]
        dropped_hash = hashlib.sha256(b"unique-part-2").hexdigest()
        dropped_slice = p.slices()[(claimant, dropped_hash)]
        assert dropped_slice
        vector_before = p.thread_vector(thread_id)
        recomputes = _count_calls(monkeypatch, Database, "_compute_thread_sums")
        embedded_before = [call.args for call in p.embedder.embed_batch.call_args_list]

        drop["on"] = True
        p.reparse(path)

        # The body and the other attachments keep the thread's chunks:
        # no subject fallback, so the reparse sent nothing to the
        # embedder (Codex round 4 on #1385).
        assert [call.args for call in p.embedder.embed_batch.call_args_list] == embedded_before
        assert len(p.rows()) == 2
        assert dropped_hash not in {r[1] for r in p.rows()}
        assert (claimant, dropped_hash) not in p.slices()
        assert not dropped_slice & p.vec_ids()
        assert not dropped_slice & {
            r[0] for r in p.db._conn.execute("SELECT chunk_id FROM message_chunks")
        }
        assert len(p.fts_rowids()) == 2
        # ``S -= old`` in the pass, with no thread-wide read, and the
        # result byte-equal to a full recompute.
        assert recomputes[0] == 0
        assert p.stored_sums(thread_id) == p.recomputed_sums(thread_id)
        # The thread vector is derived from the sums without the slice.
        assert p.thread_vector(thread_id) != vector_before
        # The payload's cache row has no occurrence left: purged, as when
        # a whole message is removed.
        assert (
            p.db._conn.execute(
                "SELECT COUNT(*) FROM attachment_extractions WHERE attachment_id = ?",
                (dropped_hash,),
            ).fetchone()[0]
            == 0
        )

    def test_a_chunkless_thread_gets_its_subject_fallback(self, tmp_path, monkeypatch):
        """A message whose only chunks were the dropped occurrence's: the
        thread is left chunkless, so its vector is the subject fallback,
        not the stale chunks' mean the pass seeded it with."""
        p = StalePipeline(tmp_path, monkeypatch)
        drop = {"on": False}

        def rewrite(_path, msg):
            msg.body_text = ""
            if drop["on"]:
                msg.attachments = []

        p.parse_with(rewrite)
        path = p.add("only", _parts("only", 1))
        p.drain()
        thread_id = p.thread_ids()[0]
        assert p.slices()
        drop["on"] = True
        p.reparse(path)
        assert p.slices() == {}
        assert p.rows() == []
        assert p.stored_sums(thread_id)[1] == 0
        subject = p.db.get_thread_display_subject(thread_id)
        assert subject
        expected = sqlite_vec_bytes(_vector(subject.strip()))
        assert p.thread_vector(thread_id) == expected


def sqlite_vec_bytes(vector: list[float]) -> bytes:
    return bytes(sqlite_vec.serialize_float32(l2_normalize(vector)))


class TestDeferred:
    def test_a_dropped_deferred_occurrence_is_removed_with_its_slice(self, tmp_path, monkeypatch):
        """An occurrence deferred by the extraction budget, with chunks
        from an earlier result, that the parse then drops: its row, mark
        and slice go, and the message finishes with no continuation."""
        p = StalePipeline(tmp_path, monkeypatch)
        path = p.add("deferred", _parts("deferred", 3))
        p.drain()
        claimant = _claimant(p, path)
        thread_id = p.thread_ids()[0]
        dropped_hash = hashlib.sha256(b"deferred-part-2").hexdigest()
        assert (claimant, dropped_hash) in p.slices()
        # Every result is stale (as after a version bump) and the budget
        # admits one dispatch per pass: two occurrences are deferred.
        p.db._conn.execute("DELETE FROM attachment_extractions")
        p.db._conn.execute("UPDATE attachments SET text_complete = NULL")
        p.db._conn.commit()
        monkeypatch.setattr(
            main, "ExtractionBudget", functools.partial(ExtractionBudget, max_launches=1)
        )
        p.queue.enqueue(path, REASON_REPARSE)
        p.drain()
        assert p.deferred() == 2
        assert p.db._conn.execute(
            "SELECT extraction_deferred_at IS NOT NULL FROM attachments WHERE attachment_id = ?",
            (dropped_hash,),
        ).fetchone()[0]

        def rewrite(_path, msg):
            msg.attachments = msg.attachments[:-1]

        p.parse_with(rewrite)
        while p.job(path) is not None:
            p.drain()
        assert p.deferred() == 0
        assert dropped_hash not in {r[1] for r in p.rows()}
        assert (claimant, dropped_hash) not in p.slices()
        assert p.stored_sums(thread_id) == p.recomputed_sums(thread_id)
        assert main._requeue_stale_extractions(p.db, p.queue) == 0


class TestSharedAcrossClaimants:
    def test_another_messages_occurrence_of_the_payload_is_untouched(self, tmp_path, monkeypatch):
        p = StalePipeline(tmp_path, monkeypatch)
        shared = [(b"shared-payload", "text/plain", f"{MARKER}.txt")]
        drop_from: set[str] = set()

        def rewrite(path, msg):
            if path in drop_from:
                msg.attachments = []

        p.parse_with(rewrite)
        first = p.add("first", shared)
        second = p.add("second", shared)
        p.drain()
        first_claimant = _claimant(p, first)
        second_claimant = _claimant(p, second)
        payload = hashlib.sha256(b"shared-payload").hexdigest()
        slices_before = p.slices()
        assert (first_claimant, payload) in slices_before
        assert (second_claimant, payload) in slices_before

        drop_from.add(first)
        p.reparse(first)

        assert [r[0] for r in p.rows()] == [second_claimant]
        after = p.slices()
        assert (first_claimant, payload) not in after
        assert after[(second_claimant, payload)] == slices_before[(second_claimant, payload)]
        assert len(p.fts_rowids()) == 1
        # Still used by the other message's occurrence.
        assert (
            p.db._conn.execute(
                "SELECT COUNT(*) FROM attachment_extractions WHERE attachment_id = ?",
                (payload,),
            ).fetchone()[0]
            == 1
        )
        for thread_id in p.thread_ids():
            assert p.stored_sums(thread_id) == p.recomputed_sums(thread_id)


class TestRollback:
    def test_a_failed_pass_keeps_the_stale_occurrence(self, tmp_path, monkeypatch):
        p = StalePipeline(tmp_path, monkeypatch)
        drop = {"on": False}

        def rewrite(_path, msg):
            if drop["on"]:
                msg.attachments = msg.attachments[:-1]

        p.parse_with(rewrite)
        path = p.add("rollback", _parts("rollback", 2))
        p.drain()
        thread_id = p.thread_ids()[0]
        rows = p.rows()
        fts = p.fts_rowids()
        slices = p.slices()
        sums = p.stored_sums(thread_id)
        real = Database.replace_thread_vector

        def failing(self, *args, **kwargs):
            raise sqlite3.OperationalError(f"disk I/O error {MARKER}")

        drop["on"] = True
        monkeypatch.setattr(Database, "replace_thread_vector", failing)
        p.queue.enqueue(path, REASON_REPARSE)
        p.drain()
        assert p.job(path)["last_error"] == "OperationalError"
        assert p.rows() == rows
        assert p.fts_rowids() == fts
        assert p.slices() == slices
        assert p.stored_sums(thread_id) == sums

        monkeypatch.setattr(Database, "replace_thread_vector", real)
        p.drain()
        assert p.job(path) is None
        assert len(p.rows()) == 1
        assert len(p.slices()) == 1
        assert p.stored_sums(thread_id) == p.recomputed_sums(thread_id)


class TestVisibility:
    def test_dropped_occurrences_are_counted_without_mail(self, tmp_path, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        p = StalePipeline(tmp_path, monkeypatch)
        drop = {"on": False}

        def rewrite(_path, msg):
            if drop["on"]:
                msg.attachments = msg.attachments[:1]

        p.parse_with(rewrite)
        path = p.add("visible", _parts("visible", 3))
        p.drain()
        attachment_indexing.attachment_outcomes.drain()
        caplog.clear()

        drop["on"] = True
        p.reparse(path)
        main._log_attachment_outcomes(force=True)
        lines = [r for r in caplog.records if r.getMessage().startswith("attachments n=")]
        # Two unique payloads lost their searchable text: a WARNING.
        assert [r.levelno for r in lines] == [logging.WARNING]
        assert " dropped=2 dropped_text=2 " in lines[0].getMessage()
        assert MARKER not in caplog.text
        errors = [r[0] for r in p.db._conn.execute("SELECT last_error FROM indexing_jobs")]
        assert not any(MARKER in (e or "") for e in errors)


class TestExtractionOff:
    def test_a_pass_with_extraction_off_removes_nothing(self, tmp_path, monkeypatch):
        """With extraction off a pass writes no occurrence rows, so it
        cannot tell a stale row from one it did not look at: it keeps
        them all."""
        p = StalePipeline(tmp_path, monkeypatch)
        path = p.add("off", _parts("off", 2))
        p.drain()
        rows, slices = p.rows(), p.slices()
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_EXTRACTION_ENABLED", False)
        p.parse_with(lambda _path, msg: setattr(msg, "attachments", msg.attachments[:1]))
        p.reparse(path)
        assert p.rows() == rows
        assert p.slices() == slices


class TestThreadAttachmentFlag:
    def test_dropping_the_last_attachment_clears_the_thread_flag(self, tmp_path, monkeypatch):
        """``upsert_thread`` only ever sets ``has_attachments``; the drop
        recomputes it from the thread's messages (Codex round 1 on #1385)."""
        p = StalePipeline(tmp_path, monkeypatch)
        drop = {"on": False}

        def rewrite(_path, msg):
            if drop["on"]:
                msg.attachments = []
                msg.has_attachments = False

        p.parse_with(rewrite)
        path = p.add("flag", _parts("flag", 1))
        p.drain()
        thread_id = p.thread_ids()[0]

        def flag() -> int:
            return p.db._conn.execute(
                "SELECT has_attachments FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()[0]

        assert flag() == 1
        drop["on"] = True
        p.reparse(path)
        assert p.rows() == []
        assert flag() == 0


def _extraction_rows(p: StalePipeline, payload: str) -> list[tuple]:
    return [
        tuple(r)
        for r in p.db._conn.execute(
            "SELECT extractor_module, extraction_status, extractor, extracted_text, "
            "extraction_error, extracted_at, ocr_pages_skipped, text_complete "
            "FROM attachment_extractions WHERE attachment_id = ? ORDER BY extractor_module",
            (payload,),
        )
    ]


class _PeerSetup:
    """A message ``first`` that drops its only attachment on a reparse
    and a new message ``second`` carrying the same payload."""

    def __init__(self, tmp_path, monkeypatch, content=b"peer-payload"):
        self.p = StalePipeline(tmp_path, monkeypatch)
        self.shared = [(content, "text/plain", f"{MARKER}.txt")]
        self.payload = hashlib.sha256(content).hexdigest()
        self.drop = {"on": False}

        def rewrite(path, msg):
            if self.drop["on"] and path.endswith("first.eml"):
                msg.attachments = []

        self.p.parse_with(rewrite)
        self.first = self.p.add("first", self.shared)
        self.p.drain()

    def run_batch(self, *, dropper_first=True):
        """One batch with the dropper and the peer; foreground rows are
        claimed (and committed) first."""
        self.drop["on"] = True
        if dropper_first:
            self.second = self.p.add("second", self.shared, reason=REASON_REPARSE)
            self.p.queue.enqueue(self.first, REASON_INITIAL_SCAN)
        else:
            self.second = self.p.add("second", self.shared)
            self.p.queue.enqueue(self.first, REASON_REPARSE)
        self.p.drain()


class TestBatchPeerRestoresItsCache:
    def test_a_peer_prepared_against_the_cached_row_gets_it_back_as_it_was(
        self, tmp_path, monkeypatch
    ):
        """Message A drops the last occurrence of a payload (the row is
        purged at once) and message B, prepared against that cached row,
        writes its occurrence after A's commit: B restores the row
        unchanged, ``extracted_at`` included, with no new extraction."""
        s = _PeerSetup(tmp_path, monkeypatch)
        before = _extraction_rows(s.p, s.payload)
        extractions = len(s.p.extractor.calls)
        s.run_batch()
        assert s.p.job(s.first) is None and s.p.job(s.second) is None
        assert len(s.p.extractor.calls) == extractions
        assert [r[1] for r in s.p.rows()] == [s.payload]
        assert _extraction_rows(s.p, s.payload) == before

    def test_a_peer_committed_before_the_dropper_keeps_the_row(self, tmp_path, monkeypatch):
        s = _PeerSetup(tmp_path, monkeypatch)
        before = _extraction_rows(s.p, s.payload)
        extractions = len(s.p.extractor.calls)
        s.run_batch(dropper_first=False)
        assert s.p.job(s.first) is None and s.p.job(s.second) is None
        assert len(s.p.extractor.calls) == extractions
        assert _extraction_rows(s.p, s.payload) == before

    def test_a_peer_that_extracts_again_keeps_its_own_result(self, tmp_path, monkeypatch):
        """A row without a completeness record is re-extracted once, so
        the peer is not a cache hit: it persists its fresh result and the
        purged row is not put back over it."""
        s = _PeerSetup(tmp_path, monkeypatch)
        s.p.db._conn.execute("UPDATE attachment_extractions SET text_complete = NULL")
        s.p.db._conn.commit()
        extractions = len(s.p.extractor.calls)
        s.run_batch()
        assert len(s.p.extractor.calls) == extractions + 1
        rows = _extraction_rows(s.p, s.payload)
        assert len(rows) == 1
        assert rows[0][7] == 1

    def test_a_non_success_row_is_restored_with_its_status_and_error(self, tmp_path, monkeypatch):
        s = _PeerSetup(tmp_path, monkeypatch, content=b"failing-payload")
        s.p.extractor.fail.add(b"failing-payload")
        s.p.db._conn.execute("DELETE FROM attachment_extractions")
        s.p.db._conn.commit()
        s.p.queue.enqueue(s.first, REASON_REPARSE)
        s.p.drain()
        before = _extraction_rows(s.p, s.payload)
        assert before and before[0][1] == "failed"
        extractions = len(s.p.extractor.calls)
        s.run_batch()
        assert len(s.p.extractor.calls) == extractions
        assert _extraction_rows(s.p, s.payload) == before

    def test_a_peer_whose_commit_fails_leaves_no_cache_row_and_extracts_on_retry(
        self, tmp_path, monkeypatch
    ):
        """A's drop purges the row at once, so a peer that fails to commit
        leaves no carrier-less extraction, and no state outlives the batch:
        the retry (as after a crash) misses the cache and extracts once."""
        s = _PeerSetup(tmp_path, monkeypatch, content=b"orphan-payload")
        extractions = len(s.p.extractor.calls)
        real = Database.set_body_complete

        def failing(self, claimant_id, *args, **kwargs):
            if claimant_id.startswith("second@"):
                raise sqlite3.OperationalError(f"disk I/O error {MARKER}")
            return real(self, claimant_id, *args, **kwargs)

        monkeypatch.setattr(Database, "set_body_complete", failing)
        s.run_batch()
        assert s.p.job(s.first) is None
        assert s.p.job(s.second)["last_error"] == "OperationalError"
        assert s.p.rows() == []
        assert _extraction_rows(s.p, s.payload) == []

        monkeypatch.setattr(Database, "set_body_complete", real)
        s.p.drain()
        assert s.p.job(s.second) is None
        assert len(s.p.extractor.calls) == extractions + 1
        assert [r[1] for r in s.p.rows()] == [s.payload]
        assert len(_extraction_rows(s.p, s.payload)) == 1

    def test_a_batch_without_drops_retains_no_purged_rows(self, tmp_path, monkeypatch):
        p = StalePipeline(tmp_path, monkeypatch)
        shared = [(b"hit-payload", "text/plain", f"{MARKER}.txt")]
        p.add("one", shared)
        p.drain()
        seen: list[dict] = []
        real = Database.delete_attachment_occurrences

        def spy(self, claimant_id, occurrence_ids, purged):
            seen.append(purged)
            return real(self, claimant_id, occurrence_ids, purged)

        monkeypatch.setattr(Database, "delete_attachment_occurrences", spy)
        for name in ("two", "three", "four"):
            p.add(name, shared)
        p.drain()
        assert seen and all(d == {} for d in seen)


class TestRestoreAttachmentExtraction:
    def test_restores_a_purged_row_as_it_was_and_never_overwrites(self, tmp_path):
        db = Database(tmp_path / "mail.db")
        db.store_attachment_extraction(
            attachment_id="a" * 64,
            extractor_module="text",
            extraction_status="success",
            extractor="text@1",
            extracted_text="synthetic",
            extraction_error=None,
            ocr_pages_skipped=2,
            text_complete=True,
        )
        row = db._conn.execute("SELECT * FROM attachment_extractions").fetchone()
        db._conn.execute("DELETE FROM attachment_extractions")
        db._conn.commit()
        db.restore_attachment_extraction(row)
        assert tuple(db._conn.execute("SELECT * FROM attachment_extractions").fetchone()) == tuple(
            row
        )
        db.store_attachment_extraction(
            attachment_id="a" * 64,
            extractor_module="text",
            extraction_status="empty",
            extractor="text@2",
            extracted_text=None,
            extraction_error=None,
        )
        newer = tuple(db._conn.execute("SELECT * FROM attachment_extractions").fetchone())
        db.restore_attachment_extraction(row)
        assert tuple(db._conn.execute("SELECT * FROM attachment_extractions").fetchone()) == newer


class TestSharedSliceAcrossModules:
    def test_a_stale_modules_chunks_leave_a_kept_payload_slice(self, tmp_path, monkeypatch):
        """Two occurrences of one payload ran different extractors, so the
        slice holds both modules' chunks. A continuation pass where the
        parse drops the second keeps the first as complete; the slice is
        rebuilt from the surviving module only (Codex round 1 on #1385)."""
        p = StalePipeline(tmp_path, monkeypatch)
        payload = b"module-payload"
        parts = [
            (payload, "text/plain", f"{MARKER}-a.txt"),
            (b"other-payload", "text/plain", f"{MARKER}-b.txt"),
            (payload, "application/pdf", f"{MARKER}-c.pdf"),
        ]
        drop = {"on": False}

        def rewrite(_path, msg):
            if drop["on"]:
                msg.attachments = msg.attachments[:2]

        p.parse_with(rewrite)
        path = p.add("modules", parts)
        p.drain()
        claimant = _claimant(p, path)
        shared_hash = hashlib.sha256(payload).hexdigest()

        def slice_texts() -> list[str]:
            return [
                r[0]
                for r in p.db._conn.execute(
                    "SELECT text FROM message_chunks WHERE claimant_id = ? AND attachment_id = ?",
                    (claimant, shared_hash),
                )
            ]

        assert any("application/pdf" in t for t in slice_texts())
        assert any("under text/plain" in t for t in slice_texts())
        # Every result is stale and the budget admits one dispatch per
        # pass: the first copy resolves, the others are deferred.
        p.db._conn.execute("DELETE FROM attachment_extractions")
        p.db._conn.execute("UPDATE attachments SET text_complete = NULL")
        p.db._conn.commit()
        monkeypatch.setattr(
            main, "ExtractionBudget", functools.partial(ExtractionBudget, max_launches=1)
        )
        p.queue.enqueue(path, REASON_REPARSE)
        p.drain()
        assert p.deferred() == 2

        drop["on"] = True
        while p.job(path) is not None:
            p.drain()
        assert len(p.rows()) == 2
        texts = slice_texts()
        assert texts
        assert not any("application/pdf" in t for t in texts)
        thread_id = p.thread_ids()[0]
        assert p.stored_sums(thread_id) == p.recomputed_sums(thread_id)


class TestPartialModuleRowsAndPeerFlag:
    def test_only_the_unused_module_row_of_a_shared_payload_is_purged(self, tmp_path, monkeypatch):
        """A parsed occurrence keeps its (payload, module) cache row; the
        stale occurrence's row of another module for the same payload
        goes (Codex round 2 on #1385)."""
        p = StalePipeline(tmp_path, monkeypatch)
        payload = b"two-module-payload"
        drop = {"on": False}

        def rewrite(_path, msg):
            if drop["on"]:
                msg.attachments = msg.attachments[:1]

        p.parse_with(rewrite)
        path = p.add(
            "modrows",
            [
                (payload, "text/plain", f"{MARKER}-a.txt"),
                (payload, "application/pdf", f"{MARKER}-b.pdf"),
            ],
        )
        p.drain()
        shared_hash = hashlib.sha256(payload).hexdigest()

        def modules() -> list[str]:
            return sorted(
                r[0]
                for r in p.db._conn.execute(
                    "SELECT extractor_module FROM attachment_extractions WHERE attachment_id = ?",
                    (shared_hash,),
                )
            )

        assert len(modules()) == 2
        drop["on"] = True
        p.reparse(path)
        assert modules() == [
            p.db._conn.execute("SELECT extractor_module FROM attachments").fetchone()[0]
        ]

    def test_a_peers_unfinished_attachments_keep_the_thread_flag(self, tmp_path, monkeypatch):
        """Two messages of one thread drop their attachments in one
        batch; after the first commit the thread flag stays true because
        the second's occurrence row is still stored (Codex round 2 on #1385)."""
        p = StalePipeline(tmp_path, monkeypatch)
        drop = {"on": False}

        def rewrite(_path, msg):
            if drop["on"]:
                msg.attachments = []
                msg.has_attachments = False

        p.parse_with(rewrite)
        first = p.add("flag-a", _parts("flag-a", 1))
        second = p.add("flag-b", _parts("flag-b", 1))
        p.drain()
        # One thread holds both messages.
        p.db._conn.execute(
            "UPDATE message_thread_map SET thread_id = (SELECT MIN(thread_id) FROM threads)"
        )
        p.db._conn.commit()
        thread_id = p.db._conn.execute(
            "SELECT thread_id FROM message_thread_map LIMIT 1"
        ).fetchone()[0]
        drop["on"] = True
        p.db._conn.execute("UPDATE messages SET has_attachments = 0")
        p.db._conn.commit()
        claimant = _claimant(p, first)
        occurrence = p.db._conn.execute(
            "SELECT attachment_occurrence_id FROM attachments WHERE claimant_id = ?",
            (claimant,),
        ).fetchone()[0]
        p.db.delete_attachment_occurrences(claimant, [occurrence], {})
        flag = p.db._conn.execute(
            "SELECT has_attachments FROM threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()[0]
        assert flag == 1, "the peer's stored occurrence keeps the flag"
        assert p.rows() and _claimant(p, second) in {r[0] for r in p.rows()}
