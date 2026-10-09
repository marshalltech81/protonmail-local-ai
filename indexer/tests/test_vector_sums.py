"""#1356: the exact running sum of each thread's chunk vectors.

The sums replace the thread-wide chunk-vector reads of Phase 1, Phase 2c
and the reaper. These tests pin the codec and the derivation rule, then
check that every write keeps each stored sum byte-equal to a full
recompute, and count the thread-wide reads each step does.
"""

from __future__ import annotations

import logging
import math
import random
import struct
from fractions import Fraction

import pytest
import sqlite_vec
from src import vector_sums
from src.chunker import MessageChunk, l2_normalize
from src.database import EMBEDDING_DIM, Database

from tests.conftest import make_message, make_thread

MARKER = "SYNTHETIC_VECTOR_SUM_MARKER"
# At least 20 tokens a chunk, the density real mail has.
_TEXT = (
    f"{MARKER} the quarterly synthetic figures show steady growth across every "
    "region with costs held flat and margins improving over the whole year"
)


def _random_unit(rng: random.Random) -> list[float]:
    vec = [rng.gauss(0.0, 1.0) for _ in range(EMBEDDING_DIM)]
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec]


def _chunk(chunk_id: str, *, attachment: bool = False, index: int = 0) -> MessageChunk:
    return MessageChunk(
        chunk_id=chunk_id,
        chunk_index=index,
        text=f"{_TEXT} {chunk_id}",
        char_start=0,
        char_end=len(_TEXT),
        token_est=25,
        kind="attachment" if attachment else "body",
    )


def _add_message(db: Database, message_id: str, thread_id: str) -> None:
    msg = make_message(message_id=message_id, filepath=f"/maildir/INBOX/cur/{message_id}")
    db.upsert_thread(make_thread(messages=[msg], thread_id=thread_id), [0.0] * EMBEDDING_DIM)


def _write(db, claimant_id, thread_id, chunk_ids, vectors, *, attachment_id=None, **kwargs):
    chunks = [
        _chunk(cid, attachment=attachment_id is not None, index=i)
        for i, cid in enumerate(chunk_ids)
    ]
    return db.replace_message_chunks(
        claimant_id=claimant_id,
        thread_id=thread_id,
        chunks=chunks,
        embeddings_by_chunk_id={cid: vectors[cid] for cid in chunk_ids if cid in vectors},
        attachment_id=attachment_id,
        **kwargs,
    )


