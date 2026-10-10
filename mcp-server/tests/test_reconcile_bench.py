"""Keeps the #1218 sizing benchmark (``scripts/reconcile_bench.py``)
runnable and its measurements honest, at a size that runs in seconds."""

import importlib.util
import json
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest
import sqlite_vec

from tests.conftest import _build_schema

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_bench.py"


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("reconcile_bench", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("identity", ["typical", "ascii998", "utf8x4"])
def test_message_id_widths(bench, identity):
    ids = [bench.message_id(i, identity) for i in (0, 1, 1023, 1024, 49_999)]
    assert len(set(ids)) == len(ids)
    if identity != "typical":
        assert all(len(i) == bench.MESSAGE_ID_MAX_CHARS for i in ids)
    if identity == "utf8x4":
        # The UTF-8 upper bound: four bytes a character, plus the suffix.
        assert len(bench.claimant_of(ids[0]).encode()) == 4 * 998 + 17


@pytest.fixture(scope="module")
def report(bench, tmp_path_factory):
    work = tmp_path_factory.mktemp("bench")
    return bench.main(
        [
            "--workdir",
            str(work),
            "--messages",
            "240",
            "--per-message",
            "2",
            "--identity",
            "utf8x4",
            "--records",
            "worst",
            "--k",
            "10,40",
            "--missing",
            "30",
            "--extras",
            "4",
            "--repeat",
            "1",
            "--wal",
            "--writer-interval",
            "0.005",
            # Keeps the tiny round's transaction open long enough for
            # the writer to commit inside it.
            "--wal-hold",
            "1.0",
            "--filtered",
            "--request-shapes",
            "1000",
        ]
    )


@pytest.fixture(scope="module")
def cardinality(bench, tmp_path_factory):
    work = tmp_path_factory.mktemp("cardinality")
    return bench.main(
        [
            "--workdir",
            str(work),
            "--messages",
            "40",
            "--per-message",
            "1",
            "--records",
            "cardinality",
            "--references",
            "1000",
            "--k",
            "3,10",
            "--missing",
            "10",
            "--extras",
            "0",
            "--repeat",
            "1",
            "--extracted-chars",
            "100",
            "--filtered",
        ]
    )


def test_certificate_scans_exactly_the_counted_set(report):
    # 240 messages; worst-case records sit in a nested folder, never in Trash.
    for kind, members in (("messages", 240), ("occurrences", 480)):
        stream = report["certificate"][f"{kind}/stream"]
        collect = report["certificate"][f"{kind}/collect"]
        assert stream["count"] == collect["count"] == members
        # SQLite's ORDER BY on the identity is UTF-8 byte order, so both
        # methods hash the same sequence.
        assert stream["digest"] == collect["digest"]
    assert report["certificate"]["messages/stream"]["max_identity_bytes"] == 4 * 998 + 17
    assert report["certificate"]["occurrences/stream"]["max_identity_bytes"] == 64


def test_reconcile_round_returns_at_most_k_missing_records(report):
    for kind, members in (("messages", 240), ("occurrences", 480)):
        result = report["reconcile"][kind]
        assert result["request"]["members"] == members
        assert result["request"]["uploaded"] == members - 30 + 4
        uploaded = members - 30 + 4
        # Hex: 64 characters plus quotes and a comma per hash.
        assert result["request"]["request_bytes_hex"] == 13 + 67 * uploaded - 1
        # Packed: one base64 string over 32 bytes a digest.
        assert result["request"]["request_bytes_packed"] == 14 + 4 * math.ceil(32 * uploaded / 3)
        for k, round_ in result["stream"].items():
            # The collect path returns the same round.
            collect = result["collect"][k]
            for key in ("returned", "extras", "missing_total", "response_bytes"):
                assert collect[key] == round_[key]
            assert round_["missing_total"] == 30
            assert round_["returned"] == min(int(k), 30)
            assert round_["extras"] == 4
            assert round_["rounds_to_repair"] == math.ceil(30 / int(k))
            assert round_["rounds_from_empty"] == math.ceil(members / int(k))
            assert round_["response_bytes"] > round_["returned"] * 1000
            # Every extra comes back as a 64-character hash.
            assert round_["response_bytes"] > 66 * round_["extras"]


def test_worst_records_carry_four_byte_fields_past_their_clips(report):
    # 33 participants, each a four-byte name and an ASCII address of 500
    # kept characters; an occurrence's four-byte filename and ASCII MIME
    # type likewise.
    messages = report["reconcile"]["messages"]["stream"]["10"]
    assert messages["record_bytes_max"] > 30 * (500 * 4 + 500)
    assert messages["participant_rows"] == 10 * 33
    assert messages["references"] == 10 * 11
    occurrences = report["reconcile"]["occurrences"]["stream"]["10"]
    assert occurrences["record_bytes_max"] > 500 * 4 + 500
    assert occurrences["participant_rows"] == occurrences["references"] == 0


def test_cardinality_records_read_every_stored_row(bench, cardinality):
    # The record readers load every participant row and References entry
    # before the output clips them, so a round's work is K times the
    # stored cardinality, not K times the listed one.
    for k, round_ in cardinality["reconcile"]["messages"]["stream"].items():
        assert round_["returned"] == int(k)
        assert round_["participant_rows"] == int(k) * bench.MAX_MESSAGE_ADDRESSES
        assert round_["references"] == int(k) * 1000


def test_wal_is_reclaimed_after_the_round(report):
    assert [w["kind"] for w in report["wal"]] == ["messages", "occurrences"]
    for wal in report["wal"]:
        commits = wal["commits_during_transaction"]
        assert commits > 0
        # Every frame committed while the round held its snapshot stays
        # in the WAL, beyond what the steady state can reuse.
        assert (
            wal["wal_max_during_transaction_bytes"]
            >= commits * wal["commit_bytes"] - wal["wal_steady_max_bytes"]
        )
        assert wal["checkpoint_after"]["busy"] == 0
        assert wal["wal_after_checkpoint_bytes"] == 0


def test_failed_round_stops_the_writer(bench, tmp_path):
    # A reader that fails (here: its request file is missing) must not
    # leave the unthrottled writer committing after run_wal returns.
    db = tmp_path / "failed.db"
    bench.build(db, 20, 1, "typical", "typical")

    def commits() -> int:
        # The ballast rowid grows by one with every writer commit.
        with closing(sqlite3.connect(db)) as conn:
            return conn.execute("SELECT COALESCE(MAX(id), 0) FROM bench_ballast").fetchone()[0]

    with pytest.raises(RuntimeError, match="reconcile round failed"):
        bench.run_wal(str(db), "messages", 5, str(tmp_path / "missing.json"), 4096, 0.0)
    after = commits()
    assert after > 0
    time.sleep(0.5)
    assert commits() == after
    assert not (tmp_path / "failed.db.stop").exists()


def test_filtered_certificate_counts_what_a_page_counts(report):
    # The certificate and one production page agree on every filtered
    # set, including the empty one whose scan finds nothing.
    for kind in ("messages", "occurrences"):
        rows = report["filtered"][kind]
        assert rows
        for row in rows:
            assert row["count"] == row["page_total"]
        nobody = next(r for r in rows if r["filters"] == {"participant": "nobody"})
        assert nobody["count"] == 0
    texts = {r["filters"].get("text"): r["count"] for r in report["filtered"]["messages"]}
    # gamma<i> is message i's own word, and the corpus has no message
    # 4242; alpha7 is in every fiftieth body.
    assert texts["gamma4242"] == 0
    assert texts["alpha7"] > 0
    vendor = next(
        r for r in report["filtered"]["messages"] if r["filters"] == {"authority_class": "vendor"}
    )
    # Every worst-case message has eleven From participants, so its
    # sender is ambiguous and no authority class decides it.
    assert vendor["count"] == vendor["page_total"] == 0
    # Body chunks carry at least the 20 tokens real mail does, so the
    # text filters run over a realistic FTS index.
    assert report["build"]["body_tokens_min"] >= 20


def test_request_shapes_count_their_elements(report):
    shapes = report["request_shapes"]
    assert shapes["packed"]["elements"] == shapes["hex_array"]["elements"] == 1000
    # The same byte budget as the hex array holds five times the
    # elements as two-character strings, all parsed before any check.
    assert shapes["short_array"]["bytes"] <= shapes["hex_array"]["bytes"]
    # A packed envelope with an ignored member is as small as the packed
    # upload of the same digests yet builds one object per junk string.
    assert shapes["packed_junk"]["bytes"] <= shapes["packed"]["bytes"] + 64
    assert shapes["packed_junk"]["elements"] == (shapes["packed"]["bytes"] - 22) // 5
    assert shapes["short_array"]["elements"] == (67 * 1000 + 12 - 13) // 5
    assert shapes["packed"]["bytes"] < shapes["hex_array"]["bytes"]


def test_occurrence_rounds_read_rows_with_extracted_text(cardinality):
    # Every extraction row carries 100 four-byte characters of text the
    # round never returns: a record still costs well under its text.
    for round_ in cardinality["reconcile"]["occurrences"]["stream"].values():
        assert round_["returned"] > 0
        assert round_["record_bytes_max"] < 4 * 100 * 4


def test_failed_phase_reports_its_stderr(bench, tmp_path):
    # A child that fails names its own error, so a CI failure shows the
    # cause rather than a bare exit status.
    with pytest.raises(RuntimeError, match="FileNotFoundError"):
        bench._child(
            "filtered", db_path=str(tmp_path / "none" / "x.db"), kind="messages", filters={}
        )


def _columns(script, table: str) -> set[str]:
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        script(conn)
        return {r[1] for r in conn.execute(f"PRAGMA table_xinfo({table})")}


@pytest.mark.parametrize(
    "table",
    [
        "messages",
        "message_participants",
        "message_participant_names",
        "attachments",
        "attachment_extractions",
        "pending_deletions",
        "entities",
    ],
)
def test_benchmark_schema_carries_the_readers_columns(bench, table):
    # The benchmark mirrors the indexer's tables. The server's test schema
    # (conftest) is the independent record of the columns its readers
    # select, so a column added there and not here fails here, before a
    # benchmark query does (#1374 added two the benchmark lacked).
    expected = _columns(_build_schema, table)
    assert expected, table
    assert expected <= _columns(lambda conn: conn.executescript(bench._SCHEMA), table)


@pytest.fixture(scope="module")
def mixed(bench, tmp_path_factory):
    work = tmp_path_factory.mktemp("mixed")
    return bench.main(
        [
            "--workdir",
            str(work),
            "--messages",
            "300",
            "--per-message",
            "2",
            "--identity",
            "utf8x4",
            "--records",
            "mixed",
            "--missing-from",
            "worst",
            "--k",
            "3",
            "--missing",
            "6",
            "--extras",
            "500",
            "--repeat",
            "1",
        ]
    )


def test_combined_round_returns_only_worst_records_with_many_extras(mixed):
    # Every record the combined round returns is a worst-case one, next
    # to many extras and four-byte IDs, so its peak covers them together.
    for kind, floor in (("messages", 30 * (500 * 4 + 500)), ("occurrences", 500 * 4 + 500)):
        for method in ("stream", "collect"):
            round_ = mixed["reconcile"][kind][method]["3"]
            assert round_["returned"] == 3
            assert round_["extras"] == 500
            assert round_["record_bytes_max"] > floor
            assert round_["response_bytes"] > 3 * floor + 66 * 500


def test_attachment_filters_reach_the_occurrence_certificate(report):
    rows = {json_key(r["filters"]): r for r in report["filtered"]["occurrences"]}
    assert rows["filename=nomatch"]["count"] == 0
    assert rows["extraction_status=none"]["count"] == 0
    assert rows["extraction_status=success"]["count"] > 0
    thread = next(v for k, v in rows.items() if k.startswith("thread_id="))
    assert thread["count"] > 0


def json_key(filters: dict) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(filters.items()))


