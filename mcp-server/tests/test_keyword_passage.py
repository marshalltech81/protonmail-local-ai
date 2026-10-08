"""Evidence selection keeps the passage a keyword match found (#858).

Each surfaced thread's passages were chosen by vector distance alone
(after the chunks of an attachment the query named), so in a long
thread the one chunk holding the query's exact word could fall outside
the six. Evidence selection now reserves a slot for a keyword-matched
chunk, after the named attachment's representative, ranked by how rare
in its thread the query words it holds are (#1246), from a scratch FTS
table of the fetched candidates. All data is synthetic; vectors are
crafted so dense order alone leaves the keyword chunk out.
"""

import asyncio
import logging
import sqlite3
from pathlib import Path

import pytest
import sqlite_vec
import src.lib.sqlite as sqlite_module
from fastmcp.exceptions import ToolError
from src.lib.inference import PromptBudget
from src.lib.sqlite import (
    PROMPT_EVIDENCE_CHUNKS_PER_THREAD,
    Database,
    _keyword_rank_keys,
    _sanitize_fts_query,
)
from src.tools.brief import register_experimental_tools
from src.tools.intelligence import register_intelligence_tools
from src.tools.outputs import EvidenceChunk
from src.tools.search import register_search_tools

from tests.conftest import (
    FakeEmbedClient,
    FakeInferenceClient,
    FakeMCPServer,
    _build_schema,
    _insert_attachment,
    _insert_chunk,
    _insert_thread,
)

WORD = "zebraquota"
KW_TEXT = f"reference {WORD} recorded here"
MARKER = "zqxmarker8581"
_NEAR = 10  # more near chunks than any thread keeps
_QUERY_VEC = [1.0, 0.0, 0.0, 0.0]
_FAR = [0.0, 0.0, 0.0, 1.0]


def _near(i: int) -> list[float]:
    return [1.0, 0.01 * (i + 1), 0.0, 0.0]


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _build_schema(conn)
    return conn


def _thread(conn, tid: str, *, subject: str = "routine status", attachments: bool = False):
    _insert_thread(
        conn,
        thread_id=tid,
        subject=subject,
        participants=["alice@example.com", "bob@example.com"],
        senders=["alice@example.com"],
        body_text="routine status update",
        has_attachments=attachments,
        embedding=_QUERY_VEC,
    )


def _near_body(conn, tid: str, n: int = _NEAR) -> None:
    for i in range(n):
        _insert_chunk(
            conn,
            chunk_id=f"{tid}-body-{i}",
            message_id=tid,
            thread_id=tid,
            text=f"routine status update part {i}",
            embedding=_near(i),
            chunk_index=i,
            char_start=100 * i,
        )


def _keyword_chunk(conn, tid: str, *, chunk_id: str | None = None, variant: str = "") -> None:
    _insert_chunk(
        conn,
        chunk_id=chunk_id or f"{tid}-kw",
        message_id=tid,
        thread_id=tid,
        text=KW_TEXT,
        embedding=_FAR,
        chunk_index=_NEAR,
        char_start=100 * _NEAR,
        variant=variant,
    )


def _long(conn, tid: str = "t-long") -> None:
    """A long thread whose only keyword chunk is the farthest one."""
    _thread(conn, tid)
    _near_body(conn, tid)
    _keyword_chunk(conn, tid)


def _attached(conn, tid: str = "t-att", *, rep_text: str = "line item {i} amount") -> None:
    """A thread whose named attachment has more near chunks than the cap,
    plus a far body chunk holding the keyword."""
    _thread(conn, tid, attachments=True)
    _insert_attachment(
        conn, message_id=tid, thread_id=tid, attachment_id=f"{tid}-ledger", filename="ledger.pdf"
    )
    for i in range(_NEAR):
        _insert_chunk(
            conn,
            chunk_id=f"{tid}-att-{i}",
            message_id=tid,
            thread_id=tid,
            text=rep_text.format(i=i) if i == 0 else f"line item {i} amount",
            embedding=_near(i),
            chunk_index=i,
            attachment_id=f"{tid}-ledger",
        )
    _keyword_chunk(conn, tid)


