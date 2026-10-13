"""FTS word predicates preserve semantics and bound repeated index work."""

import sqlite3
import time

import pytest
from src.lib.predicates import Leaf, compile_leaves, normalize_where
from src.lib.sqlite import Database

from tests.conftest import _insert_attachment, _insert_chunk, _insert_message, claimant_of
from tests.test_sqlite import _open_built_db_conn
from tests.test_where import _where

_TERMS = tuple(f"word{i}" for i in range(16))
_TEXT = " ".join([*_TERMS, *(f"padding{i % 100}" for i in range(234))])


def _dense_db(tmp_path, messages, analyze=False, body_chunks=10):
    conn, path = _open_built_db_conn(tmp_path, f"words-{messages}.db")
    for i in range(messages):
        mid = f"m{i}"
        _insert_message(conn, message_id=mid, thread_id=mid, sent_at=None, has_attachments=True)
        _insert_attachment(
            conn, message_id=mid, thread_id=mid, attachment_id=f"a{i}", filename="synthetic.pdf"
        )
    conn.execute("UPDATE attachments SET text_complete = 1")
    rows = []
    for i in range(messages):
        for j in range(20):
            rowid = i * 20 + j + 1
            attachment = None if j < body_chunks else f"a{i}"
            rows.append(
                (
                    f"c{rowid}",
                    claimant_of(f"m{i}"),
                    f"m{i}",
                    j,
                    _TEXT,
                    0,
                    len(_TEXT),
                    250,
                    "",
                    rowid,
                    attachment,
                    "attachment" if attachment else "body",
                )
            )
    conn.executemany("INSERT INTO message_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.executemany(
        "INSERT INTO message_chunks_fts(rowid, text) VALUES (?, ?)", [(r[9], _TEXT) for r in rows]
    )
    if analyze:
        conn.execute("ANALYZE")
    conn.commit()
    conn.close()
    return Database(str(path))


def _measure(db, leaf, monkeypatch):
    work = {"connections": 0, "instructions": 0, "shadow_reads": 0}
    connect = sqlite3.connect

    def trace(statement):
        if "message_chunks_fts_idx" in statement or "message_chunks_fts_data" in statement:
            work["shadow_reads"] += 1

    def progress():
        work["instructions"] += 1000
        return 0

    def counted_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        work["connections"] += 1
        conn.set_trace_callback(trace)
        conn.set_progress_handler(progress, 1000)
        return conn

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", counted_connect)
        started = time.perf_counter()
        if leaf == "text":
            page = db.query_messages(text=" ".join(_TERMS), limit=1)
        else:
            page = db.query_messages(
                where=normalize_where(_where({"leaf": leaf, "value": " ".join(_TERMS)})), limit=1
            )
        work["elapsed"] = time.perf_counter() - started
        sql, params = compile_leaves([Leaf(leaf, _TERMS)])
        with db._connect() as conn:
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM messages m WHERE " + sql, params
            ).fetchall()
    return page, work, [r[3] for r in plan]


@pytest.mark.parametrize("body_chunks", [1, 10])
@pytest.mark.parametrize("analyze", [False, True])
@pytest.mark.parametrize("leaf", ["text", "body_words", "attachment_text_words"])
def test_common_word_queries_do_not_reopen_fts_for_every_chunk(
    tmp_path, monkeypatch, leaf, analyze, body_chunks
):
    measured = []
    for messages in (40, 160):
        page, work, plan = _measure(
            _dense_db(tmp_path, messages, analyze, body_chunks), leaf, monkeypatch
        )
        assert (page.total_matches, page.indeterminate, len(page.messages)) == (messages, 0, 1)
        assert (
            work["connections"] >= 2
        )  # Query and explicit plan; all connections are instrumented.
        assert work["instructions"] > 0
        assert work["shadow_reads"] > 0
        # FTS hits drive the join; a rowid probe per metadata row is the bad plan.
        fts = [p for p in plan if p.startswith("SCAN f VIRTUAL TABLE INDEX")]
        chunks = [
            p for p in plan if p.startswith("SEARCH c USING INDEX idx_message_chunks_fts_rowid")
        ]
        assert len(fts) == len(chunks) == len(_TERMS), (plan, work)
        assert all("=" not in p.split("INDEX", 1)[1] for p in fts), (plan, work)
        # A generous bound on actual FTS shadow reads catches the repeated probes,
        # including native work the outer VM's progress counter cannot see.
        assert work["shadow_reads"] < 100 * len(_TERMS), work
        assert work["elapsed"] < 30, work
        measured.append(work)
    small, large = measured
    assert large["connections"] == small["connections"]
    assert large["instructions"] < 8 * small["instructions"] + 50_000, measured