def test_build_leaves_no_planner_statistics(bench, tmp_path):
    # Neither the indexer nor the server runs ANALYZE, so a deployed
    # index has no sqlite_stat1; the benchmark plans without it too.
    db = tmp_path / "plain.db"
    bench.build(db, 20, 1, "typical", "typical")
    with closing(sqlite3.connect(db)) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'sqlite_stat1'"
            ).fetchone()[0]
            == 0
        )


def test_where_expressions_reach_the_certificate(report):
    # The explicit ``where`` expression is compiled as query_messages
    # compiles it, so the page and the certificate count the same set.
    rows = [r for r in report["filtered"]["messages"] if "where" in r["filters"]]
    assert len(rows) == 4  # two 16-node expressions, two 16-term body_words
    for row in rows:
        assert row["count"] == row["page_total"]


def test_where_expressions_use_the_node_cap(bench):
    from src.lib.predicates import MAX_WHERE_NODES

    for case in (c for c in bench.MESSAGE_FILTERS if "where" in c):
        assert len(bench._where_leaves(case["where"])) <= MAX_WHERE_NODES + 1
    assert len(bench._WHERE_LEAVES) == MAX_WHERE_NODES


def test_attachment_scan_rejects_where(bench):
    with pytest.raises(ValueError):
        bench.scan_sql("occurrences", {"where": {"all": bench._WHERE_LEAVES[:1]}})