@pytest.fixture
def kw_db(tmp_path: Path) -> Database:
    conn = _open(tmp_path / "kw.db")
    _long(conn)
    _attached(conn)
    _attached(conn, "t-overlap", rep_text=f"line item {{i}} amount {WORD}")
    # Found by the thread's subject, not by any chunk.
    _thread(conn, "t-subj", subject=f"{WORD} notice")
    _near_body(conn, "t-subj")
    # Found by an attachment's filename, not by any chunk.
    _thread(conn, "t-file", attachments=True)
    _insert_attachment(
        conn,
        message_id="t-file",
        thread_id="t-file",
        attachment_id="t-file-a",
        filename=f"{WORD}.pdf",
    )
    _near_body(conn, "t-file")
    # Two files claim one Message-ID; each carries the same keyword chunk.
    _thread(conn, "t-dup")
    _near_body(conn, "t-dup")
    _keyword_chunk(conn, "t-dup", chunk_id="t-dup-kw-b", variant="b")
    _keyword_chunk(conn, "t-dup", chunk_id="t-dup-kw-a")
    conn.close()
    return Database(str(tmp_path / "kw.db"))


@pytest.fixture
def long_db(tmp_path: Path) -> Database:
    conn = _open(tmp_path / "long.db")
    _long(conn)
    conn.close()
    return Database(str(tmp_path / "long.db"))


def _select(db: Database, query: str, tid: str, limit: int = PROMPT_EVIDENCE_CHUNKS_PER_THREAD):
    return db.get_query_evidence_chunks(query, [tid], _QUERY_VEC, limit)[tid]


def _ids(chunks) -> list[str]:
    return [c.chunk_id for c in chunks]


def _labels(chunks) -> list[str]:
    return [c.selected_by for c in chunks]


class TestSlotOrder:
    def test_keyword_chunk_outside_six_nearest_is_selected(self, kw_db):
        chunks = _select(kw_db, WORD, "t-long")
        assert _ids(chunks) == ["t-long-kw"] + [f"t-long-body-{i}" for i in range(5)]
        assert _labels(chunks) == ["keyword_match"] + ["vector"] * 5

    def test_matched_attachment_cannot_crowd_out_keyword_chunk(self, kw_db):
        chunks = _select(kw_db, f"ledger {WORD}", "t-att")
        assert _ids(chunks) == ["t-att-att-0", "t-att-kw"] + [f"t-att-att-{i}" for i in range(1, 5)]
        assert _labels(chunks) == ["attachment_match", "keyword_match"] + ["attachment_match"] * 4

    def test_attachment_representative_that_matches_keywords_takes_one_slot(self, kw_db):
        """Overlap collapses: the representative holds the word, so it
        fills both reservations and is labelled keyword_match."""
        chunks = _select(kw_db, f"ledger {WORD}", "t-overlap")
        assert _ids(chunks) == [f"t-overlap-att-{i}" for i in range(6)]
        assert _labels(chunks) == ["keyword_match"] + ["attachment_match"] * 5

    def test_no_keyword_match_keeps_vector_order(self, kw_db):
        chunks = _select(kw_db, "nonexistentword", "t-long")
        assert _ids(chunks) == [f"t-long-body-{i}" for i in range(6)]
        assert _labels(chunks) == ["vector"] * 6

    def test_attachment_match_without_keyword_chunk_keeps_existing_order(self, kw_db):
        chunks = _select(kw_db, "ledger", "t-att")
        assert _ids(chunks) == [f"t-att-att-{i}" for i in range(6)]
        assert _labels(chunks) == ["attachment_match"] * 6

    @pytest.mark.parametrize("tid", ["t-subj", "t-file"])
    def test_thread_or_filename_only_match_has_no_keyword_passage(self, kw_db, tid):
        chunks = _select(kw_db, WORD, tid)
        assert _ids(chunks) == [f"{tid}-body-{i}" for i in range(6)]
        assert "keyword_match" not in _labels(chunks)

    def test_duplicate_claimants_pick_is_deterministic(self, kw_db):
        """Equal distances: the lower chunk_id wins the slot, every time."""
        for _ in range(3):
            chunks = _select(kw_db, WORD, "t-dup")
            assert _ids(chunks)[0] == "t-dup-kw-a"
            assert "t-dup-kw-b" not in _ids(chunks)

    def test_per_thread_limit_still_caps(self, kw_db):
        assert _ids(_select(kw_db, WORD, "t-long", limit=1)) == ["t-long-kw"]
        assert _ids(_select(kw_db, f"ledger {WORD}", "t-att", limit=1)) == ["t-att-att-0"]
        assert _ids(_select(kw_db, f"ledger {WORD}", "t-att", limit=2)) == [
            "t-att-att-0",
            "t-att-kw",
        ]

    def test_every_one_of_fifty_threads_keeps_its_keyword_chunk(self, tmp_path):
        """One lookup for every surfaced thread, with no pooled row cap:
        no thread's keyword chunk is starved by another's."""
        conn = _open(tmp_path / "fifty.db")
        tids = [f"t{i:02d}" for i in range(50)]
        for tid in tids:
            _thread(conn, tid)
            _near_body(conn, tid, n=7)
            _keyword_chunk(conn, tid)
        conn.close()
        db = Database(str(tmp_path / "fifty.db"))
        grouped = db.get_query_evidence_chunks(
            WORD, tids, _QUERY_VEC, PROMPT_EVIDENCE_CHUNKS_PER_THREAD
        )
        assert [_ids(grouped[tid])[0] for tid in tids] == [f"{tid}-kw" for tid in tids]