@pytest.mark.parametrize("complete", [1, 0, None])
@pytest.mark.parametrize("leaf", ["body_words", "attachment_text_words"])
def test_words_span_chunks_but_never_cross_sources(tmp_path, leaf, complete):
    conn, path = _open_built_db_conn(tmp_path, "semantics.db")
    for i in range(3):
        mid = f"m{i}"
        _insert_message(
            conn,
            message_id=mid,
            thread_id=mid,
            sent_at=None,
            has_attachments=i != 2,
            completeness={"body_complete": complete, "attachments_manifest_complete": complete},
        )
        if i != 2:
            _insert_attachment(
                conn, message_id=mid, thread_id=mid, attachment_id=f"a{i}", filename="synthetic.pdf"
            )
    conn.execute("UPDATE attachments SET text_complete = ?", (complete,))
    for mid, body, attachment in [
        ("m0", ("alpha", "budget"), ("alpha", "budget")),
        ("m1", ("alpha",), ("budget",)),
    ]:
        for source, texts in [(None, body), (f"a{mid[1:]}", attachment)]:
            for j, text in enumerate(texts):
                _insert_chunk(
                    conn,
                    chunk_id=f"{mid}-{source}-{j}",
                    message_id=mid,
                    thread_id=mid,
                    text=text,
                    embedding=[1.0, 0.0, 0.0, 0.0],
                    attachment_id=source,
                    chunk_index=j,
                )
    conn.commit()
    conn.close()
    db = Database(str(path))
    for negate in (False, True):
        page = db.query_messages(
            where=normalize_where(_where({"leaf": leaf, "value": "alpha budget", "negate": negate}))
        )
        expected = {"m1", "m2"} if negate and complete == 1 else set() if negate else {"m0"}
        assert {m.message_id for m in page.messages} == expected
        assert page.total_matches == len(expected)
        assert page.indeterminate == (0 if complete == 1 else 2)


@pytest.mark.parametrize("leaf", ["body_words", "attachment_text_words"])
def test_original_join_is_a_semantic_oracle_and_exposes_repeated_fts_work(tmp_path, leaf):
    db = _dense_db(tmp_path, 40)
    suffix = "IS NULL" if leaf == "body_words" else "IS NOT NULL"
    # Retain the pre-fix join: the optimizer may already avoid its bad plan
    # on newer SQLite builds, so the red proof is classified by the plan.
    old_match = " AND ".join(
        "m.claimant_id IN (SELECT c.claimant_id FROM message_chunks_fts f "
        "JOIN message_chunks c ON c.fts_rowid = f.rowid "
        "WHERE message_chunks_fts MATCH ? AND c.attachment_id " + suffix + ")"
        for _ in _TERMS
    )
    params = [f'"{word}"' for word in _TERMS]
    reads = []
    with db._connect() as conn:
        conn.set_trace_callback(
            lambda sql: (
                reads.append(sql)
                if "message_chunks_fts_idx" in sql or "message_chunks_fts_data" in sql
                else None
            )
        )
        old = {
            r[0]
            for r in conn.execute("SELECT m.claimant_id FROM messages m WHERE " + old_match, params)
        }
        shadow_reads = len(reads)
        old_plan = [
            r[3]
            for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT m.claimant_id FROM messages m WHERE " + old_match, params
            )
        ]
        current, current_params = compile_leaves([Leaf(leaf, _TERMS)])
        new = {
            r[0]
            for r in conn.execute(
                "SELECT m.claimant_id FROM messages m WHERE " + current, current_params
            )
        }
    assert old == new == {claimant_of(f"m{i}") for i in range(40)}
    if leaf == "body_words":
        if sqlite3.sqlite_version != "3.46.1":
            pytest.skip(
                f"Old-plan red proof targets runtime SQLite 3.46.1; got {sqlite3.sqlite_version}"
            )
        assert any("idx_message_chunks_attachment" in line for line in old_plan), old_plan
    if any("idx_message_chunks_attachment" in line for line in old_plan):
        assert shadow_reads >= 100 * len(_TERMS), (old_plan, shadow_reads)


@pytest.mark.parametrize("leaf", ["text", "body_words", "attachment_text_words"])
def test_rare_and_missing_words_keep_population_counts(tmp_path, leaf):
    db = _dense_db(tmp_path, 40)
    with sqlite3.connect(db.path) as conn:
        # Add a rare word to one body and one attachment chunk of one message.
        for rowid in (1, 11):
            text = _TEXT + " rareword"
            conn.execute("UPDATE message_chunks SET text = ? WHERE fts_rowid = ?", (text, rowid))
            conn.execute("DELETE FROM message_chunks_fts WHERE rowid = ?", (rowid,))
            conn.execute("INSERT INTO message_chunks_fts(rowid, text) VALUES (?, ?)", (rowid, text))
    for text, wanted in [("word0 rareword", {"m0"}), ("word0 absentword", set())]:
        page = (
            db.query_messages(text=text)
            if leaf == "text"
            else db.query_messages(where=normalize_where(_where({"leaf": leaf, "value": text})))
        )
        assert {m.message_id for m in page.messages} == wanted
        assert page.total_matches == len(wanted)
        assert page.indeterminate == 0