def test_filtered_runs_cover_high_cardinality_participants(cardinality):
    # Every message carries the most participant rows the indexer
    # accepts, so the participant and name predicates visit them all.
    rows = cardinality["filtered"]["messages"]
    assert rows
    for row in rows:
        assert row["count"] == row["page_total"]


@pytest.fixture(scope="module")
def terms(bench, tmp_path_factory):
    work = tmp_path_factory.mktemp("terms")
    return bench.main(
        [
            "--workdir",
            str(work),
            "--messages",
            "60",
            "--per-message",
            "1",
            "--chunks",
            "6",
            "--chunk-tokens",
            "30",
            "--filtered",
            "--k",
            "3",
            "--missing",
            "5",
            "--repeat",
            "1",
        ]
    )


@pytest.fixture(scope="module")
def all_extras(bench, tmp_path_factory):
    work = tmp_path_factory.mktemp("allextras")
    return bench.main(
        [
            "--workdir",
            str(work),
            "--messages",
            "200",
            "--per-message",
            "1",
            "--records",
            "mixed",
            "--all-extras",
            "--upload-total",
            "300,400",
            "--k",
            "3",
            "--repeat",
            "2",
        ]
    )


def test_cardinality_corpus_stores_names_for_every_participant(bench, cardinality):
    built = cardinality["build"]
    assert built["participant_rows"] == 40 * bench.MAX_MESSAGE_ADDRESSES
    # A first name per participant, plus the extra names of one address.
    assert built["name_rows"] == 40 * bench.MAX_MESSAGE_ADDRESSES