# --- rarity ranking (#1246) ---------------------------------------------------


def _chunk(conn, tid: str, n: int, text: str, embedding: list[float], **kwargs) -> None:
    _insert_chunk(
        conn,
        chunk_id=f"{tid}-{n}",
        message_id=tid,
        thread_id=tid,
        text=text,
        embedding=embedding,
        chunk_index=n,
        char_start=100 * n,
        **kwargs,
    )


def _rarity_db(path: Path, build) -> Database:
    conn = _open(path)
    build(conn)
    conn.close()
    return Database(str(path))


RARE_ID = "inv-40917"
QUESTION = f"What is the status of invoice {RARE_ID}?"


class TestRarityRanking:
    def test_natural_question_rare_identifier_wins_the_slot(self, tmp_path):
        """Every chunk holds a common word of the question; only the far
        one holds its identifier. The slot used to go to the nearest
        chunk holding any word; now the identifier's chunk wins."""

        def build(conn):
            _thread(conn, "t-q")
            for i in range(_NEAR):
                _chunk(conn, "t-q", i, f"the status meeting is on day {i}", _near(i))
            _chunk(conn, "t-q", _NEAR, f"the invoice {RARE_ID} was paid", _FAR)

        db = _rarity_db(tmp_path / "q.db", build)
        chunks = _select(db, QUESTION, "t-q")
        assert _ids(chunks) == [f"t-q-{_NEAR}"] + [f"t-q-{i}" for i in range(5)]
        # Every chunk holds a query word, so every kept one is labelled.
        assert _labels(chunks) == ["keyword_match"] * 6

    def test_padded_tuple_orders_mixed_frequencies(self, tmp_path):
        def build(conn):
            _thread(conn, "t-mix")
            # n = 12. alpha df 2 (chunks 0, 11); beta df 3 (11, 5, 6);
            # gamma df 9 (0, 1-8).
            _chunk(conn, "t-mix", 0, "alpha gamma", _near(0))
            for i in range(1, 9):
                _chunk(conn, "t-mix", i, "gamma beta" if i in (5, 6) else "gamma", _near(i))
            _chunk(conn, "t-mix", 9, "filler", _near(9))
            _chunk(conn, "t-mix", 10, "filler", _near(10))
            _chunk(conn, "t-mix", 11, "alpha beta", _FAR)

        db = _rarity_db(tmp_path / "mix.db", build)
        all_chunks = db.get_evidence_chunks_for_threads(["t-mix"], _QUERY_VEC, per_thread_limit=99)
        keys, unranked = _keyword_rank_keys("alpha beta gamma", all_chunks)
        assert unranked == 0
        pad = [13] * 14
        assert keys["t-mix-11"] == (2, 3, *pad)
        assert keys["t-mix-0"] == (2, 9, *pad)
        assert keys["t-mix-5"] == (3, 9, *pad)
        assert keys["t-mix-1"] == (9, 13, *pad)
        assert "t-mix-9" not in keys
        # [2, 3] beats the nearer [2, 9].
        assert _ids(_select(db, "alpha beta gamma", "t-mix"))[0] == "t-mix-11"

    def test_word_in_every_chunk_is_ignored_but_qualifies(self, tmp_path):
        """No informative word: the nearest holder wins, as before."""

        def build(conn):
            _thread(conn, "t-all")
            for i in range(4):
                _chunk(conn, "t-all", i, f"status note {i}", _near(i))

        db = _rarity_db(tmp_path / "all.db", build)
        all_chunks = db.get_evidence_chunks_for_threads(["t-all"], _QUERY_VEC, per_thread_limit=99)
        keys, _ = _keyword_rank_keys("status", all_chunks)
        assert set(keys.values()) == {(5,) * 16}
        chunks = _select(db, "status", "t-all")
        assert _ids(chunks) == [f"t-all-{i}" for i in range(4)]
        assert _labels(chunks) == ["keyword_match"] * 4

    def test_frequency_is_per_thread(self, tmp_path):
        """A word common in the mailbox can still be the rare one in a
        thread: here ``renewal`` is in seven chunks overall but one of
        t-b's three, where ``status`` is in two."""

        def build(conn):
            for tid in ("t-a", "t-b"):
                _thread(conn, tid)
            for i in range(6):
                _chunk(conn, "t-a", i, "renewal", _near(i))
            _chunk(conn, "t-a", 6, "other", _near(6))
            _chunk(conn, "t-b", 0, "status", _near(0))
            _chunk(conn, "t-b", 1, "status", _near(1))
            _chunk(conn, "t-b", 2, "renewal", _FAR)

        db = _rarity_db(tmp_path / "per.db", build)
        grouped = db.get_query_evidence_chunks(
            "renewal status", ["t-a", "t-b"], _QUERY_VEC, PROMPT_EVIDENCE_CHUNKS_PER_THREAD
        )
        # t-a: renewal is its only informative word (6 of 7): nearest holder.
        assert _ids(grouped["t-a"])[0] == "t-a-0"
        # t-b: renewal (1 of 3) is rarer than status (2 of 3).
        assert _ids(grouped["t-b"])[0] == "t-b-2"

    def test_stems_and_case_are_one_word(self, tmp_path):
        """``Invoices`` and ``invoice`` share one porter token: one probe,
        one frequency, no double count."""

        def build(conn):
            _thread(conn, "t-stem")
            _chunk(conn, "t-stem", 0, "invoice", _near(0))
            _chunk(conn, "t-stem", 1, "invoice ledger", _near(1))
            _chunk(conn, "t-stem", 2, "nothing", _near(2))

        db = _rarity_db(tmp_path / "stem.db", build)
        all_chunks = db.get_evidence_chunks_for_threads(["t-stem"], _QUERY_VEC, per_thread_limit=99)
        keys, unranked = _keyword_rank_keys("Invoices invoice INVOICE ledger", all_chunks)
        assert unranked == 0
        assert keys["t-stem-0"] == (2, *[4] * 15)
        assert keys["t-stem-1"] == (1, 2, *[4] * 14)

    def test_attachment_representative_with_a_common_word_keeps_two_slots(self, tmp_path):
        """The representative holds a common query word; a body chunk
        holds the rare one. They are different chunks, so both lead."""

        def build(conn):
            _thread(conn, "t-rep", attachments=True)
            _insert_attachment(
                conn,
                message_id="t-rep",
                thread_id="t-rep",
                attachment_id="t-rep-ledger",
                filename="ledger.pdf",
            )
            for i in range(4):
                _chunk(
                    conn,
                    "t-rep",
                    i,
                    f"ledger status line {i}",
                    _near(i),
                    attachment_id="t-rep-ledger",
                )
            _chunk(conn, "t-rep", 4, "status update", _near(4))
            _chunk(conn, "t-rep", 5, f"status of {RARE_ID}", _FAR)

        db = _rarity_db(tmp_path / "rep.db", build)
        chunks = _select(db, f"ledger status {RARE_ID}", "t-rep")
        assert _ids(chunks)[:2] == ["t-rep-0", "t-rep-5"]
        assert _labels(chunks)[:2] == ["keyword_match", "keyword_match"]

    def test_chunk_without_valid_vector_is_not_a_candidate(self, tmp_path):
        """A chunk the fetch cannot return neither wins nor counts in a
        word's frequency: counting it would make ``alpha`` df 2 and hand
        the slot to the farther ``beta`` chunk."""
        nan = [float("nan"), 0.0, 0.0, 0.0]

        def build(conn):
            _thread(conn, "t-nan")
            _chunk(conn, "t-nan", 0, "filler", _near(0))
            _chunk(conn, "t-nan", 1, "alpha", _near(1))
            _chunk(conn, "t-nan", 2, "beta", _near(2))
            _chunk(conn, "t-nan", 3, "alpha", nan)

        db = _rarity_db(tmp_path / "nan.db", build)
        chunks = _select(db, "alpha beta", "t-nan")
        assert _ids(chunks) == ["t-nan-1", "t-nan-0", "t-nan-2"]

    def test_words_beyond_sixteen_qualify_and_are_disclosed(self, tmp_path, caplog):
        caplog.set_level(logging.INFO)
        words = [f"w{k:02d}x" for k in range(18)]

        def build(conn):
            _thread(conn, "t-many")
            for i in range(_NEAR):
                _chunk(conn, "t-many", i, f"filler {i}", _near(i))
            # Only the seventeenth distinct word: qualifies, fully padded.
            _chunk(conn, "t-many", _NEAR, f"{words[16]} {MARKER}", _FAR)

        db = _rarity_db(tmp_path / "many.db", build)
        all_chunks = db.get_evidence_chunks_for_threads(["t-many"], _QUERY_VEC, per_thread_limit=99)
        keys, unranked = _keyword_rank_keys(" ".join(words), all_chunks)
        assert unranked == 2
        assert keys == {f"t-many-{_NEAR}": (_NEAR + 2,) * 16}

        server = FakeMCPServer()
        register_search_tools(server, db, FakeEmbedClient())
        for _ in range(2):
            out = asyncio.run(
                server.tools["get_evidence"](
                    query=" ".join(words) + f" {MARKER}", thread_id="t-many", limit=6
                )
            )
            [thread] = out.structured_content["threads"]
            assert thread["chunks"][0]["chunk_id"] == f"t-many-{_NEAR}"
            assert thread["chunks"][0]["selected_by"] == "keyword_match"
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and "Keyword passage ranking" in r.getMessage()
        ]
        # Rate-limited: one line for two capped calls in the window.
        assert warnings == [
            "Keyword passage ranking used the first 16 distinct query words; "
            "later words only qualify a passage: units_over_16"
        ]
        timing = [r.getMessage() for r in caplog.records if r.name == "mcp.timings"]
        assert len(timing) == 2
        # 18 words and the marker: three past the sixteenth.
        assert all("'keyword_units_unranked': 3" in line for line in timing)
        assert MARKER not in caplog.text

    def test_ranked_word_beats_an_unranked_one(self, tmp_path):
        words = [f"w{k:02d}x" for k in range(17)]

        def build(conn):
            _thread(conn, "t-rank")
            _chunk(conn, "t-rank", 0, "filler", _near(0))
            _chunk(conn, "t-rank", 1, words[16], _near(1))
            _chunk(conn, "t-rank", 2, words[3], _FAR)

        db = _rarity_db(tmp_path / "rank.db", build)
        assert _ids(_select(db, " ".join(words), "t-rank"))[0] == "t-rank-2"