def _stored(db: Database, thread_id: str):
    row = db._conn.execute(
        "SELECT count, encoding_version, sum FROM thread_vector_sums WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    return None if row is None else tuple(row)


def _recomputed(db: Database, thread_id: str):
    """Independent recompute: every chunk vector of the thread, decoded
    exactly through ``Fraction``, not through the code under test."""
    total = [Fraction(0)] * EMBEDDING_DIM
    count = 0
    for (blob,) in db._conn.execute(
        "SELECT v.embedding FROM message_chunks c "
        "JOIN message_chunks_vec v ON v.chunk_id = c.chunk_id WHERE c.thread_id = ?",
        (thread_id,),
    ):
        values = struct.unpack(f"{EMBEDDING_DIM}f", blob)
        total = [t + Fraction(x) for t, x in zip(total, values, strict=True)]
        count += 1
    units = [int(t * 2**149) for t in total]
    assert all(Fraction(u, 2**149) == t for u, t in zip(units, total, strict=True))
    return (count, vector_sums.ENCODING_VERSION, vector_sums.encode(units))


def _assert_exact(db: Database, *thread_ids: str) -> None:
    for thread_id in thread_ids:
        assert _stored(db, thread_id) == _recomputed(db, thread_id), thread_id


def _thread_vec(db: Database, thread_id: str) -> bytes:
    return db._conn.execute(
        "SELECT embedding FROM threads_vec WHERE thread_id = ?", (thread_id,)
    ).fetchone()[0]


def _make_legacy(db: Database, *thread_ids: str) -> None:
    """A thread from before v10: no sums row."""
    for thread_id in thread_ids:
        db._conn.execute("DELETE FROM thread_vector_sums WHERE thread_id = ?", (thread_id,))
    db._conn.commit()


class ThreadReads:
    """Counts the thread-wide chunk-vector reads (``_compute_thread_sums``
    calls and the vectors they read) and every chunk vector decoded."""

    def __init__(self, monkeypatch):
        self.calls = 0
        self.rows = 0
        self.decoded = 0
        real = Database._compute_thread_sums
        real_units = vector_sums.vector_units

        def compute(db_self, thread_id):
            total, count = real(db_self, thread_id)
            self.calls += 1
            self.rows += count
            return total, count

        def units(blob):
            self.decoded += 1
            return real_units(blob)

        monkeypatch.setattr(Database, "_compute_thread_sums", compute)
        monkeypatch.setattr(vector_sums, "vector_units", units)

    def take(self) -> tuple[int, int, int]:
        counts = (self.calls, self.rows, self.decoded)
        self.calls = self.rows = self.decoded = 0
        return counts


# ---------------------------------------------------------------------------
# Codec and derivation rule
# ---------------------------------------------------------------------------


class TestCodec:
    def test_units_are_exact_for_every_float32_class(self):
        values = [
            0.0,
            -0.0,
            1.0,
            -1.0,
            0.1,
            1e-45,  # the smallest subnormal
            -1.1754942e-38,  # the largest subnormal
            1.17549435e-38,  # the smallest normal
            3.4028235e38,  # the largest finite
            -3.4028235e38,
        ]
        blob = struct.pack(f"{len(values)}f", *values)
        stored = struct.unpack(f"{len(values)}f", blob)
        units = vector_sums.vector_units(blob)
        assert [Fraction(u, 2**149) for u in units] == [Fraction(x) for x in stored]

    @pytest.mark.parametrize("bad", [math.inf, -math.inf, math.nan])
    def test_a_non_finite_component_is_refused(self, bad):
        with pytest.raises(ValueError, match="non-finite"):
            vector_sums.vector_units(struct.pack("2f", 0.5, bad))

    def test_encoding_round_trips_and_is_canonical(self):
        rng = random.Random(7)
        cases = [
            [0] * 8,
            [1, -1, 0, 2**300, -(2**300) + 1, 3 << 149, -(5 << 7), 0],
            [rng.randrange(-(2**200), 2**200) << rng.randrange(0, 150) for _ in range(8)],
        ]
        for total in cases:
            blob = vector_sums.encode(total)
            assert vector_sums.decode(blob, 8) == total
            assert vector_sums.encode(vector_sums.decode(blob, 8)) == blob
        assert vector_sums.encode([0] * 8) == b""
        assert vector_sums.decode(b"", 3) == [0, 0, 0]

    def test_a_malformed_blob_is_refused(self):
        blob = vector_sums.encode([5, -3, 7])
        with pytest.raises(ValueError, match="truncated"):
            vector_sums.decode(blob[:-1], 3)
        with pytest.raises(ValueError, match="truncated"):
            vector_sums.decode(blob[:1], 3)
        with pytest.raises(ValueError, match="dimension"):
            vector_sums.decode(blob, 4)

    def test_mismatched_dimensions_are_refused(self):
        with pytest.raises(ValueError, match="dimension"):
            vector_sums.add_units([1, 2], [1, 2, 3])


class TestDerivationRule:
    def test_mean_is_the_exact_quotient_rounded_once(self):
        rng = random.Random(3)
        for _ in range(200):
            count = rng.randrange(1, 10_000)
            total = [rng.randrange(-(2**160), 2**160) for _ in range(4)]
            got = vector_sums.mean(total, count)
            assert got == [float(Fraction(t, count << 149)) for t in total]

    def test_a_tie_rounds_to_even(self):
        # (2**53 + 1) and (2**53 + 3) are halfway between two float64s:
        # the first rounds down to the even 2**53, the second up to the
        # even 2**53 + 4.
        total = [(2**53 + 1) << 149, (2**53 + 3) << 149]
        assert vector_sums.mean(total, 1) == [2.0**53, 2.0**53 + 4]

    def test_an_empty_count_is_refused(self):
        with pytest.raises(ValueError, match="empty"):
            vector_sums.thread_vector([0, 0], 0)

    def test_matches_the_float64_mean_it_replaces(self):
        """The thread vector stored before #1356 was the sequential float64
        mean of the stored chunk vectors, normalized, as float32. The
        exact rule stores the same bytes (it can differ only where that
        sum lost a bit and the float32 rounding then sits on its edge)."""
        rng = random.Random(11)
        same = 0
        for n in (1, 2, 7, 40):
            blobs = [sqlite_vec.serialize_float32(_random_unit(rng)) for _ in range(n)]
            vectors = [list(struct.unpack(f"{EMBEDDING_DIM}f", b)) for b in blobs]
            old_sums = [0.0] * EMBEDDING_DIM
            for vec in vectors:
                for i, value in enumerate(vec):
                    old_sums[i] += value
            old = sqlite_vec.serialize_float32(l2_normalize([s / n for s in old_sums]))
            total = [0] * EMBEDDING_DIM
            for blob in blobs:
                total = vector_sums.add_units(total, vector_sums.vector_units(blob))
            new = sqlite_vec.serialize_float32(vector_sums.thread_vector(total, n))
            same += old == new
            old_f = struct.unpack(f"{EMBEDDING_DIM}f", old)
            new_f = struct.unpack(f"{EMBEDDING_DIM}f", new)
            assert max(abs(a - b) for a, b in zip(old_f, new_f, strict=True)) < 1e-9
        assert same == 4


# ---------------------------------------------------------------------------
# Every write keeps the sums exact
# ---------------------------------------------------------------------------


class TestMutationSites:
    def test_a_new_thread_starts_with_an_empty_sum(self, db):
        _add_message(db, "m1@x", "t1")
        assert _stored(db, "t1") == (0, vector_sums.ENCODING_VERSION, b"")

    def test_inserting_deleting_and_keeping_chunks(self, db, monkeypatch):
        rng = random.Random(1)
        vecs = {f"c{i}": _random_unit(rng) for i in range(5)}
        _add_message(db, "m1@x", "t1")
        reads = ThreadReads(monkeypatch)
        _write(db, "m1@x", "t1", ["c0", "c1", "c2"], vecs)
        _assert_exact(db, "t1")
        assert reads.take() == (0, 0, 3)
        # c0 kept, c1 and c2 deleted, c3 inserted.
        _write(db, "m1@x", "t1", ["c0", "c3"], vecs)
        _assert_exact(db, "t1")
        assert reads.take() == (0, 0, 3)
        # Nothing changes: nothing is read.
        _write(db, "m1@x", "t1", ["c0", "c3"], vecs)
        assert reads.take() == (0, 0, 0)
        # delete_missing=False only adds.
        _write(db, "m1@x", "t1", ["c4"], vecs, delete_missing=False)
        _assert_exact(db, "t1")
        assert _stored(db, "t1")[0] == 3

    def test_slice_writes_and_slice_deletion(self, db):
        rng = random.Random(2)
        vecs = {f"a{i}": _random_unit(rng) for i in range(4)}
        vecs["b0"] = _random_unit(rng)
        _add_message(db, "m1@x", "t1")
        _write(db, "m1@x", "t1", ["b0"], vecs)
        _write(db, "m1@x", "t1", ["a0", "a1"], vecs, attachment_id="att1")
        _write(db, "m1@x", "t1", ["a2", "a3"], vecs, attachment_id="att2")
        _assert_exact(db, "t1")
        _write(db, "m1@x", "t1", [], vecs, attachment_id="att1")
        _assert_exact(db, "t1")
        assert _stored(db, "t1")[0] == 3

    def test_a_reap_subtracts_the_reaped_messages(self, db, monkeypatch):
        rng = random.Random(4)
        vecs = {f"c{i}": _random_unit(rng) for i in range(4)}
        _add_message(db, "m1@x", "t1")
        _add_message(db, "m2@x", "t1")
        _write(db, "m1@x", "t1", ["c0", "c1"], vecs)
        _write(db, "m2@x", "t1", ["c2", "c3"], vecs, attachment_id="att")
        db.add_pending_deletion("/maildir/INBOX/cur/m2@x", "m2@x", "t1")
        reads = ThreadReads(monkeypatch)
        survivor = make_message(message_id="m1@x", filepath="/maildir/INBOX/cur/m1@x")
        removed = db.reap_thread_messages(
            make_thread(messages=[survivor], thread_id="t1"), None, ["m2@x"]
        )
        assert removed == ["/maildir/INBOX/cur/m2@x"]
        _assert_exact(db, "t1")
        # The reaped vectors only: no thread-wide read.
        assert reads.take() == (0, 0, 2)
        count, _, blob = _stored(db, "t1")
        expected = vector_sums.thread_vector(vector_sums.decode(blob, EMBEDDING_DIM), count)
        assert _thread_vec(db, "t1") == sqlite_vec.serialize_float32(expected)

    def test_a_reap_with_no_surviving_chunk_changes_nothing(self, db):
        rng = random.Random(5)
        vecs = {"c0": _random_unit(rng)}
        _add_message(db, "m1@x", "t1")
        _add_message(db, "m2@x", "t1")
        _write(db, "m2@x", "t1", ["c0"], vecs)
        db.add_pending_deletion("/maildir/INBOX/cur/m2@x", "m2@x", "t1")
        before = (_stored(db, "t1"), _thread_vec(db, "t1"))
        survivor = make_message(message_id="m1@x", filepath="/maildir/INBOX/cur/m1@x")
        assert (
            db.reap_thread_messages(
                make_thread(messages=[survivor], thread_id="t1"), None, ["m2@x"]
            )
            is None
        )
        assert (_stored(db, "t1"), _thread_vec(db, "t1")) == before
        assert db.get_thread_messages("t1")

    def test_deleting_the_thread_drops_its_sum(self, db):
        rng = random.Random(6)
        _add_message(db, "m1@x", "t1")
        _write(db, "m1@x", "t1", ["c0"], {"c0": _random_unit(rng)})
        db.add_pending_deletion("/maildir/INBOX/cur/m1@x", "m1@x", "t1")
        assert db.delete_thread_completely("t1")
        assert _stored(db, "t1") is None

    def test_a_message_moved_to_another_thread(self, db):
        """Moving a message in the map does not move its chunk rows, so
        no sum changes; chunks written for it afterwards count in the new
        thread, and deleting the old ones subtracts them from the old."""
        rng = random.Random(8)
        vecs = {f"c{i}": _random_unit(rng) for i in range(3)}
        _add_message(db, "m1@x", "t1")
        _add_message(db, "m2@x", "t2")
        _write(db, "m1@x", "t1", ["c0", "c1"], vecs)
        before = _stored(db, "t1")
        msg = make_message(message_id="m1@x", filepath="/maildir/INBOX/cur/m1@x")
        db.upsert_thread(make_thread(messages=[msg], thread_id="t2"), [0.0] * EMBEDDING_DIM)
        assert (
            db._conn.execute(
                "SELECT thread_id FROM message_thread_map WHERE claimant_id = 'm1@x'"
            ).fetchone()[0]
            == "t2"
        )
        assert _stored(db, "t1") == before
        _assert_exact(db, "t1", "t2")
        _write(db, "m1@x", "t2", ["c1", "c2"], vecs)
        _assert_exact(db, "t1", "t2")
        assert (_stored(db, "t1")[0], _stored(db, "t2")[0]) == (1, 1)

    def test_a_failed_write_rolls_the_sum_back(self, db):
        rng = random.Random(9)
        vecs = {f"c{i}": _random_unit(rng) for i in range(3)}
        _add_message(db, "m1@x", "t1")
        _write(db, "m1@x", "t1", ["c0"], vecs)
        before = _stored(db, "t1")
        # The second chunk has no embedding: the call fails part-way.
        with pytest.raises(ValueError, match="missing embedding"):
            _write(db, "m1@x", "t1", ["c1", "c2"], {"c1": vecs["c1"]})
        assert _stored(db, "t1") == before
        # A failure later in the caller's transaction rolls it back too.
        with pytest.raises(RuntimeError), db.transaction():
            _write(db, "m1@x", "t1", ["c1"], vecs)
            raise RuntimeError("later step failed")
        assert _stored(db, "t1") == before
        _assert_exact(db, "t1")

    def test_randomized_writes_keep_every_sum_byte_equal(self, db):
        rng = random.Random(1356)
        threads = ["t0", "t1", "t2"]
        messages = {f"m{i}@x": threads[i % 3] for i in range(6)}
        for mid, tid in messages.items():
            _add_message(db, mid, tid)
        pool: dict[str, list[float]] = {}
        slices = [None, "att1", "att2"]
        for step in range(120):
            mid = rng.choice(list(messages))
            tid = messages[mid]
            slice_id = rng.choice(slices)
            op = rng.random()
            if op < 0.8:
                # Chunk IDs are global: each (message, slice) has its own.
                ids = rng.sample([f"{mid}:{slice_id}:{k}" for k in range(5)], rng.randrange(0, 5))
                for cid in ids:
                    pool.setdefault(cid, _random_unit(rng))
                _write(
                    db,
                    mid,
                    tid,
                    ids,
                    pool,
                    attachment_id=slice_id,
                    delete_missing=rng.random() < 0.7,
                )
            elif op < 0.9:
                _make_legacy(db, tid)
            else:
                with db.transaction():
                    db._delete_chunks_for_message(db._conn.cursor(), mid)
            _assert_exact(db, *(t for t in threads if _stored(db, t) is not None))
        for tid in threads:
            db.thread_chunk_mean(tid)
        _assert_exact(db, *threads)


# ---------------------------------------------------------------------------
# Inline fill, backfill and the check
# ---------------------------------------------------------------------------


def _legacy_threads(db, n, per_thread, rng):
    vecs = {}
    for t in range(n):
        tid = f"t{t}"
        _add_message(db, f"m{t}@x", tid)
        ids = [f"c{t}_{k}" for k in range(per_thread)]
        vecs.update({cid: _random_unit(rng) for cid in ids})
        _write(db, f"m{t}@x", tid, ids, vecs)
    _make_legacy(db, *(f"t{t}" for t in range(n)))
    return vecs


class TestInlineFill:
    def test_the_first_write_fills_once_then_never_reads(self, db, monkeypatch):
        rng = random.Random(12)
        vecs = _legacy_threads(db, 1, 6, rng)
        vecs["new"] = _random_unit(rng)
        reads = ThreadReads(monkeypatch)
        _write(db, "m0@x", "t0", ["new"], vecs, attachment_id="att")
        assert reads.take()[:2] == (1, 7)
        _assert_exact(db, "t0")
        _write(db, "m0@x", "t0", [], vecs, attachment_id="att")
        assert reads.take()[:2] == (0, 0)
        _assert_exact(db, "t0")

    def test_phase1_and_phase2c_reads_fill_once(self, db, monkeypatch):
        rng = random.Random(13)
        _legacy_threads(db, 1, 4, rng)
        reads = ThreadReads(monkeypatch)
        mean, prior = db.get_phase1_seed_state("t0")
        assert prior is None and mean is not None
        assert reads.take()[:2] == (1, 4)
        assert db.get_phase1_seed_state("t0")[0] == mean
        assert db.thread_chunk_mean("t0") == mean
        assert reads.take() == (0, 0, 0)
        _assert_exact(db, "t0")

    def test_a_chunkless_thread_keeps_its_prior_vector(self, db):
        _add_message(db, "m1@x", "t1")
        db.replace_thread_vector("t1", [1.0] + [0.0] * (EMBEDDING_DIM - 1))
        mean, prior = db.get_phase1_seed_state("t1")
        assert mean is None and prior[0] == 1.0
        assert db.get_phase1_seed_state("nope") == (None, None)

    def test_an_unreadable_row_is_recomputed(self, db, caplog):
        rng = random.Random(14)
        _legacy_threads(db, 1, 2, rng)
        db.thread_chunk_mean("t0")
        db._conn.execute("UPDATE thread_vector_sums SET encoding_version = 99")
        db._conn.commit()
        caplog.set_level(logging.WARNING)
        db.thread_chunk_mean("t0")
        _assert_exact(db, "t0")
        db._conn.execute("UPDATE thread_vector_sums SET sum = x'0102'")
        db._conn.commit()
        db.thread_chunk_mean("t0")
        _assert_exact(db, "t0")
        assert caplog.text.count("an unreadable row is recomputed") == 2
        assert MARKER not in caplog.text

    def test_a_sum_that_disagrees_with_its_chunks_is_recomputed(self, db, caplog):
        rng = random.Random(15)
        vecs = {"c0": _random_unit(rng)}
        _add_message(db, "m1@x", "t1")
        # A stored sum that missed the chunk it holds: deleting the chunk
        # would take the count below zero.
        _write(db, "m1@x", "t1", ["c0"], vecs)
        db._conn.execute("UPDATE thread_vector_sums SET count = 0, sum = x''")
        db._conn.commit()
        caplog.set_level(logging.WARNING)
        _write(db, "m1@x", "t1", [], vecs)
        _assert_exact(db, "t1")
        assert "disagreed with its chunks" in caplog.text
        assert MARKER not in caplog.text


class TestBackfill:
    def test_bounded_batches_resume_after_an_interruption(self, db, monkeypatch):
        rng = random.Random(16)
        _legacy_threads(db, 5, 2, rng)
        reads = ThreadReads(monkeypatch)
        assert db.fill_missing_thread_vector_sums(max_threads=2, max_rows=100) == (2, 4, 3)
        assert reads.take()[:2] == (2, 4)
        # Interrupted on its second thread: the first stays filled.
        real = Database._compute_thread_sums
        calls = []

        def failing(db_self, thread_id):
            calls.append(thread_id)
            if len(calls) == 2:
                raise RuntimeError("synthetic interruption")
            return real(db_self, thread_id)

        monkeypatch.setattr(Database, "_compute_thread_sums", failing)
        with pytest.raises(RuntimeError):
            db.fill_missing_thread_vector_sums(max_threads=10, max_rows=100)
        missing = [
            r[0]
            for r in db._conn.execute(
                "SELECT thread_id FROM threads t WHERE NOT EXISTS "
                "(SELECT 1 FROM thread_vector_sums s WHERE s.thread_id = t.thread_id)"
            )
        ]
        assert len(missing) == 2
        monkeypatch.setattr(Database, "_compute_thread_sums", real)
        reads = ThreadReads(monkeypatch)
        assert db.fill_missing_thread_vector_sums(max_threads=10, max_rows=100) == (2, 4, 0)
        # Each remaining thread read once; filled threads not again.
        assert reads.take()[:2] == (2, 4)
        assert db.fill_missing_thread_vector_sums(max_threads=10, max_rows=100) == (0, 0, 0)
        _assert_exact(db, *(f"t{t}" for t in range(5)))

    def test_the_row_bound_stops_after_a_whole_thread(self, db):
        rng = random.Random(17)
        _legacy_threads(db, 3, 3, rng)
        # A thread is read whole even past the bound; the batch then stops.
        assert db.fill_missing_thread_vector_sums(max_threads=10, max_rows=2) == (1, 3, 2)


class TestCheck:
    def test_a_matching_sum_is_left_alone(self, db):
        rng = random.Random(18)
        _legacy_threads(db, 1, 3, rng)
        db.thread_chunk_mean("t0")
        before = _thread_vec(db, "t0")
        assert db.reconcile_thread_vector_sums("t0") == (False, 3)
        assert _thread_vec(db, "t0") == before

    @pytest.mark.parametrize(
        "damage",
        [
            "UPDATE thread_vector_sums SET count = count + 1",
            "UPDATE thread_vector_sums SET sum = x''",
            "UPDATE thread_vector_sums SET encoding_version = 99",
            "DELETE FROM thread_vector_sums",
        ],
    )
    def test_a_differing_sum_is_repaired_with_the_thread_vector(self, db, damage):
        rng = random.Random(19)
        _legacy_threads(db, 1, 3, rng)
        db.thread_chunk_mean("t0")
        db.replace_thread_vector("t0", [1.0] + [0.0] * (EMBEDDING_DIM - 1))
        db._conn.execute(damage)
        db._conn.commit()
        assert db.reconcile_thread_vector_sums("t0") == (True, 3)
        _assert_exact(db, "t0")
        count, _, blob = _stored(db, "t0")
        expected = vector_sums.thread_vector(vector_sums.decode(blob, EMBEDDING_DIM), count)
        assert _thread_vec(db, "t0") == sqlite_vec.serialize_float32(expected)

    def test_a_chunkless_thread_keeps_its_vector(self, db):
        _add_message(db, "m1@x", "t1")
        fallback = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
        db.replace_thread_vector("t1", fallback)
        db._conn.execute("UPDATE thread_vector_sums SET count = 2")
        db._conn.commit()
        assert db.reconcile_thread_vector_sums("t1") == (True, 0)
        assert _stored(db, "t1")[0] == 0
        assert _thread_vec(db, "t1") == sqlite_vec.serialize_float32(fallback)
        assert db.reconcile_thread_vector_sums("gone") == (False, 0)

    def test_the_repair_rolls_back_on_failure(self, db, monkeypatch):
        rng = random.Random(20)
        _legacy_threads(db, 1, 2, rng)
        db.thread_chunk_mean("t0")
        db._conn.execute("UPDATE thread_vector_sums SET count = 9")
        db._conn.commit()
        before = (_stored(db, "t0"), _thread_vec(db, "t0"))

        def fail(*_args):
            raise RuntimeError("synthetic")

        monkeypatch.setattr(Database, "_write_thread_vector", staticmethod(fail))
        with pytest.raises(RuntimeError):
            db.reconcile_thread_vector_sums("t0")
        assert (_stored(db, "t0"), _thread_vec(db, "t0")) == before

    def test_batches_walk_every_thread_and_wrap(self, db):
        rng = random.Random(21)
        _legacy_threads(db, 5, 1, rng)
        db.fill_missing_thread_vector_sums(max_threads=10, max_rows=100)
        seen = []
        cursor = None
        for _ in range(3):
            checked, repaired, rows, cursor = db.reconcile_thread_vector_sums_batch(
                after=cursor, max_threads=2, max_rows=100
            )
            seen.append((checked, repaired, rows, cursor))
        assert seen == [(2, 0, 2, "t1"), (2, 0, 2, "t3"), (1, 0, 1, None)]
        assert db.reconcile_thread_vector_sums_batch(after=None, max_threads=10, max_rows=1)[
            :3
        ] == (1, 0, 1)


# ---------------------------------------------------------------------------
# Work at the part cap
# ---------------------------------------------------------------------------


class TestWorkAtThePartCap:
    def test_a_continuation_reads_no_thread_vector(self, db, monkeypatch):
        """A thread holding 9,990 chunk vectors (the part cap's scale, at
        realistic text density): once filled, a continuation pass's chunk
        write and thread mean read no chunk vector of the thread; a thread
        from before v10 is read once, on its first touch, and never again."""
        parts = 9_990
        _add_message(db, "big@x", "big")
        unit = [0.0] * EMBEDDING_DIM
        rng = random.Random(22)
        vectors = {}
        with db.transaction():
            for start in range(0, parts, 333):
                ids = [f"p{n}" for n in range(start, min(start + 333, parts))]
                for cid in ids:
                    vec = unit.copy()
                    vec[rng.randrange(EMBEDDING_DIM)] = 1.0
                    vectors[cid] = vec
                _write(db, "big@x", "big", ids, vectors, attachment_id=f"att{start}")
        _assert_exact(db, "big")
        reads = ThreadReads(monkeypatch)
        vectors["next"] = _random_unit(rng)
        with db.transaction():
            _write(db, "big@x", "big", ["next"], vectors, attachment_id="att-next")
            mean = db.thread_chunk_mean("big")
        # Only the inserted vector is decoded.
        assert reads.take() == (0, 0, 1)
        assert mean is not None
        _make_legacy(db, "big")
        assert db.thread_chunk_mean("big") == mean
        assert reads.take()[:2] == (1, parts + 1)
        assert db.thread_chunk_mean("big") == mean
        assert reads.take() == (0, 0, 0)