def test_chunk_and_term_cardinality_is_built_and_queried(bench, terms):
    assert terms["build"]["chunks"] == 60 * 6
    assert terms["build"]["body_tokens_min"] == 30
    rows = [
        r
        for r in terms["filtered"]["messages"]
        if "text" in r["filters"] and len(r["filters"]["text"].split()) == 16
    ]
    assert len(rows) == 1
    assert rows[0]["count"] == rows[0]["page_total"]
    # Each of the sixteen terms compiles to its own FTS subquery.
    count_sql, _, _ = bench.scan_sql("messages", {"text": bench.MESSAGE_FILTERS[0]["text"]})
    assert count_sql.count("MATCH") == 16
    negated = [
        r
        for r in terms["filtered"]["messages"]
        if r["filters"].get("where", {}).get("all", [{}])[0].get("negate") is True
        and r["filters"]["where"]["all"][0]["leaf"] == "body_words"
        and len(r["filters"]["where"]["all"][0]["value"].split()) == 16
    ]
    assert len(negated) == 1


def test_scan_methods_alternate_which_runs_first(bench):
    order = []

    def call(method):
        order.append(method)
        return {}

    bench._alternating(4, call)
    assert order == [
        "stream",
        "collect",
        "collect",
        "stream",
        "stream",
        "collect",
        "collect",
        "stream",
    ]


def test_certificates_report_how_often_each_method_ran_first(all_extras):
    for kind in ("messages", "occurrences"):
        assert all_extras["certificate"][f"{kind}/stream"]["first_runs"] == 1
        assert all_extras["certificate"][f"{kind}/collect"]["first_runs"] == 1