# Each text is one chunk; each query is matched against all of them, in
# the mailbox's ``message_chunks_fts`` and in the scratch table.
_SHAPE_TEXTS = [
    "Résumé attached for review",
    "Résumé decomposed accents",
    "resume plain",
    "x-ray results and e-mail follow-up",
    "xray as one word",
    "contact alice@example.com or bob.smith@example.org",
    "the host mail.example.net answered",
    "version 3.14 released on 2024-01-02",
    "naïve café Straße Ünïcödé",
    "strasse spelled out",
    "東京 会議 予定",
    "emoji 🎉 party",
    "snake_case_identifier and CamelCase",
    "invoices were running late",
    "an invoice arrived",
]
_SHAPE_QUERIES = [
    "résumé",
    "Résumé",
    "resume",
    "x-ray",
    "e-mail follow-up",
    "alice@example.com",
    "@example.org",
    "mail.example.net",
    "3.14",
    "2024-01-02",
    "naive cafe",
    "Straße",
    "東京",
    "🎉 party",
    "snake_case_identifier",
    "camelcase",
    "invoice",
    "running",
    "-- @ . ...",
    "What's the status of invoice INV-1?",
]


@pytest.mark.parametrize("query", _SHAPE_QUERIES)
def test_scratch_matches_equal_mailbox_matches(tmp_path, query):
    def build(conn):
        _thread(conn, "t-shape")
        for i, text in enumerate(_SHAPE_TEXTS):
            _chunk(conn, "t-shape", i, text, _near(i))

    db = _rarity_db(tmp_path / "shape.db", build)
    fts_query = _sanitize_fts_query(query)
    mailbox = (
        {
            r["chunk_id"]
            for r in db._fetchall(
                "SELECT c.chunk_id FROM message_chunks c JOIN message_chunks_fts f "
                "ON f.rowid = c.fts_rowid WHERE message_chunks_fts MATCH ?",
                (fts_query,),
            )
        }
        if fts_query
        else set()
    )
    all_chunks = db.get_evidence_chunks_for_threads(["t-shape"], _QUERY_VEC, per_thread_limit=99)
    keys, _ = _keyword_rank_keys(query, all_chunks)
    assert set(keys) == mailbox


class _CountingScratch(sqlite3.Connection):
    """Records the rows each ``executemany`` writes."""

    inserted: list[tuple[str, int]]

    def executemany(self, sql, rows, /):
        rows = list(rows)
        self.inserted.append((sql.split("(")[0].strip(), len(rows)))
        return super().executemany(sql, rows)


def test_scratch_inserts_exactly_the_fetched_rows(tmp_path, monkeypatch):
    """The scratch table holds the fetched, vector-valid candidates and
    nothing else, so its work is bounded by what the evidence fetch read."""

    def build(conn):
        for t in range(3):
            tid = f"t-w{t}"
            _thread(conn, tid)
            for i in range(4 + t):
                _chunk(conn, tid, i, f"{WORD} part {i}", _near(i))
        # Not surfaced, and not a candidate.
        _thread(conn, "t-other")
        _chunk(conn, "t-other", 0, WORD, _near(0))
        # Surfaced, but no valid vector: not fetched, not inserted.
        _chunk(conn, "t-w0", 9, WORD, [float("nan"), 0.0, 0.0, 0.0])

    db = _rarity_db(tmp_path / "work.db", build)
    opened: list[_CountingScratch] = []

    def connect():
        conn = sqlite3.connect(":memory:", factory=_CountingScratch)
        conn.inserted = []
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite_module, "_scratch_connection", connect)
    grouped = db.get_query_evidence_chunks(
        f"{WORD} part {WORD}", ["t-w0", "t-w1", "t-w2"], _QUERY_VEC, 99
    )
    fetched = sum(len(chunks) for chunks in grouped.values())
    assert fetched == 4 + 5 + 6
    [scratch] = opened
    assert scratch.inserted == [("INSERT INTO units", 2), ("INSERT INTO candidates", fetched)]