def test_all_extras_upload_misses_every_member_and_returns_worst_records(all_extras):
    for kind, uploaded in (("messages", 300), ("occurrences", 400)):
        req = all_extras["reconcile"][kind]["request"]
        assert req["extras"] == req["uploaded"] == uploaded
        for method in ("stream", "collect"):
            round_ = all_extras["reconcile"][kind][method]["3"]
            assert round_["extras"] == uploaded
            assert round_["missing_total"] == req["members"]
            assert round_["returned"] == 3
    # Worst-case records first: wide fields, not the typical ones.
    assert all_extras["reconcile"]["messages"]["stream"]["3"]["record_bytes_max"] > 10_000


@pytest.mark.parametrize(
    ("rotate", "expected"),
    [
        (0, ["page", "stream", "collect"]),
        (1, ["stream", "collect", "page"]),
        (2, ["collect", "page", "stream"]),
        (3, ["page", "stream", "collect"]),
    ],
)
def test_filtered_run_rotates_page_and_certificates(bench, tmp_path, monkeypatch, rotate, expected):
    from src.lib import sqlite as server_sqlite

    db = tmp_path / "order.db"
    bench.build(db, 20, 1, "typical", "typical")
    order: list[str] = []
    real_cert = bench.phase_certificate
    real_page = server_sqlite.Database.query_messages

    def cert(db_path, kind, method, filters=None):
        order.append(method)
        return real_cert(db_path, kind, method, filters)

    def page(self, *args, **kwargs):
        order.append("page")
        return real_page(self, *args, **kwargs)

    monkeypatch.setattr(bench, "phase_certificate", cert)
    monkeypatch.setattr(server_sqlite.Database, "query_messages", page)
    bench.phase_filtered(str(db), "messages", {"participant": "from0.7"}, rotate)
    assert order == expected


def test_round_window_starts_after_the_snapshot_and_ends_before_the_rollback(bench):
    import inspect

    src = inspect.getsource(bench.phase_reconcile)
    # The deferred BEGIN takes its snapshot at the COUNT; the writer's
    # commits are retained until the rollback.
    assert src.index('"SELECT 1 FROM messages LIMIT 1"') < src.index(
        "started_at = time.monotonic()"
    )
    assert src.index("started_at = time.monotonic()") < src.index("count = conn.execute")
    assert src.index("ended_at = time.monotonic()") < src.index("conn.rollback()")


def test_wal_commits_are_stamped_by_the_writer_after_they_commit(report):
    for wal in report["wal"]:
        assert wal["commits_during_transaction"] <= wal["writer_commits_total"]


def test_missing_worst_beyond_the_worst_members_is_rejected(bench, tmp_path):
    db = tmp_path / "few.db"
    bench.build(db, 60, 1, "typical", "mixed")  # one worst message in fifty
    with pytest.raises(
        ValueError, match="--missing-from worst: .* can be left out, --missing asks for 50"
    ):
        bench.write_request(str(db), "messages", 50, 0, tmp_path / "r.json", "worst")


def test_writer_commits_carry_no_instrumentation_row(bench, tmp_path):
    db = tmp_path / "plain2.db"
    bench.build(db, 20, 1, "typical", "typical")
    with closing(sqlite3.connect(db)) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "bench_commits" not in names


def test_k_and_method_order_alternate_across_repeats(bench):
    order = []

    def call(k, method):
        order.append((k, method))
        return {}

    bench._balanced_k(2, [100, 1000], call)
    assert order == [
        (100, "stream"),
        (100, "collect"),
        (1000, "stream"),
        (1000, "collect"),
        (1000, "collect"),
        (1000, "stream"),
        (100, "collect"),
        (100, "stream"),
    ]


@pytest.mark.parametrize("mode", ["spread", "worst"])
def test_missing_beyond_the_available_members_is_rejected_in_every_mode(bench, tmp_path, mode):
    db = tmp_path / f"short-{mode}.db"
    bench.build(db, 60, 1, "typical", "mixed")
    with pytest.raises(ValueError, match=f"--missing-from {mode}: .* can be left out"):
        bench.write_request(str(db), "messages", 500, 0, tmp_path / "r.json", mode)