def test_no_query_words_skips_the_scratch_connection(kw_db, monkeypatch):
    def connect():
        raise AssertionError("scratch opened")

    monkeypatch.setattr(sqlite_module, "_scratch_connection", connect)
    chunks = _select(kw_db, "?! ,;", "t-long")
    assert _labels(chunks) == ["vector"] * 6


# --- work-growth gate ------------------------------------------------------


_DATE = "2024-01-01T00:00:00+00:00"
# A sixteen-word question whose every word is in every corpus chunk: the
# worst case for a ``MATCH`` (#1262).
_GATE_WORDS = (
    "what is the status of invoice that we sent to them last week for this order"
).split()
_GATE_QUERY = " ".join(_GATE_WORDS) + "?"


def _gate_text(i: int) -> str:
    """About 23 tokens, near real mail's chunk density. Thin chunks (three
    tokens) leave so few FTS5 segments that a lookup seeking the mailbox
    index looks flat when it is not (#1262)."""
    return " ".join(_GATE_WORDS) + f" filler word{i % 97} lorem ipsum dolor sit amet"


def _corpus(path: Path, chunks: int, per_thread: int = 10) -> Database:
    """``chunks`` chunks with vectors, every one holding every query word,
    written as the indexer writes them (``indexer/src/database.py``: the
    same text in ``message_chunks`` and ``message_chunks_fts``, one FTS
    insert per chunk so the index keeps its automerge segments), with the
    indexer's two ``message_chunks`` indexes the lookups rely on."""
    conn = _open(path)
    conn.executescript(
        "CREATE INDEX idx_message_chunks_thread ON message_chunks(thread_id);"
        "CREATE INDEX idx_message_chunks_fts_rowid ON message_chunks(fts_rowid);"
    )
    cur = conn.cursor()
    threads = chunks // per_thread
    cur.executemany(
        "INSERT INTO threads (thread_id, subject, participants, folder, date_first, "
        "date_last, message_ids) VALUES (?, 's', '[]', 'INBOX', ?, ?, '[]')",
        [(f"t{t}", _DATE, _DATE) for t in range(threads)],
    )
    for i in range(chunks):
        text = _gate_text(i)
        cur.execute("INSERT INTO message_chunks_fts (text) VALUES (?)", (text,))
        cur.execute(
            "INSERT INTO message_chunks (chunk_id, claimant_id, thread_id, chunk_index, text, "
            "char_start, char_end, token_est, chunked_at, fts_rowid, kind) "
            "VALUES (?, 'm', ?, ?, ?, 0, ?, 23, '2024', ?, 'body')",
            (f"c{i}", f"t{i // per_thread}", i % per_thread, text, len(text), cur.lastrowid),
        )
        cur.execute(
            "INSERT INTO message_chunks_vec (chunk_id, embedding) VALUES (?, ?)",
            (f"c{i}", sqlite_vec.serialize_float32(_near(i % per_thread))),
        )
    conn.commit()
    conn.close()
    return Database(str(path))


def _measure(monkeypatch, fn) -> tuple[int, object]:
    """``fn``'s result and the SQLite VM steps run on every connection it
    opens: the mailbox's and any in-memory one."""
    steps = [0]
    real = sqlite3.connect

    def tick() -> int:
        steps[0] += 1
        return 0

    def connect(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.set_progress_handler(tick, 1)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        result = fn()
    finally:
        monkeypatch.undo()
    return steps[0], result


def test_lookup_work_stays_flat_while_chunk_lane_grows(tmp_path, monkeypatch):
    """The acceptance gate (#858, #1262): the evidence lookup reads only the
    surfaced threads' chunks, so its VM steps, on every connection it uses,
    barely move from 1k to 50k chunks of realistic density, while the
    corpus-wide chunk lane's grow with the corpus."""
    surfaced = [f"t{2 * i}" for i in range(50)]
    lookup: dict[int, int] = {}
    lane: dict[int, int] = {}
    for size in (1_000, 50_000):
        db = _corpus(tmp_path / f"c{size}.db", size)
        lookup[size], grouped = _measure(
            monkeypatch,
            lambda db=db: db.get_query_evidence_chunks(
                _GATE_QUERY, surfaced, _QUERY_VEC, PROMPT_EVIDENCE_CHUNKS_PER_THREAD
            ),
        )
        # Work done, not only the result: every surfaced thread kept a
        # keyword-matched passage.
        assert set(grouped) == set(surfaced)
        assert all(chunks[0].selected_by == "keyword_match" for chunks in grouped.values())
        lane[size], _ = _measure(
            monkeypatch, lambda db=db: db._chunk_keyword_search(_GATE_QUERY, 50)
        )
    assert lookup[50_000] < 2 * lookup[1_000], lookup
    assert lane[50_000] > 10 * lane[1_000], lane


# --- consumers ---------------------------------------------------------------


def _evidence(db: Database, **kwargs) -> dict:
    server = FakeMCPServer()
    register_search_tools(server, db, FakeEmbedClient())
    return asyncio.run(server.tools["get_evidence"](**kwargs)).structured_content


class TestGetEvidence:
    def test_get_evidence_reports_selected_by(self, kw_db):
        out = _evidence(kw_db, query=f"ledger {WORD}", thread_id="t-att", limit=3)
        [thread] = out["threads"]
        assert [(c["chunk_id"], c["selected_by"]) for c in thread["chunks"]] == [
            ("t-att-att-0", "attachment_match"),
            ("t-att-kw", "keyword_match"),
            ("t-att-att-1", "attachment_match"),
        ]

    def test_get_evidence_source_body_keeps_keyword_chunk(self, kw_db):
        """Precision filters read the full ranked list in the new order."""
        out = _evidence(kw_db, query=f"ledger {WORD}", max_threads=5, source="body")
        by_thread = {t["thread_id"]: t["chunks"] for t in out["threads"]}
        assert by_thread["t-att"][0]["chunk_id"] == "t-att-kw"
        assert by_thread["t-att"][0]["selected_by"] == "keyword_match"

    def test_selected_by_is_in_the_published_schema(self):
        prop = EvidenceChunk.model_json_schema()["properties"]["selected_by"]
        assert prop["enum"] == ["keyword_match", "attachment_match", "vector"]


class _CaptureReranker:
    candidates = 10

    def __init__(self) -> None:
        self.documents: list[str] = []

    def rerank(self, query, documents, top_n):
        self.documents = list(documents)
        return [(i, float(len(documents) - i)) for i in range(len(documents))][:top_n]


def test_reranker_sees_keyword_chunk_first(long_db):
    """The reranker reads passage zero; for a thread with a keyword hit
    and no named attachment, that is now the keyword chunk."""
    reranker = _CaptureReranker()
    long_db.hybrid_search(
        query_text=WORD,
        query_embedding=_QUERY_VEC,
        limit=5,
        with_evidence=True,
        reranker=reranker,
        evidence_per_thread=PROMPT_EVIDENCE_CHUNKS_PER_THREAD,
    )
    [doc] = reranker.documents
    assert doc.endswith(KW_TEXT)


@pytest.mark.parametrize(
    "tool",
    [
        "ask_mailbox",
        "extract_from_emails",
        "brief_issue",
        "check_conclusion",
        "get_evidence",
        "get_evidence_thread",
    ],
)
def test_every_consumer_gets_the_keyword_passage(long_db, tool):
    inference = FakeInferenceClient()
    server = FakeMCPServer()
    register_intelligence_tools(server, long_db, FakeEmbedClient(), inference)
    register_experimental_tools(server, long_db, FakeEmbedClient(), inference)
    register_search_tools(server, long_db, FakeEmbedClient())
    calls = {
        "ask_mailbox": lambda: server.tools["ask_mailbox"](question=WORD),
        "extract_from_emails": lambda: server.tools["extract_from_emails"](
            query=WORD, schema={"n": "string"}
        ),
        "brief_issue": lambda: server.tools["brief_issue"](topic=WORD),
        "check_conclusion": lambda: server.tools["check_conclusion"](conclusion=WORD),
        "get_evidence": lambda: server.tools["get_evidence"](query=WORD, max_threads=1),
        "get_evidence_thread": lambda: server.tools["get_evidence"](
            query=WORD, thread_id="t-long", limit=PROMPT_EVIDENCE_CHUNKS_PER_THREAD
        ),
    }
    try:
        out = asyncio.run(calls[tool]())
    except ToolError:
        out = None  # the canned reply need not parse; the prompt was sent
    if tool.startswith("get_evidence"):
        [thread] = out.structured_content["threads"]
        assert thread["chunks"][0]["chunk_id"] == "t-long-kw"
    else:
        assert inference.complete_calls
        assert any(KW_TEXT in user for _system, user in inference.complete_calls)


# --- failure and budget ------------------------------------------------------


class _FailingScratch(sqlite3.Connection):
    """Raises, with mail-like text in the message, on the statement
    holding ``fail_on``."""

    fail_on = ""
    error: type[sqlite3.Error] = sqlite3.OperationalError

    def execute(self, sql, params=(), /):
        if self.fail_on in sql:
            raise self.error(f"fts5: syntax error near {MARKER}")
        return super().execute(sql, params)

    def executemany(self, sql, rows, /):
        if self.fail_on in sql:
            raise self.error(f"fts5: syntax error near {MARKER}")
        return super().executemany(sql, rows)


@pytest.mark.parametrize(
    ("fail_on", "error", "key"),
    [
        ("CREATE VIRTUAL TABLE units", sqlite3.OperationalError, "OperationalError"),
        ("INSERT INTO candidates", sqlite3.DatabaseError, "DatabaseError"),
        ("MATCH", sqlite3.IntegrityError, "other"),
    ],
)
def test_failed_lookup_keeps_selection_and_is_visible(
    long_db, monkeypatch, caplog, fail_on, error, key
):
    caplog.set_level(logging.INFO)

    def connect():
        conn = sqlite3.connect(":memory:", factory=_FailingScratch)
        conn.fail_on = fail_on
        conn.error = error
        return conn

    monkeypatch.setattr(sqlite_module, "_scratch_connection", connect)
    server = FakeMCPServer()
    register_search_tools(server, long_db, FakeEmbedClient())
    for _ in range(3):
        out = asyncio.run(
            server.tools["get_evidence"](query=f"{WORD} {MARKER}", thread_id="t-long", limit=6)
        )
        [thread] = out.structured_content["threads"]
        # The existing selection: vector order, nothing keyword-labelled.
        assert [c["chunk_id"] for c in thread["chunks"]] == [f"t-long-body-{i}" for i in range(6)]
        assert {c["selected_by"] for c in thread["chunks"]} == {"vector"}

    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "Keyword passage lookup" in r.getMessage()
    ]
    # Rate-limited: one line for three failures in the window.
    assert warnings == [f"Keyword passage lookup failed; keeping vector order: {key}"]
    timing = [r.getMessage() for r in caplog.records if r.name == "mcp.timings"]
    assert len(timing) == 3
    assert all("'degraded_keyword_chunks': 1" in line for line in timing)
    assert MARKER not in caplog.text