def test_upload_total_below_the_retained_members_is_rejected(bench, tmp_path):
    db = tmp_path / "total.db"
    bench.build(db, 60, 1, "typical", "typical")
    with pytest.raises(ValueError, match="--upload-total 10: the upload holds"):
        bench.write_request(str(db), "messages", 2, 0, tmp_path / "r.json", "spread", 10)


def test_report_config_records_every_workload_dimension(report, cardinality):
    keys = {
        "references",
        "extracted_chars",
        "chunks",
        "chunk_tokens",
        "missing",
        "missing_from",
        "extras",
        "upload_total",
        "all_extras",
        "k",
    }
    assert keys <= set(report["config"])
    assert cardinality["config"]["extracted_chars"] == 100
    assert cardinality["config"]["references"] == 1000


def test_commit_each_builds_the_same_corpus_one_message_per_transaction(bench, tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    bench.build(a, 30, 1, "typical", "typical", chunks=2)
    built = bench.build(b, 30, 1, "typical", "typical", chunks=2, commit_each=True)
    assert built["chunks"] == 60
    counts = []
    for db in (a, b):
        with closing(sqlite3.connect(db)) as conn:
            counts.append(
                (
                    conn.execute("SELECT COUNT(*) FROM message_chunks").fetchone()[0],
                    conn.execute("SELECT COUNT(*) FROM message_chunks_fts").fetchone()[0],
                )
            )
    assert counts[0] == counts[1] == (60, 60)


def test_fts_rowids_follow_insertion_order(bench, tmp_path):
    db = tmp_path / "rowids.db"
    bench.build(db, 30, 1, "typical", "typical", chunks=2, commit_each=True)
    with closing(sqlite3.connect(db)) as conn:
        ids = [r[0] for r in conn.execute("SELECT fts_rowid FROM message_chunks ORDER BY rowid")]
    assert ids == list(range(1, 61))


def test_worst_records_carry_the_full_nested_maildir_path(bench, tmp_path):
    db = tmp_path / "paths.db"
    bench.build(db, 20, 1, "typical", "worst")
    with closing(sqlite3.connect(db)) as conn:
        folder, filepath = conn.execute("SELECT folder, filepath FROM messages LIMIT 1").fetchone()
    assert len(folder.encode()) > 3_000 and folder.count("/") == 13
    assert filepath.startswith(f"/maildir/{folder}/cur/")
    assert len(filepath.encode()) <= 4_096 + 600  # the folder path plus the 255-byte file name


@pytest.mark.parametrize(
    ("argv", "ok"),
    [
        (["--repeat", "1"], True),
        (["--repeat", "2"], True),
        (["--repeat", "3"], False),
        (["--repeat", "2", "--filtered"], False),
        (["--repeat", "6", "--filtered"], True),
        (["--repeat", "1", "--filtered"], True),
    ],
)
def test_repeat_count_must_balance_the_orders(bench, argv, ok):

    ns = _ns(repeat=int(argv[1]), filtered="--filtered" in argv)
    if ok:
        bench._require_balanced_repeat(ns)
    else:
        with pytest.raises(SystemExit, match="cannot balance"):
            bench._require_balanced_repeat(ns)


def test_collect_scan_time_includes_ordering_and_hashing(bench, tmp_path):
    db = tmp_path / "scan.db"
    bench.build(db, 200, 1, "typical", "typical")
    r = bench.phase_certificate(str(db), "messages", "collect")
    assert r["fetch_s"] <= r["scan_s"] <= r["total_s"]


def test_upload_total_needs_both_values(bench, tmp_path):
    with pytest.raises(SystemExit):
        bench.main(["--workdir", str(tmp_path), "--messages", "10", "--upload-total", "100"])


def test_chunk_rows_carry_every_production_column(bench, tmp_path):
    db = tmp_path / "cols.db"
    bench.build(db, 10, 1, "typical", "typical", chunks=2)
    with closing(sqlite3.connect(db)) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(message_chunks)")]
        row = conn.execute(
            "SELECT char_start, char_end, token_est, chunked_at FROM message_chunks "
            "WHERE chunk_index = 1 LIMIT 1"
        ).fetchone()
    for name in ("char_start", "char_end", "token_est", "chunked_at"):
        assert name in cols
    assert row[1] > row[0] > 0 and row[2] == 40 and row[3]


@pytest.mark.parametrize("repeat", [0, -2])
def test_nonpositive_repeat_is_refused(bench, repeat):

    with pytest.raises(SystemExit):
        bench._require_balanced_repeat(_ns(repeat=repeat))


def test_report_and_wal_results_record_the_artificial_hold(report):
    assert "wal_hold" in report["config"]
    assert all("hold_s" in w for w in report["wal"])


def test_worst_fields_are_parser_reachable(bench):
    shape = bench._shape(1, "ascii998", "worst", 0)
    assert all("@" in address and address.isascii() for _, address, _ in shape["people"])
    assert shape["in_reply_to"].isascii() and all(r.isascii() for r in shape["references"])
    assert shape["content_type"].isascii()
    assert max(len(a) for _, a, _ in shape["people"]) > 500


@pytest.mark.parametrize("k", [[0], [100, 0], [-5]])
def test_nonpositive_k_is_refused(bench, k):

    with pytest.raises(SystemExit):
        bench._require_balanced_repeat(_ns(repeat=2, k=k))


def _ns(**kw):
    import argparse

    base = {
        "repeat": 2,
        "filtered": False,
        "k": [100],
        "missing": 5,
        "extras": 0,
        "upload_total": None,
        "all_extras": False,
        "records": "typical",
        "per_message": 3,
        "messages": 10,
        "missing_from": "spread",
        "chunk_tokens": 40,
        "chunks": 1,
        "references": 0,
        "writer_interval": [0.0],
        "wal_hold": 0.0,
        "extracted_chars": 0,
        "request_shapes": 0,
        "wal": False,
        "writer_commit_bytes": [131072],
    }
    return argparse.Namespace(**{**base, **kw})


@pytest.mark.parametrize(
    "kw",
    [
        {"missing": -1},
        {"extras": -1},
        {"upload_total": [100, -1]},
        {"upload_total": [0, 300]},
        {"all_extras": True},
        {"all_extras": True, "records": "mixed", "upload_total": [0, 0]},
        {"per_message": -1},
        {"messages": 0},
        {"all_extras": True, "records": "worst"},
    ],
)
def test_invalid_upload_shapes_are_refused_before_building(bench, kw):
    with pytest.raises(SystemExit):
        bench._require_balanced_repeat(_ns(**kw))


def test_all_extras_with_mixed_records_is_accepted(bench):
    bench._require_balanced_repeat(
        _ns(all_extras=True, records="mixed", upload_total=[100, 300], messages=6000)
    )


def test_all_extras_needs_enough_worst_records_for_k(bench):
    with pytest.raises(SystemExit, match="cannot fill K = 100"):
        bench._require_balanced_repeat(
            _ns(all_extras=True, records="mixed", upload_total=[100, 300], messages=1000)
        )


@pytest.mark.parametrize(
    "kw",
    [
        {"missing_from": "worst", "records": "typical"},
        {"filtered": True, "repeat": 6, "chunk_tokens": 3},
        {"chunks": 0},
        {"chunk_tokens": 1},
        {"wal": True, "writer_commit_bytes": [1024, -1]},
    ],
)
def test_incompatible_arguments_are_refused_before_building(bench, kw):
    with pytest.raises(SystemExit):
        bench._require_balanced_repeat(_ns(**kw))


def test_chunk_ids_are_production_sized_digests(bench, tmp_path):
    db = tmp_path / "chunkids.db"
    bench.build(db, 10, 1, "ascii998", "typical", chunks=2)
    with closing(sqlite3.connect(db)) as conn:
        ids = [r[0] for r in conn.execute("SELECT chunk_id FROM message_chunks")]
    assert len(ids) == 20 and len(set(ids)) == 20
    assert all(len(i) == 64 and set(i) <= set("0123456789abcdef") for i in ids)


def test_common_prefix_claimants_share_998_bytes_and_stay_distinct(bench, tmp_path):
    db = tmp_path / "common.db"
    bench.build(db, 12, 1, "ascii998common", "typical")
    with closing(sqlite3.connect(db)) as conn:
        cids = [r[0] for r in conn.execute("SELECT claimant_id FROM messages")]
        threads = {r[0] for r in conn.execute("SELECT thread_id FROM messages")}
    assert len(set(cids)) == 12
    assert len({c[:998] for c in cids}) == 1
    assert all(len(c.encode()) == 998 + 17 for c in cids)
    assert len(threads) == 1  # one Message-ID, one thread, as in production


@pytest.mark.parametrize("identity", ["typical", "ascii998", "ascii998common", "utf8x4"])
def test_claimant_suffix_is_the_stored_content_hash_prefix(bench, tmp_path, identity):
    db = tmp_path / "suffix.db"
    bench.build(db, 12, 1, identity, "typical")
    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute("SELECT claimant_id, content_hash FROM messages").fetchall()
    assert len({h for _, h in rows}) == 12
    assert all(c.rsplit("#", 1)[1] == h[:16] for c, h in rows)


def test_alternate_names_trade_participants_within_the_address_budget(bench, tmp_path):
    db = tmp_path / "names.db"
    built = bench.build(db, 3, 1, "typical", "cardinality_names", references=2)
    unique = bench.MAX_MESSAGE_ADDRESSES - bench.MAX_EXTRA_PARTICIPANT_NAMES
    assert built["participant_rows"] == 3 * unique
    assert built["name_rows"] == 3 * bench.MAX_MESSAGE_ADDRESSES


def test_negative_references_are_refused_before_building(bench):
    with pytest.raises(SystemExit):
        bench._require_balanced_repeat(_ns(references=-1))


@pytest.mark.parametrize("kw", [{"extracted_chars": -1}, {"request_shapes": -1}])
def test_negative_workload_sizes_are_refused_before_building(bench, kw):
    with pytest.raises(SystemExit):
        bench._require_balanced_repeat(_ns(**kw))


def test_wal_windows_use_the_monotonic_clock(bench):
    import inspect

    for fn in (bench.phase_reconcile, bench.phase_writer, bench.run_wal):
        src = inspect.getsource(fn)
        assert "time.time()" not in src
    assert "time.monotonic()" in inspect.getsource(bench.phase_writer)


@pytest.mark.parametrize(
    "kw",
    [
        {"wal": True, "writer_interval": [-1.0]},
        {"wal": True, "wal_hold": float("nan")},
        {"wal": True, "writer_interval": [float("inf")]},
    ],
)
def test_invalid_wal_timing_arguments_are_refused_before_building(bench, kw):
    with pytest.raises(SystemExit):
        bench._require_balanced_repeat(_ns(**kw))


def test_threads_of_four_start_at_a_root_without_a_reply_chain(bench, tmp_path):
    db = tmp_path / "threads.db"
    bench.build(db, 12, 1, "typical", "typical")
    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute(
            "SELECT message_id, thread_id, in_reply_to FROM messages ORDER BY rowid"
        ).fetchall()
    roots = {m: (t, r) for m, t, r in rows}
    for i in range(12):
        mid = bench.message_id(i, "typical")
        if i % 4 == 0:
            assert roots[mid][1] is None
        else:
            assert roots[mid][1] == bench.message_id(i - 1, "typical")
        assert roots[mid][0] == bench.message_id(i - i % 4, "typical")


def test_wal_window_includes_the_size_at_its_close(report):
    for wal in report["wal"]:
        assert wal["wal_max_during_transaction_bytes"] >= wal["wal_at_transaction_start_bytes"]


def test_worst_records_keep_their_assigned_thread_and_flag_ambiguous_senders(bench, tmp_path):
    db = tmp_path / "worst-threads.db"
    bench.build(db, 12, 1, "ascii998", "worst")
    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute(
            "SELECT message_id, thread_id, in_reply_to, references_json, sender_ambiguous "
            "FROM messages"
        ).fetchall()
    for mid, tid, reply, refs, ambiguous in rows:
        assert ambiguous == 1  # eleven From participants
        i = next(n for n in range(12) if bench.message_id(n, "ascii998") == mid)
        if i % 4:
            assert reply == bench.message_id(i - 1, "ascii998")
            assert json.loads(refs)[0] == reply
        assert tid == bench.message_id(i - i % 4, "ascii998")