def test_second_position_keyword_passage_omission_is_disclosed(tmp_path):
    """Selection does not guarantee visibility: a long named-attachment
    representative fills the thread's share, the keyword passage behind
    it is left out, and the coverage note says so."""
    conn = _open(tmp_path / "budget.db")
    for t in range(5):
        tid = f"t-b{t}"
        _thread(conn, tid, attachments=True)
        _insert_attachment(
            conn, message_id=tid, thread_id=tid, attachment_id=f"{tid}-a", filename="ledger.pdf"
        )
        _insert_chunk(
            conn,
            chunk_id=f"{tid}-att",
            message_id=tid,
            thread_id=tid,
            text="line item amount " + "x" * 8000,
            embedding=_near(0),
            attachment_id=f"{tid}-a",
        )
        _keyword_chunk(conn, tid)
    conn.close()
    db = Database(str(tmp_path / "budget.db"))

    selected = db.get_query_evidence_chunks(
        f"ledger {WORD}", ["t-b0"], _QUERY_VEC, PROMPT_EVIDENCE_CHUNKS_PER_THREAD
    )["t-b0"]
    assert _ids(selected) == ["t-b0-att", "t-b0-kw"]

    inference = FakeInferenceClient()
    server = FakeMCPServer()
    register_intelligence_tools(
        server,
        db,
        FakeEmbedClient(),
        inference,
        prompt_budget=PromptBudget(context_tokens=4096, max_output_tokens=1024),
    )
    asyncio.run(server.tools["ask_mailbox"](question=f"ledger {WORD}"))
    # The first call is the answer; a later one is the citation repair.
    _system, user = inference.complete_calls[0]
    assert KW_TEXT not in user
    assert "Evidence note: to fit the prompt budget, 5 retrieved passages were left out" in user
