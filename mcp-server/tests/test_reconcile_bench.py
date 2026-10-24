"""Keeps the #1218 sizing benchmark (``scripts/reconcile_bench.py``)
runnable and its measurements honest, at a size that runs in seconds."""

import importlib.util
import json
import math
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest
import sqlite_vec

from tests.conftest import _build_schema

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_bench.py"


def _load():
    # Registered before it runs: a dataclass looks its module up there.
    spec = importlib.util.spec_from_file_location("reconcile_bench", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["reconcile_bench"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bench():
    return _load()


@pytest.mark.parametrize("identity", ["typical", "ascii998", "utf8x4"])
def test_message_id_widths(bench, identity):
    ids = [bench.message_id(i, identity) for i in (0, 1, 1023, 1024, 49_999)]
    assert len(set(ids)) == len(ids)
    if identity != "typical":
        assert all(len(i) == bench.MESSAGE_ID_MAX_CHARS for i in ids)
    if identity == "utf8x4":
        # The UTF-8 upper bound: four bytes a character, plus the suffix.
        assert len(bench.claimant_of(ids[0], b"file").encode()) == 4 * 998 + 17


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
            "--writer-batch",
            "4",
            "--writer-interval",
            "0.005",
            # Enough for the round and its warm-up at this pace; the
            # default (20,000) only costs setup time here.
            "--writer-backlog",
            "2000",
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
    assert messages["participant_rows"] == 10 * 30
    assert messages["references"] == 10 * 10
    occurrences = report["reconcile"]["occurrences"]["stream"]["10"]
    assert occurrences["record_bytes_max"] > 500 * 4 + 500
    assert occurrences["participant_rows"] == occurrences["references"] == 0


def test_cardinality_records_transfer_only_the_listed_rows(cardinality):
    # SQLite still visits the stored lists to compute exact totals,
    # but only ten entries per list cross into Python per returned record.
    for k, round_ in cardinality["reconcile"]["messages"]["stream"].items():
        assert round_["returned"] == int(k)
        # One From, ten To and ten Cc entries per message.
        assert round_["participant_rows"] == int(k) * 21
        assert round_["references"] == int(k) * 10


def test_wal_is_reclaimed_after_the_round(bench, report):
    # Both scan methods, so the WAL figures size whichever one ships,
    # each under both writer lifecycles (absent: arrival and drain).
    assert [(w["kind"], w["method"], w["writer_lifecycle"]) for w in report["wal"]] == [
        (kind, method, lifecycle)
        for kind in ("messages", "occurrences")
        for method in ("stream", "collect")
        for lifecycle in ("arrival", "drain")
    ]
    for wal in report["wal"]:
        drain = wal["writer_lifecycle"] == "drain"
        # A drain reports its backlog and what it had left (it did not
        # run out: that fails the measurement); arrival has none.
        assert wal["writer_backlog"] == (2000 if drain else None)
        assert (0 < wal["writer_jobs_left"] < 2000) if drain else wal["writer_jobs_left"] >= 0
        assert wal["writer_extract_s"] == bench.PDF_EXTRACT_SECONDS
        # New mail the warm-up committed joined the snapshot (checked
        # against it in run_wal, which would have failed otherwise).
        assert wal["snapshot_growth"] > 0
    # Less what it added, each snapshot is its kind's corpus.
    for kind in ("messages", "occurrences"):
        corpus = {
            w["snapshot_count"] - w["snapshot_growth"] for w in report["wal"] if w["kind"] == kind
        }
        assert len(corpus) == 1
    assert report["wal_baseline"]["jobs_left"] == 0
    assert report["wal_baseline"]["chunks"] == report["build"]["chunks"]
    for wal in report["wal"]:
        # The explicit checkpoint found the retained frames still in the
        # WAL: no last-connection close reclaimed them first.
        assert wal["wal_before_checkpoint_bytes"] >= wal["wal_max_during_transaction_bytes"] > 0
        commits = wal["commits_during_transaction"]
        assert commits > 0
        # Every frame committed while the round held its snapshot stays
        # in the WAL: the growth over the window (not the size, which
        # holds the warm-up's commits too) is at least one frame (a page
        # and its 24-byte header) for each writing commit inside it.
        growth = wal["wal_max_during_transaction_bytes"] - wal["wal_at_transaction_start_bytes"]
        assert growth >= commits * (4096 + 24) > 0
        assert wal["writer_batch"] == 4
        assert wal["checkpoint_after"]["busy"] == 0
        assert wal["wal_after_checkpoint_bytes"] == 0


def test_failed_round_stops_the_writer(bench, tmp_path):
    # A reader that fails (here: its request file is missing) must not
    # leave the unthrottled writer committing after run_wal returns.
    db = tmp_path / "failed.db"
    bench.build(db, 20, 1, "typical", "typical")

    def commits() -> int:
        # Every writer message adds a messages row after the corpus's 20,
        # in the copy the configuration runs on.
        with closing(sqlite3.connect(str(db) + bench.WAL_COPY_SUFFIX)) as conn:
            return conn.execute("SELECT MAX(rowid) FROM messages").fetchone()[0]

    with pytest.raises(RuntimeError, match="reconcile round failed"):
        bench.run_wal(str(db), "messages", 5, str(tmp_path / "missing.json"), 1, 0.0)
    after = commits()
    assert after > 20
    time.sleep(0.5)
    assert commits() == after
    assert not (tmp_path / ("failed.db" + bench.WAL_COPY_SUFFIX + ".stop")).exists()


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
    # 4242; alpha07 is in every fiftieth body.
    assert texts["gamma0000004242"] == 0
    assert texts["alpha07"] > 0
    vendor = next(
        r for r in report["filtered"]["messages"] if r["filters"] == {"authority_class": "vendor"}
    )
    assert vendor["count"] > 0
    # Body chunks carry at least the 20 tokens real mail does, so the
    # text filters run over a realistic FTS index.
    assert report["build"]["body_tokens_min"] >= 20


def test_request_shapes_count_their_elements(report):
    shapes = report["request_shapes"]
    assert shapes["packed"]["elements"] == shapes["hex_array"]["elements"] == 1000
    # The same byte budget as the hex array holds one element per five
    # bytes (about 13 times as many) as two-character strings, all parsed
    # before any check.
    assert shapes["short_array"]["bytes"] <= shapes["hex_array"]["bytes"]
    # A packed envelope with an ignored member is as small as the packed
    # upload of the same digests yet builds one object per junk string.
    assert shapes["packed_junk"]["bytes"] <= shapes["packed"]["bytes"] + 64
    assert shapes["packed_junk"]["elements"] == (shapes["packed"]["bytes"] - 22) // 5
    assert shapes["short_array"]["elements"] == (67 * 1000 + 12 - 13) // 5
    assert shapes["packed"]["bytes"] < shapes["hex_array"]["bytes"]


@pytest.fixture(scope="module")
def extracted(bench, tmp_path_factory):
    work = tmp_path_factory.mktemp("extracted")
    reports = {}
    for chars in (0, 100):
        reports[chars] = bench.main(
            [
                "--workdir",
                str(work / str(chars)),
                "--messages",
                "40",
                "--per-message",
                "1",
                "--k",
                "3,10",
                "--missing",
                "10",
                "--repeat",
                "1",
                "--extracted-chars",
                str(chars),
            ]
        )
    return reports


def test_occurrence_rounds_read_rows_with_extracted_text(extracted):
    # Increasing cached extraction text must not increase returned record size.
    baseline = extracted[0]["reconcile"]["occurrences"]["stream"]
    stressed = extracted[100]["reconcile"]["occurrences"]["stream"]
    assert extracted[0]["config"]["extracted_chars"] == 0
    assert extracted[100]["config"]["extracted_chars"] == 100
    assert baseline.keys() == stressed.keys()
    for k, round_ in stressed.items():
        assert round_["returned"] == baseline[k]["returned"] > 0
        assert round_["record_bytes_max"] == baseline[k]["record_bytes_max"]


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
            "3",
            "--chunk-tokens",
            "210",
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
    # One name per participant: ``cardinality`` carries no further names.
    assert built["name_rows"] == 40 * bench.MAX_MESSAGE_ADDRESSES


def test_chunk_and_term_cardinality_is_built_and_queried(bench, terms):
    # Three body chunks and one attachment chunk a message, each body
    # chunk at the indexer's chunk target or above.
    assert terms["build"]["chunks"] == 60 * (3 + 1)
    assert bench.CHUNK_TARGET_TOKENS <= terms["build"]["body_tokens_min"]
    assert terms["build"]["body_tokens_max"] <= bench.CHUNK_MAX_TOKENS
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
    bench.phase_filtered(str(db), "messages", {"participant": "from0.007"}, rotate)
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


def test_report_config_records_every_workload_dimension(bench, report, cardinality):
    # Every argument but the work directory, as it was resolved.
    keys = {a.name for a in bench.ARGS} - {"workdir"}
    assert keys == set(report["config"]) - {"sqlite", "python"}
    assert cardinality["config"]["references"] == 1000
    assert report["config"]["writer_interval"] == [0.005]
    assert report["config"]["writer_batch"] == [4]


def test_fts_rowids_follow_insertion_order(bench, tmp_path):
    db = tmp_path / "rowids.db"
    bench.build(db, 30, 1, "typical", "typical", chunks=2)
    with closing(sqlite3.connect(db)) as conn:
        ids = [r[0] for r in conn.execute("SELECT fts_rowid FROM message_chunks ORDER BY rowid")]
    # Two body chunks and one attachment chunk a message.
    assert ids == list(range(1, 91))


def test_worst_records_carry_the_full_nested_maildir_path(bench, tmp_path):
    db = tmp_path / "paths.db"
    bench.build(db, 20, 1, "typical", "worst")
    with closing(sqlite3.connect(db)) as conn:
        folder, filepath = conn.execute(
            "SELECT folder, '/maildir/' || relative_path FROM message_occurrences LIMIT 1"
        ).fetchone()
    assert len(folder.encode()) > 3_000 and folder.count("/") == 13
    assert filepath.startswith(f"/maildir/{bench.disk_folder(folder)}/cur/")
    assert len(filepath.encode()) < 4_096  # Linux PATH_MAX


def test_collect_scan_time_includes_ordering_and_hashing(bench, tmp_path):
    db = tmp_path / "scan.db"
    bench.build(db, 200, 1, "typical", "typical")
    r = bench.phase_certificate(str(db), "messages", "collect")
    assert r["fetch_s"] <= r["scan_s"] <= r["total_s"]


def test_chunk_rows_carry_every_production_column(bench, tmp_path):
    db = tmp_path / "cols.db"
    bench.build(db, 10, 1, "typical", "typical", chunks=2)
    with closing(sqlite3.connect(db)) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(message_chunks)")]
        row = conn.execute(
            "SELECT char_start, char_end, token_est, chunked_at, text FROM message_chunks "
            "WHERE chunk_index = 1 LIMIT 1"
        ).fetchone()
    for name in ("char_start", "char_end", "token_est", "chunked_at"):
        assert name in cols
    assert row[1] > row[0] > 0 and row[2] == bench.token_estimate(row[4]) and row[3]


def test_report_and_wal_results_record_the_artificial_hold(report):
    assert "wal_hold" in report["config"]
    assert all("hold_s" in w for w in report["wal"])
    # The reported transaction is the WAL window, hold included, so
    # its duration matches the commits and growth counted in it.
    assert all(w["reader_transaction_s"] >= w["hold_s"] == 1.0 for w in report["wal"])


def test_worst_fields_are_parser_reachable(bench):
    shape = bench._shape(1, "ascii998", "worst", 0)
    assert all("@" in address and address.isascii() for _, address, _ in shape["people"])
    assert shape["in_reply_to"].isascii() and all(r.isascii() for r in shape["references"])
    assert shape["content_type"].isascii()
    assert max(len(a) for _, a, _ in shape["people"]) > 500


def test_chunk_ids_are_production_sized_digests(bench, tmp_path):
    db = tmp_path / "chunkids.db"
    bench.build(db, 10, 1, "ascii998", "typical", chunks=2)
    with closing(sqlite3.connect(db)) as conn:
        ids = [r[0] for r in conn.execute("SELECT chunk_id FROM message_chunks")]
    assert len(ids) == 30 and len(set(ids)) == 30  # two body chunks and one attachment
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


def test_wal_windows_use_the_monotonic_clock(bench):
    import inspect

    for fn in (bench.phase_reconcile, bench.phase_writer, bench.run_wal):
        src = inspect.getsource(fn)
        assert "time.time()" not in src
    assert "time.monotonic()" in inspect.getsource(bench.phase_writer)


def test_threads_of_four_chain_each_reply_to_the_previous_message(bench, tmp_path):
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


# --- the argument table --------------------------------------------------

# A small valid run; each case below changes it.
_BASE = {"--messages": "300", "--per-message": "1", "--missing": "10", "--repeat": "1", "--k": "3"}

# What a boundary value of an argument needs beside it to be valid, so
# the case tests that argument's own bound and nothing else.
_WITH = {
    "messages": {"low": {"--missing": "0", "--k": "1"}},
    "per_message": {"low": {"--missing": "0"}},
    "references": {"*": {"--records": "cardinality", "--messages": "3", "--missing": "0"}},
    "upload_total": {
        "low": {
            "--all-extras": None,
            "--records": "mixed",
            "--missing": None,
            "--k": "1",
        }
    },
    "writer_batch": {"*": {"--wal": None}},
    "writer_interval": {"*": {"--wal": None}},
    "writer_backlog": {"*": {"--wal": None}},
    "writer_extract_seconds": {"*": {"--wal": None}},
    "wal_hold": {"*": {"--wal": None}},
}


def _argv(changes: dict) -> list[str]:
    options = {**_BASE}
    for flag, value in changes.items():
        if value is None and flag in options:
            del options[flag]
        else:
            options[flag] = value
    argv = []
    for flag, value in options.items():
        argv += [flag] if value is None else [flag, value]
    return argv


def _boundary_cases():
    module = _load()
    cases = []
    for arg in module.ARGS:
        if arg.kind not in ("int", "float", "ints", "floats"):
            continue
        step = 1 if arg.kind in ("int", "ints") else 0.001
        fmt = (lambda v: str(int(v))) if arg.kind in ("int", "ints") else str
        with_ = _WITH.get(arg.name, {})
        for which, bound, sign in (("low", arg.low, -1), ("high", arg.high, 1)):
            if bound is None:
                continue
            extra = {**with_.get("*", {}), **with_.get(which, {})}
            value = fmt(bound)
            if arg.length:
                value = ",".join([value] * arg.length)
            cases.append(pytest.param(arg.flag, value, extra, True, id=f"{arg.name}-{which}"))
            beyond = fmt(bound + sign * step)
            if arg.length:
                beyond = ",".join([beyond] * arg.length)
            cases.append(pytest.param(arg.flag, beyond, extra, False, id=f"{arg.name}-{which}-out"))
        # A value of the wrong type stops argparse.
        cases.append(pytest.param(arg.flag, "x", with_.get("*", {}), False, id=f"{arg.name}-type"))
    return cases


@pytest.mark.parametrize(("flag", "value", "extra", "ok"), _boundary_cases())
def test_every_numeric_argument_is_checked_at_its_bounds(bench, flag, value, extra, ok):
    argv = _argv({**extra, flag: value})
    if ok:
        # The table accepts the bound; a combination rule may still
        # refuse it (a References count at its cap overflows the file
        # limit), but never as out of range.
        try:
            bench.parse_args(argv)
        except SystemExit as refused:
            assert "must be at" not in str(refused)
    else:
        with pytest.raises(SystemExit):
            bench.parse_args(argv)


def test_the_parser_offers_exactly_the_table(bench):
    import argparse

    seen = []
    real = argparse.ArgumentParser.add_argument

    def record(self, *names, **kw):
        seen.extend(n for n in names if n.startswith("--") and n != "--help")
        return real(self, *names, **kw)

    argparse.ArgumentParser.add_argument = record
    try:
        bench.parse_args(_argv({}))
    finally:
        argparse.ArgumentParser.add_argument = real
    assert seen == [a.flag for a in bench.ARGS]


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_floats_must_be_finite(bench, value):
    with pytest.raises(SystemExit, match="finite"):
        bench.parse_args([*_argv({"--wal": None}), f"--wal-hold={value}"])


@pytest.mark.parametrize("value", ["5", "5,6,7"])
def test_upload_total_takes_two_values(bench, value):
    with pytest.raises(SystemExit, match="takes 2 values"):
        bench.parse_args(_argv({"--upload-total": value, "--missing": None}))


@pytest.mark.parametrize(
    ("changes", "name", "absent", "zero"),
    [
        # Absent takes the default the table states; zero is taken as given,
        # or refused where the table's bound excludes it (``--repeat``).
        ({}, "extras", 100, 0),
        ({"--missing": None, "--messages": "6000"}, "missing", 5000, 0),
        ({"--repeat": None}, "repeat", 2, None),
        ({"--repeat": None, "--filtered": None}, "repeat", 6, None),
        (
            {"--records": "cardinality", "--messages": "3", "--missing": "0"},
            "references",
            100_000,
            0,
        ),
        ({"--wal": None}, "wal_hold", 0.0, 0.0),
        # The indexer's steady-state and first-index batch sizes.
        ({"--wal": None}, "writer_batch", [8, 50], None),
        ({"--wal": None}, "writer_interval", [0.0, 0.1], [0.0]),
        ({"--wal": None}, "writer_backlog", 20_000, None),
        ({"--wal": None, "--writer-lifecycle": "arrival"}, "writer_backlog", None, None),
        ({"--wal": None}, "writer_extract_seconds", 0.1, 0.0),
        ({"--wal": None}, "writer_lifecycle", ["arrival", "drain"], None),
        ({}, "extracted_chars", 0, 0),
        ({}, "request_shapes", 0, 0),
    ],
)
def test_absent_and_zero_are_resolved_as_the_table_says(bench, changes, name, absent, zero):
    assert getattr(bench.parse_args(_argv(changes)), name) == absent
    flag = "--" + name.replace("_", "-")
    if zero is None:
        with pytest.raises(SystemExit):
            bench.parse_args(_argv({**changes, flag: "0"}))
    else:
        assert getattr(bench.parse_args(_argv({**changes, flag: "0"})), name) == zero


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"--upload-total": "500,500", "--extras": "3"}, "exclude each other"),
        ({"--all-extras": None, "--records": "mixed", "--missing": None}, "needs --upload-total"),
        ({"--all-extras": None, "--upload-total": "5,5", "--missing": None}, "--records mixed"),
        ({"--all-extras": None, "--upload-total": "5,5", "--records": "mixed"}, "excludes"),
        (
            {
                "--all-extras": None,
                "--upload-total": "5,5",
                "--records": "mixed",
                "--missing": None,
                "--missing-from": "worst",
            },
            "excludes",
        ),
        (
            {
                "--all-extras": None,
                "--upload-total": "5,5",
                "--records": "mixed",
                "--missing": None,
                "--k": "100",
            },
            "cannot fill K = 100",
        ),
        ({"--missing-from": "worst"}, "needs --records mixed"),
        ({"--missing": "286"}, "can leave out 285 messages"),
        ({"--records": "mixed", "--missing-from": "worst", "--missing": "7"}, "can leave out 6"),
        ({"--per-message": "0"}, "can leave out 0 occurrences"),
        ({"--upload-total": "100,100"}, "holds 275 members"),
        ({"--k": "100,100"}, "distinct"),
        ({"--references": "5"}, "cardinality records only"),
        ({"--filtered": None, "--chunk-tokens": "19"}, "at least 20"),
        ({"--filtered": None, "--extracted-chars": "5"}, "excludes --extracted-chars"),
        ({"--chunks": "2"}, "one chunk per paragraph"),
        ({"--chunks": "2", "--chunk-tokens": "199"}, "one chunk per paragraph"),
        ({"--extracted-chars": "5", "--per-message": "0", "--missing": "0"}, "--per-message"),
        ({"--records": "mixed", "--missing-from": "worst", "--missing": "0"}, "leaves nothing"),
        ({"--writer-batch": "5"}, "need --wal"),
        ({"--writer-interval": "0.5"}, "need --wal"),
        ({"--wal-hold": "1"}, "need --wal"),
        ({"--writer-lifecycle": "drain"}, "need --wal"),
        ({"--writer-backlog": "5"}, "need --wal"),
        ({"--writer-extract-seconds": "1"}, "need --wal"),
        (
            {"--wal": None, "--writer-lifecycle": "arrival", "--writer-backlog": "5"},
            "needs --writer-lifecycle drain",
        ),
        ({"--wal": None, "--writer-lifecycle": "drain,drain"}, "distinct"),
        ({"--wal": None, "--writer-lifecycle": "steady"}, "arrival or drain"),
        ({"--wal": None, "--writer-lifecycle": ""}, "arrival or drain"),
        ({"--wal": None, "--writer-batch": "4,4"}, "distinct"),
        ({"--wal": None, "--writer-interval": "0,0.0"}, "distinct"),
        ({"--repeat": "3"}, "cannot balance"),
        ({"--repeat": "2", "--filtered": None}, "cannot balance"),
        (
            {
                "--records": "cardinality",
                "--references": "3000000",
                "--messages": "3",
                "--missing": "0",
            },
            "file limit",
        ),
    ],
)
def test_forbidden_combinations_are_refused_before_building(bench, tmp_path, changes, message):
    with pytest.raises(SystemExit, match=message):
        bench.parse_args(_argv({**changes, "--workdir": str(tmp_path)}))
    assert not list(tmp_path.iterdir())


def _wal_argv(changes: dict, tmp_path) -> list[str]:
    # A small valid run with --wal; each case changes it.
    return _argv({"--wal": None, "--missing": "0", **changes, "--workdir": str(tmp_path)})


def _estimate(bench, changes: dict, tmp_path) -> float:
    # Parsed at one message (within any budget), then sized.
    args = bench.parse_args(_wal_argv({**changes, "--messages": "1"}, tmp_path))
    args.messages = int(changes.get("--messages", "300"))
    return bench.wal_baseline_estimate(args)


def _edge(bench, tmp_path, flag: str, low: int, high: int, fixed: dict) -> int:
    """The largest value of ``flag`` (in ``[low, high]``) whose estimate
    is within the budget, the other arguments ``fixed``."""
    while low < high:
        mid = (low + high + 1) // 2
        if _estimate(bench, {**fixed, flag: str(mid)}, tmp_path) <= bench.WAL_BASELINE_BUDGET_S:
            low = mid
        else:
            high = mid - 1
    return low


def _refused(bench, argv) -> str | None:
    try:
        bench.parse_args(argv)
    except SystemExit as refused:
        return str(refused)
    return None


@pytest.fixture
def no_build(bench, monkeypatch):
    # Refused or accepted, parsing the arguments builds nothing.
    monkeypatch.setattr(bench, "build", lambda *a, **k: pytest.fail("built"))
    monkeypatch.setattr(bench, "build_wal_baseline", lambda *a, **k: pytest.fail("built"))


def test_the_documented_wal_run_is_within_the_budget(bench, tmp_path, no_build):
    # docs/mcp-tools.md's command (50,000 typical messages, three PDFs
    # each): estimated at about eight minutes, well inside 30.
    argv = ["--workdir", str(tmp_path), "--messages", "50000", "--identity", "typical"]
    args = bench.parse_args([*argv, "--wal", "--filtered"])
    assert bench.wal_baseline_estimate(args) < bench.WAL_BASELINE_BUDGET_S / 2
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("flag", "low", "high", "fixed"),
    [
        # Attachments per message, the dominant term.
        ("--per-message", 1, 9998, {"--messages": "5000"}),
        # Bytes written per message: the extraction rows' stress text.
        ("--extracted-chars", 0, 10_000_000, {"--messages": "2000", "--per-message": "1"}),
        # Body chunks (paragraphs of 250 words: one chunk each).
        (
            "--chunks",
            1,
            4000,
            {"--messages": "5000", "--per-message": "0", "--chunk-tokens": "250"},
        ),
        # The common-ID thread's growth.
        ("--messages", 1, 100_000, {"--identity": "ascii998common", "--per-message": "1"}),
    ],
)
def test_the_wal_baseline_budget_holds_along_each_dimension(
    bench, tmp_path, no_build, flag, low, high, fixed
):
    edge = _edge(bench, tmp_path, flag, low, high, fixed)
    assert low < edge < high
    under = _wal_argv({**fixed, flag: str(edge)}, tmp_path)
    over = _wal_argv({**fixed, flag: str(edge + 1)}, tmp_path)
    assert _refused(bench, under) is None
    assert "estimated" in (_refused(bench, over) or "")
    # Without --wal no baseline is built: the same corpus is accepted.
    plain = [a for a in over if a != "--wal"]
    assert _refused(bench, plain) is None
    assert not list(tmp_path.iterdir())


def test_the_wal_baseline_budget_adds_its_dimensions(bench, tmp_path, no_build):
    # Attachments and chunks each within the budget alone, over it
    # together: one estimate, not one cap a dimension.
    fixed = {"--messages": "5000", "--chunk-tokens": "250"}
    attachments = _edge(bench, tmp_path, "--per-message", 1, 9998, fixed)
    chunks = _edge(bench, tmp_path, "--chunks", 1, 4000, {**fixed, "--per-message": "0"})
    a, c = str(attachments * 2 // 3), str(chunks * 2 // 3)
    assert _refused(bench, _wal_argv({**fixed, "--per-message": a}, tmp_path)) is None
    assert (
        _refused(bench, _wal_argv({**fixed, "--per-message": "0", "--chunks": c}, tmp_path)) is None
    )
    together = _wal_argv({**fixed, "--per-message": a, "--chunks": c}, tmp_path)
    assert "estimated" in (_refused(bench, together) or "")
    # The finding's example.
    example = _wal_argv(
        {"--identity": "ascii998common", "--messages": "5000", "--per-message": "9998"}, tmp_path
    )
    assert "estimated" in (_refused(bench, example) or "")
    assert not list(tmp_path.iterdir())


def test_the_largest_accepted_combinations_pass(bench):
    bench.parse_args(_argv({"--chunks": "3", "--chunk-tokens": "250"}))
    bench.parse_args(
        _argv(
            {
                "--all-extras": None,
                "--upload-total": "5,5",
                "--records": "mixed",
                "--missing": None,
                "--k": "6",
            }
        )
    )
    bench.parse_args(_argv({"--records": "mixed", "--missing-from": "worst", "--missing": "6"}))


def test_wal_window_takes_sizes_and_count_from_the_same_commits(bench):
    commits = [(1.0, 100), (2.0, 200), (3.0, 350), (4.0, 500), (5.0, 650)]
    # A commit stamped exactly at the start belongs before the window;
    # one stamped at the end, inside it.
    assert bench.wal_window(commits, 2.0, 4.0) == (200, 500, 2)
    assert bench.wal_window(commits, 2.5, 2.6) == (200, 200, 0)
    assert bench.wal_window(commits, 0.5, 1.0) == (0, 100, 1)


def test_build_commits_each_message_on_its_own(bench, tmp_path, monkeypatch):
    statements: list[str] = []
    real = bench.sqlite3.connect

    def traced(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(bench.sqlite3, "connect", traced)
    bench.build(tmp_path / "cadence.db", 12, 1, "typical", "typical")
    assert statements.count("COMMIT") == 12 and statements.count("BEGIN") == 12


def test_worst_replies_name_their_parent_last_and_one_sender(bench, tmp_path):
    db = tmp_path / "worst-threads.db"
    bench.build(db, 12, 1, "ascii998", "worst")
    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute(
            "SELECT message_id, thread_id, references_json, sender_ambiguous FROM messages"
        ).fetchall()
    for mid, tid, refs, ambiguous in rows:
        # One From header with eleven authors: not ambiguous (the parser
        # flags a repeated From header or an incomplete scan only).
        assert ambiguous == 0
        i = next(n for n in range(12) if bench.message_id(n, "ascii998") == mid)
        if i % 4:
            assert json.loads(refs)[-1] == bench.message_id(i - 1, "ascii998")
        assert tid == bench.message_id(i - i % 4, "ascii998")


@pytest.mark.parametrize(
    "records", ["typical", "worst", "mixed", "cardinality", "cardinality_names"]
)
@pytest.mark.parametrize("identity", ["typical", "ascii998", "ascii998common", "utf8x4"])
def test_shapes_repeat_with_the_period(bench, identity, records):
    # The pre-build checks read the first SHAPE_PERIOD messages only:
    # every later message has the size and token counts of its residue.
    period = bench.SHAPE_PERIOD
    for i in [*range(period, 2 * period), 2**30 - 1, 10**9 + 7]:
        j = i % period
        for parts in (0, 2):
            assert len(bench.render_eml(i, identity, records, 3, 2, 210, parts)) == len(
                bench.render_eml(j, identity, records, 3, 2, 210, parts)
            )
        for c in (0, 1):
            assert bench.token_estimate(bench._paragraph(i, c, 210)) == bench.token_estimate(
                bench._paragraph(j, c, 210)
            )


def test_file_size_check_is_exact_for_many_parts(bench):
    # n parts cost n - 1 times the second part's bytes more than one.
    args = bench.parse_args(_argv({"--per-message": "7", "--messages": "120"}))
    assert bench._largest_file(args) == max(
        len(bench.render_eml(i, "typical", "typical", 0, 1, bench.BODY_TOKENS, 7))
        for i in range(120)
    )


def test_writer_indexes_each_batch_into_its_folders(bench, tmp_path):
    # One batch of three new messages (the corpus's numbering goes on
    # in threads of four: a root and two replies), each with a body
    # chunk and an attachment: the commits the indexer makes for them
    # (indexer/tests/test_reconcile_bench_fidelity.py compares the
    # sequence with the indexer's own), and the rows it leaves, each
    # message in its own folder, so the round's sets grow by the ones
    # outside Trash, as new mail joins them in production.
    db = tmp_path / "batch.db"
    bench.build(db, 8, 1, "typical", "typical")
    counts = [bench.scan_sql(kind)[::2] for kind in ("messages", "occurrences")]
    with closing(sqlite3.connect(db)) as conn:
        before = [conn.execute(sql, params).fetchone()[0] for sql, params in counts]
    stop = tmp_path / "stop"
    timer = _stop_after_batch(db, stop, 11)
    out = bench.phase_writer(str(db), str(stop), 3, 3600.0, shape={"per_message": 1})
    timer.join()
    # Per message: enqueue, charge, phase 1, refund, charge, refund,
    # phase 2c, delete; per batch of several: one interrupted mark.
    assert out["commits"] == 8 * 3 + 1
    with closing(sqlite3.connect(db)) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        added = conn.execute(
            "SELECT o.folder, m.body_complete, m.thread_id FROM messages m "
            "JOIN message_occurrences o ON o.claimant_id = m.claimant_id "
            "WHERE m.rowid > 8 ORDER BY m.rowid"
        ).fetchall()
        assert [(f, c) for f, c, _ in added] == [(bench._folder(i), 1) for i in (8, 9, 10)]
        assert {t for _, _, t in added} == {bench.message_id(8, "typical")}
        assert conn.execute("SELECT COUNT(*) FROM indexing_jobs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 1
        # A body chunk and an attachment chunk a message, each with its
        # vector, all in the thread's sum.
        assert conn.execute("SELECT COUNT(*) FROM message_chunks_vec").fetchone()[0] == 6
        assert conn.execute("SELECT count FROM thread_vector_sums").fetchone()[0] == 6
        assert conn.execute("SELECT COUNT(*) FROM attachments WHERE rowid > 8").fetchone()[0] == 3
        # None of them is filed in Trash, so each joins the round's sets.
        assert "Trash" not in {bench._folder(i) for i in (8, 9, 10)}
        after = [conn.execute(sql, params).fetchone()[0] for sql, params in counts]
        assert after == [n + 3 for n in before]
        # The thread keeps the folder of the message that created it.
        assert conn.execute("SELECT folder FROM threads").fetchone()[0] == bench._folder(8)


def test_writer_stores_the_extraction_stress_text_as_build_does(bench, tmp_path, monkeypatch):
    # ``--extracted-chars``: every extraction row holds that many
    # four-byte characters and no attachment chunk is built, so, as in
    # the indexer, the empty attachment slice writes no thread sum.
    db = tmp_path / "stress.db"
    bench.build(db, 8, 1, "typical", "typical", extracted_chars=5)
    statements: list[str] = []
    real = bench.sqlite3.connect

    def traced(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(bench.sqlite3, "connect", traced)
    stop = tmp_path / "stop"
    timer = _stop_after_batch(db, stop, 9)
    shape = {"per_message": 1, "extracted_chars": 5}
    bench.phase_writer(str(db), str(stop), 1, 3600.0, shape=shape)
    timer.join()
    with closing(sqlite3.connect(db)) as conn:
        texts = {r[0] for r in conn.execute("SELECT extracted_text FROM attachment_extractions")}
        kinds = [r[0] for r in conn.execute("SELECT kind FROM message_chunks WHERE rowid > 8")]
    assert texts == {bench._WIDE * 5}
    assert kinds == ["body"]
    # The body's sum in phase 2c; the new thread's empty sum in phase 1.
    sums = [s for s in statements if s.startswith("INSERT INTO thread_vector_sums")]
    assert ["DO UPDATE" in s for s in sums] == [False, True]


def test_writer_stops_mid_batch_once_stop_appears(bench, tmp_path):
    # The round is over once ``stop`` exists, so the writer makes no
    # further commit: a long batch is not left to run into run_wal's
    # shutdown timeout.
    db = tmp_path / "long.db"
    bench.build(db, 8, 1, "typical", "typical")
    stop = tmp_path / "stop"
    timer = _stop_after_ready(stop, 0.2)
    started = time.monotonic()
    out = bench.phase_writer(str(db), str(stop), 1000, 0.0, shape={"per_message": 1})
    timer.join()
    assert time.monotonic() - started < 30
    assert 0 < out["commits"] < 8 * 1000 + 1
    assert len(out["commit_times"]) == out["commits"] + 1


def test_a_writer_that_fails_before_it_is_ready_fails_the_measurement(bench, tmp_path):
    # A ``threads`` table without the indexer's columns: the writer's
    # setup fails, and run_wal says so as soon as the writer exits.
    db = tmp_path / "unready.db"
    bench.build(db, 8, 0, "typical", "typical")
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE threads (x)")
        conn.commit()
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"hashes": ""}))
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="did not get ready"):
        bench.run_wal(str(db), "messages", 5, str(request), 1, 0.0)
    assert time.monotonic() - started < 30
    assert not (tmp_path / ("unready.db" + bench.WAL_COPY_SUFFIX + ".stop")).exists()


def test_writer_holds_one_message_at_a_time(bench, tmp_path, monkeypatch):
    # A batch's records are made where each is used, not all before the
    # first commit and share one stress text, and thread state is read
    # back from the index as the indexer does: memory does not grow with
    # the batch's messages.
    import weakref

    db = tmp_path / "memory.db"
    bench.build(db, 8, 1, "typical", "typical")
    live: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
    most = 0
    stress: set[int] = set()
    real = bench._record

    def counted(*args):
        nonlocal most
        rec = real(*args)
        live[id(rec)] = rec
        most = max(most, len(live))
        stress.update(id(a[3]) for a in rec.attachments)
        return rec

    monkeypatch.setattr(bench, "_record", counted)
    stop = tmp_path / "stop"
    timer = _stop_after_batch(db, stop, 8 + 50)
    shape = {"per_message": 2, "extracted_chars": 5}
    bench.phase_writer(str(db), str(stop), 50, 3600.0, shape=shape)
    timer.join()
    # The record in use and the one being made.
    assert most <= 2
    assert len(stress) == 1


def test_writer_prepares_each_message_before_its_transactions(bench, tmp_path, monkeypatch):
    # The indexer parses, chunks and embeds before its write
    # transactions, so the writer renders each message, builds its
    # chunks and makes its vectors outside every transaction (and the
    # order lock): a transaction's span is its writes only.
    from contextlib import contextmanager

    db = tmp_path / "prepare.db"
    bench.build(db, 8, 1, "typical", "typical")
    inside = False
    real_ordered = bench._ordered

    @contextmanager
    def ordered(order):
        nonlocal inside
        with real_ordered(order):
            inside = True
            try:
                yield
            finally:
                inside = False

    made: list[bool] = []
    real_record, real_vector = bench._record, bench._unit_vector

    def record(*args):
        made.append(inside)
        return real_record(*args)

    def vector(rng):
        made.append(inside)
        return real_vector(rng)

    monkeypatch.setattr(bench, "_ordered", ordered)
    monkeypatch.setattr(bench, "_record", record)
    monkeypatch.setattr(bench, "_unit_vector", vector)
    stop = tmp_path / "stop"
    timer = _stop_after_batch(db, stop, 8 + 4)
    bench.phase_writer(str(db), str(stop), 4, 3600.0, shape={"per_message": 2, "chunks": 2})
    timer.join()
    # Two records a message (phase 1, phase 2c) and the vector pool.
    assert len(made) == 2 * 4 + bench.VECTOR_POOL
    assert not any(made)


def test_drain_enqueues_its_backlog_before_it_is_ready_and_drains_it(bench, tmp_path, monkeypatch):
    # Owner decision 3's ``drain`` (a first index): the backlog is
    # enqueued before the writer is ready, so its commit is outside any
    # window, then drained oldest first, a batch at a time, with no
    # enqueue commit; once none is due the writer records the time and
    # commits nothing more.
    db = tmp_path / "drain.db"
    bench.build(db, 8, 1, "typical", "typical")
    enqueued_at: list[float] = []
    real_backlog = bench._writer_backlog

    def backlog(*args):
        real_backlog(*args)
        enqueued_at.append(time.monotonic())

    monkeypatch.setattr(bench, "_writer_backlog", backlog)
    stop = tmp_path / "stop"
    timer = _stop_after_batch(db, stop, 8 + 6)
    out = bench.phase_writer(
        str(db),
        str(stop),
        4,
        0.0,
        shape={"per_message": 1},
        lifecycle="drain",
        backlog=6,
    )
    timer.join()
    # The backlog was written before the first stamp (the ready state).
    assert len(enqueued_at) == 1 and enqueued_at[0] <= out["commit_times"][0][0]
    # A batch of four, then one of two: per message charge, phase 1,
    # refund, charge, refund, phase 2c, delete; each batch's mark.
    assert out["commits"] == (7 * 4 + 1) + (7 * 2 + 1)
    assert out["jobs_left"] == 0
    assert out["backlog_exhausted_at"] >= out["commit_times"][-1][0]
    with closing(sqlite3.connect(db)) as conn:
        ids = [r[0] for r in conn.execute("SELECT message_id FROM messages WHERE rowid > 8")]
    due = sorted(range(8, 14), key=lambda i: (bench.sent_at(i), i))
    assert ids == [bench.message_id(i, "typical") for i in due]


def test_drain_records_exhaustion_at_its_last_commit_whatever_the_pause(bench, tmp_path):
    # The backlog's last job deleted, the writer is idle from that
    # commit: its stamp (taken under the order lock, like the round's
    # window) is the time, not a clock read at the next claim after the
    # pause, which ``stop`` can pre-empt, or after the empty check, which
    # the round's end can precede.
    db = tmp_path / "pause.db"
    bench.build(db, 8, 1, "typical", "typical")
    stop = tmp_path / "stop"
    timer = _stop_after_batch(db, stop, 8 + 1)
    out = bench.phase_writer(
        str(db), str(stop), 1, 3600.0, shape={"per_message": 1}, lifecycle="drain", backlog=1
    )
    timer.join()
    assert out["jobs_left"] == 0
    assert out["backlog_exhausted_at"] is not None
    assert out["backlog_exhausted_at"] == out["commit_times"][-1][0]


def test_arrival_never_runs_out(bench, tmp_path):
    # ``arrival`` enqueues new mail whenever nothing is due, so it has no
    # backlog to exhaust.
    import threading

    db = tmp_path / "arrival.db"
    bench.build(db, 8, 1, "typical", "typical")
    stop = tmp_path / "stop"

    def stop_after_three_batches() -> None:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if Path(str(stop) + ".ready").exists():
                with closing(sqlite3.connect(db, timeout=30)) as conn:
                    if conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] >= 8 + 3:
                        break
            time.sleep(0.01)
        stop.touch()

    timer = threading.Thread(target=stop_after_three_batches)
    timer.start()
    out = bench.phase_writer(str(db), str(stop), 1, 0.0, shape={"per_message": 1})
    timer.join()
    # Two whole batches of one (ten commits each) and a third under way,
    # each queued when nothing was due.
    assert out["commits"] > 2 * 10
    assert out["backlog_exhausted_at"] is None


def test_a_drain_that_runs_out_before_the_round_ends_fails_the_measurement(bench, tmp_path):
    # An idle writer for part of the window would understate the WAL
    # growth, so a backlog that runs out before the round ends fails.
    db = tmp_path / "short.db"
    bench.build(db, 20, 0, "typical", "typical")
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"hashes": ""}))
    with pytest.raises(RuntimeError, match="backlog ran out before the round ended"):
        bench.run_wal(str(db), "messages", 5, str(request), 1, 0.0, lifecycle="drain", backlog=1)
    assert not (tmp_path / ("short.db" + bench.WAL_COPY_SUFFIX + ".stop")).exists()


def test_a_failed_wal_configuration_leaves_no_copy_behind(bench, tmp_path):
    # The WAL copy (and its -wal and -shm files) can be as large as the
    # index: a configuration that fails (here a drain whose backlog runs
    # out) still removes it.
    with pytest.raises(RuntimeError, match="backlog ran out"):
        bench.main(
            _argv(
                {
                    "--workdir": str(tmp_path),
                    "--wal": None,
                    "--writer-batch": "1",
                    "--writer-interval": "0",
                    "--writer-lifecycle": "drain",
                    "--writer-backlog": "1",
                }
            )
        )
    assert not [p.name for p in tmp_path.iterdir() if bench.WAL_COPY_SUFFIX in p.name]


def test_writer_makes_no_commit_once_stop_appears_while_it_waits_for_the_lock(bench, tmp_path):
    # ``stop`` created while the writer is blocked on the order lock (the
    # round holds it at its window's end): once it gets the lock it
    # begins nothing, so every commit is stamped before ``stop``.
    import fcntl
    import threading

    db = tmp_path / "locked.db"
    bench.build(db, 8, 0, "typical", "typical")
    stop = tmp_path / "stop"
    order = tmp_path / "order"
    stopped_at: list[float] = []

    def stop_under_the_lock() -> None:
        ready = Path(str(stop) + ".ready")
        deadline = time.monotonic() + 120
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)
        with open(order, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            # The writer, committing without pause, is now waiting.
            time.sleep(0.3)
            stop.touch()
            stopped_at.append(time.monotonic())
            fcntl.flock(f, fcntl.LOCK_UN)

    timer = threading.Thread(target=stop_under_the_lock)
    timer.start()
    out = bench.phase_writer(str(db), str(stop), 1, 0.0, order=str(order))
    timer.join()
    assert out["commits"] > 0
    assert all(when < stopped_at[0] for when, _ in out["commit_times"])


def test_writer_rolls_back_a_transaction_during_which_stop_appeared(bench, tmp_path, monkeypatch):
    # ``stop`` created while a transaction runs (a heavy phase 1, say):
    # the transaction is rolled back, not committed.
    db = tmp_path / "mid.db"
    bench.build(db, 8, 0, "typical", "typical")
    stop = tmp_path / "stop"
    real = bench._writer_phase1

    def phase1(conn, rec, now):
        real(conn, rec, now)
        stop.touch()

    monkeypatch.setattr(bench, "_writer_phase1", phase1)
    out = bench.phase_writer(str(db), str(stop), 1, 0.0)
    # The enqueue and the charge committed; phase 1 did not.
    assert out["commits"] == 2
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 8
        assert conn.execute("SELECT attempts FROM indexing_jobs").fetchall() == [(1,)]


def test_writer_chunk_vectors_stay_distinct_past_the_pool(bench, tmp_path):
    # Each chunk has its own text, so its own embedding: repeated vectors
    # would make the thread's encoded sum shorter than independent ones.
    # A thread of four messages with 20 attachments each has 84 chunks,
    # more than the pool's 64 vectors.
    import struct

    db = tmp_path / "distinct.db"
    bench.build(db, 8, 1, "typical", "typical")
    stop = tmp_path / "stop"
    timer = _stop_after_batch(db, stop, 8 + 4)
    bench.phase_writer(str(db), str(stop), 4, 3600.0, shape={"per_message": 20})
    timer.join()
    with closing(sqlite3.connect(db)) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        blobs = [r[0] for r in conn.execute("SELECT embedding FROM message_chunks_vec")]
    assert len(blobs) == 4 * 21 > bench.VECTOR_POOL
    assert len(set(blobs)) == len(blobs)
    for blob in blobs:
        norm = math.sqrt(sum(v * v for v in struct.unpack(f"{bench.EMBEDDING_DIM}f", blob)))
        assert abs(norm - 1.0) < 1e-5


def test_run_wal_publishes_stop_under_the_order_lock(bench):
    # A commit under way when the round ends either finishes before
    # ``stop`` exists or sees it (and rolls back): the marker is
    # published under the lock that serializes the writer's commits.
    import inspect

    src = inspect.getsource(bench.run_wal)
    end = src[src.index("finally:") :]
    lock = end.index("with _ordered(order):")
    assert lock < end.index("stop.touch()") < end.index("writer.communicate")
    assert src.count("stop.touch()") == 1


def test_a_writer_that_cannot_be_told_to_stop_is_reaped(bench, tmp_path, monkeypatch):
    # Publishing ``stop`` can fail (a full disk after the writer grew the
    # WAL): the writer is then killed and reaped, not left committing.
    import errno

    db = tmp_path / "full.db"
    bench.build(db, 20, 0, "typical", "typical")
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"hashes": ""}))
    real_touch = Path.touch

    def touch(self, *args, **kwargs):
        if self.name.endswith(".stop"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_touch(self, *args, **kwargs)

    writers = []
    real_popen = bench.subprocess.Popen

    def popen(argv, *args, **kwargs):
        process = real_popen(argv, *args, **kwargs)
        if "writer" in argv:
            writers.append(process)
        return process

    monkeypatch.setattr(Path, "touch", touch)
    monkeypatch.setattr(bench.subprocess, "Popen", popen)
    try:
        with pytest.raises(OSError, match="No space left"):
            bench.run_wal(str(db), "messages", 5, str(request), 1, 0.0)
        (writer,) = writers
        reaped = writer.returncode is not None
    finally:
        # A writer left running would commit until the disk fills.
        for process in writers:
            if process.poll() is None:
                process.kill()
                process.communicate()
    assert reaped
    assert not (tmp_path / ("full.db" + bench.WAL_COPY_SUFFIX + ".stop.ready")).exists()


@pytest.fixture(scope="module")
def baseline(bench, tmp_path_factory):
    """A small corpus (mixed records: worst-case rows in ``bench_worst``)
    and its WAL baseline."""
    work = tmp_path_factory.mktemp("baseline")
    corpus = work / "corpus.db"
    built = bench.build(corpus, 60, 2, "typical", "mixed")
    base = str(work / "base.db")
    shape = {"identity": "typical", "records": "mixed", "per_message": 2}
    return corpus, base, built, bench.build_wal_baseline(str(corpus), base, 60, shape)


def test_wal_baseline_fills_every_indexer_table(bench, baseline):
    # The corpus indexed again by the writer's commits: the same
    # identities (each kind's certificate) and counts ``build`` reports,
    # the same worst-case rows, and every table a deployed index fills
    # holding them, with the queue empty.
    corpus, base, built, report = baseline
    for kind in ("messages", "occurrences"):
        assert (
            bench.phase_certificate(base, kind, "stream")["digest"]
            == bench.phase_certificate(str(corpus), kind, "stream")["digest"]
        )
    for key in ("chunks", "participant_rows", "name_rows", "body_tokens_min", "body_tokens_max"):
        assert report[key] == built[key]
    assert report["jobs_left"] == 0
    with closing(sqlite3.connect(corpus)) as c, closing(sqlite3.connect(base)) as conn:
        assert sorted(conn.execute("SELECT identity FROM bench_worst")) == sorted(
            c.execute("SELECT identity FROM bench_worst")
        )
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)

        def count(table: str) -> int:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

        assert count("bench_worst") > 0
        assert count("threads") == count("threads_vec") == count("thread_vector_sums") == 15
        assert count("message_thread_map") == count("indexed_files") == 60
        assert count("message_chunks_vec") == built["chunks"]
        assert count("entity_aliases") > 0
        assert count("indexing_jobs") == 0
        assert conn.execute("SELECT MIN(length(sum)) FROM thread_vector_sums").fetchone()[0] > 0


def test_writer_vectors_stay_distinct_from_the_baselines(bench, baseline, tmp_path):
    # The writer's vector stream goes on after the chunks the index
    # already holds, so new mail (in the common-ID shape, added to the
    # same thread) never repeats a baseline vector.
    _corpus, base, built, _report = baseline
    copy = tmp_path / "copy.db"
    with closing(sqlite3.connect(base)) as src, closing(sqlite3.connect(copy)) as dst:
        src.backup(dst)
    stop = tmp_path / "stop"
    timer = _stop_after_batch(copy, stop, 60 + 4)
    shape = {"identity": "typical", "records": "mixed", "per_message": 2}
    bench.phase_writer(str(copy), str(stop), 4, 3600.0, shape=shape)
    timer.join()
    with closing(sqlite3.connect(copy)) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        blobs = [r[0] for r in conn.execute("SELECT embedding FROM message_chunks_vec")]
    assert len(blobs) == built["chunks"] + 4 * 3
    assert len(set(blobs)) == len(blobs)


def test_chunk_vectors_never_repeat_past_the_rotations(bench):
    # Past every rotation of the pool the stream goes on with distinct
    # unit vectors (sign patterns), so no accepted thread's chunk count
    # repeats an embedding. One pool vector: 4,096 rotations a cycle.
    import hashlib
    import random
    import struct

    pool = [bench._unit_vector(random.Random(1))]
    seen = set()
    for n, vector in enumerate(bench._chunk_vectors(pool)):
        if n == 3 * bench.EMBEDDING_DIM + 50:
            break
        seen.add(hashlib.sha256(vector).digest())
        if n % 1000 == 0:
            values = struct.unpack(f"{bench.EMBEDDING_DIM}f", vector)
            assert abs(math.sqrt(sum(v * v for v in values)) - 1.0) < 1e-5
    assert len(seen) == 3 * bench.EMBEDDING_DIM + 50
    # ``start`` resumes the same stream.
    tail = bench._chunk_vectors(pool, 2 * bench.EMBEDDING_DIM + 7)
    whole = bench._chunk_vectors(pool)
    for _ in range(2 * bench.EMBEDDING_DIM + 7):
        next(whole)
    assert next(tail) == next(whole)


def test_writer_goes_on_from_the_next_corpus_message(bench, tmp_path):
    # A corpus that ends mid-thread (nine messages: 8 starts a thread)
    # is continued at message 9, a reply in that thread, not at 12.
    db = tmp_path / "nine.db"
    bench.build(db, 9, 0, "typical", "typical")
    stop = tmp_path / "stop"
    timer = _stop_after_batch(db, stop, 9 + 1)
    bench.phase_writer(str(db), str(stop), 1, 3600.0)
    timer.join()
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute(
            "SELECT message_id, thread_id FROM messages WHERE rowid > 9"
        ).fetchall() == [(bench.message_id(9, "typical"), bench.message_id(8, "typical"))]


def test_measured_statements_read_no_table_only_the_wal_baseline_has(bench, baseline, monkeypatch):
    # ``build``'s index has no table of ``_WRITER_SCHEMA``, so the read
    # figures stand only while no measured statement reads one: every
    # measured phase runs here on the baseline, which has them all, and
    # the authorizer records every table read.
    from src.lib.sqlite import Database

    corpus, base, _built, _report = baseline
    read: set[str] = set()
    real = Database._connect

    def connect(self):
        conn = real(self)

        def authorize(action, table, _column, _db, _source):
            if action == sqlite3.SQLITE_READ and table:
                read.add(table)
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        return conn

    monkeypatch.setattr(Database, "_connect", connect)
    bench.phase_idle(base)
    for kind in ("messages", "occurrences"):
        request = corpus.parent / f"request-{kind}.json"
        bench.write_request(base, kind, 5, 3, request)
        for method in ("stream", "collect"):
            bench.phase_certificate(base, kind, method)
            bench.phase_reconcile(base, kind, 3, str(request), method)
        bench.phase_reconcile(base, kind, 3, str(request), worst_first=True)
        filters = bench.MESSAGE_FILTERS if kind == "messages" else bench.OCCURRENCE_FILTERS
        for spec in filters:
            bench.phase_filtered(base, kind, bench._fill_thread(spec, "typical"))
    assert "messages" in read and "attachments" in read
    writer_only = {
        t for t in read if any(t == w or t.startswith(w + "_") for w in bench.WRITER_TABLES)
    }
    assert writer_only == set()


def test_visibility_selects_the_rows_committed_before_the_snapshot(bench):
    visible = [(1.0, 60, 120), (2.0, 61, 120), (3.0, 61, 122), (4.0, 62, 122)]
    assert bench._visible_at(visible, float("-inf")) == (60, 120)
    assert bench._visible_at(visible, 0.5) == (60, 120)
    # A commit stamped at the snapshot was made before it (``wal_window``).
    assert bench._visible_at(visible, 3.0) == (61, 122)
    assert bench._visible_at(visible, 9.0) == (62, 122)


def test_round_problems_name_every_field_that_differs(bench):
    expected = {
        "count": 10,
        "certificate": "c",
        "missing_total": 4,
        "extras": 2,
        "selected": "s",
    }
    good = {
        "count": 10,
        "scanned": 10,
        "certificate": "c",
        "missing_total": 4,
        "extras": 2,
        "returned": 3,
        "complete": False,
        "selected": "s",
    }
    assert bench._round_problems(good, expected, 3) == []
    assert bench._round_problems({**good, "returned": 4, "complete": True}, expected, 5) == []
    for field, wrong in (
        ("count", 9),
        ("scanned", 11),
        ("certificate", "d"),
        ("missing_total", 5),
        ("extras", 1),
        ("returned", 2),
        ("complete", True),
        ("selected", "t"),
    ):
        assert bench._round_problems({**good, field: wrong}, expected, 3) == [field]


def test_expected_round_holds_the_new_mail_committed_before_the_snapshot(bench, baseline, tmp_path):
    # A batch of new mail on a copy of the baseline: the expected set at
    # the corpus's rows is the corpus, and at the rows after the batch
    # it holds the new messages outside Trash and their occurrences.
    corpus, base, _built, _report = baseline
    copy = tmp_path / "copy.db"
    with closing(sqlite3.connect(base)) as src, closing(sqlite3.connect(copy)) as dst:
        src.backup(dst)
    stop = tmp_path / "stop"
    timer = _stop_after_batch(copy, stop, 60 + 4)
    shape = {"identity": "typical", "records": "mixed", "per_message": 2}
    out = bench.phase_writer(str(copy), str(stop), 4, 3600.0, shape=shape)
    timer.join()
    assert out["visible"][0][1:] == (60, 120)
    assert out["visible"][-1][1:] == (64, 128)
    shown = sum(bench._folder(i) != "Trash" for i in range(60, 64))
    for kind, new in (("messages", shown), ("occurrences", 2 * shown)):
        request = tmp_path / f"request-{kind}.json"
        bench.write_request(str(corpus), kind, 5, 3, request)
        before = bench.expected_round(str(copy), kind, str(request), (60, 120))
        after = bench.expected_round(str(copy), kind, str(request), (64, 128))
        assert before["missing_total"] == 5 and before["extras"] == 3
        assert after["count"] == before["count"] + new
        assert after["missing_total"] == 5 + new and after["extras"] == 3
        assert (
            before["certificate"] == bench.phase_certificate(str(corpus), kind, "stream")["digest"]
        )


@pytest.mark.parametrize("worst_first", [False, True])
def test_a_round_that_returns_other_records_fails(bench, tmp_path, monkeypatch, worst_first):
    # The same number of records, but not the ones the round must pick
    # (the lowest missing identities, worst-case ones first when asked):
    # the selection's digest differs, and the measurement fails.
    db = tmp_path / "pick.db"
    bench.build(db, 300, 1, "typical", "mixed")
    request = tmp_path / "request.json"
    bench.write_request(str(db), "messages", 6, 0, request, missing_from="worst")
    real = bench._materialize

    def other(conn, kind, missing):
        # The highest missing identities instead of the chosen ones.
        rows = conn.execute(
            "SELECT claimant_id FROM messages ORDER BY claimant_id DESC LIMIT ?", (len(missing),)
        )
        return real(conn, kind, [r[0] for r in rows])

    out = bench.phase_reconcile(str(db), "messages", 3, str(request), worst_first=worst_first)
    expected = bench.expected_round(
        str(db), "messages", str(request), (10**9, 10**9), k=3, worst_first=worst_first
    )
    assert bench._round_problems(out, expected, 3) == []
    monkeypatch.setattr(bench, "_materialize", other)
    out = bench.phase_reconcile(str(db), "messages", 3, str(request), worst_first=worst_first)
    assert bench._round_problems(out, expected, 3) == ["selected"]


def test_a_round_whose_result_is_not_its_snapshot_fails(bench, tmp_path, monkeypatch):
    # The round's count, certificate, diff and records are checked
    # against the snapshot after the writer stops: one that differs
    # fails the measurement, naming the fields.
    db = tmp_path / "check.db"
    bench.build(db, 20, 1, "typical", "typical")
    request = tmp_path / "request.json"
    bench.write_request(str(db), "messages", 2, 1, request)
    real = bench._child

    def child(phase, **kwargs):
        out = real(phase, **kwargs)
        return {**out, "count": out["count"] + 1} if phase == "reconcile" else out

    monkeypatch.setattr(bench, "_child", child)
    with pytest.raises(RuntimeError, match="not its snapshot's: count"):
        bench.run_wal(str(db), "messages", 5, str(request), 1, 0.0)


def test_writer_tokenizer_is_the_indexers_pinned_version():
    # The writer cuts thread bodies with the indexer's tokenizer; the
    # ``tokenizers`` this environment resolves (through cohere) must be
    # the version the indexer pins, or the cut could differ.
    import importlib.metadata
    import tomllib

    pyproject = Path(__file__).resolve().parents[2] / "indexer" / "pyproject.toml"
    deps = tomllib.loads(pyproject.read_text())["project"]["dependencies"]
    pins = [d.split("==", 1)[1] for d in deps if d.startswith("tokenizers==")]
    assert pins == [importlib.metadata.version("tokenizers")]


def test_wal_is_refused_without_the_tokenizer_asset(bench, tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "TOKENIZER_PATHS", (tmp_path / "missing.json",))
    with pytest.raises(SystemExit, match="tokenizer"):
        bench.parse_args(_argv({"--wal": None, "--workdir": str(tmp_path)}))
    assert not list(tmp_path.iterdir())
    bench.parse_args(_argv({"--workdir": str(tmp_path)}))  # without --wal: not needed


def test_each_wal_configuration_starts_from_the_built_index(bench, tmp_path):
    # Deleting a writer's rows would restore row counts, not the file
    # (freelist, B-tree splits, FTS segments, vector chunks), so each
    # configuration runs on a fresh copy of the index build left, and
    # the index itself is never written.
    import hashlib

    db = tmp_path / "base.db"
    bench.build(db, 8, 1, "typical", "typical")
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"hashes": ""}))
    built = hashlib.sha256(db.read_bytes()).hexdigest()
    copy = Path(str(db) + bench.WAL_COPY_SUFFIX)
    shape = {"per_message": 1}
    for _ in range(2):
        out = bench.run_wal(str(db), "messages", 5, str(request), 1, 0.0, shape=shape)
        assert out["writer_commits_total"] > 0
        with closing(sqlite3.connect(copy)) as conn:
            # The writer numbers on from the corpus each time: no earlier
            # writer's message is in its copy.
            first = conn.execute(
                "SELECT message_id FROM messages WHERE rowid > 8 ORDER BY rowid LIMIT 1"
            ).fetchone()[0]
        assert first == bench.message_id(8, "typical")
    assert hashlib.sha256(db.read_bytes()).hexdigest() == built


def test_round_transaction_time_starts_at_the_snapshot(bench):
    import inspect

    src = inspect.getsource(bench.phase_reconcile)
    snapshot = src.index('"SELECT 1 FROM messages LIMIT 1"')
    assert snapshot < src.index("t_snapshot = time.perf_counter()") < src.index("count_sql, params")
    assert '"transaction_s": t_records - t_snapshot' in src


def test_certificate_count_time_excludes_the_connection_setup(bench):
    import inspect

    src = inspect.getsource(bench.phase_certificate)
    assert src.index("t_open = time.perf_counter()") < src.index("count_sql, params")
    assert '"count_s": t_count - t_open' in src
    assert '"total_s": t_end - t0' in src


def test_round_measures_only_the_reconcile_algorithm(bench):
    import inspect

    src = inspect.getsource(bench.phase_reconcile)
    t0 = src.index("t0 = time.perf_counter()")
    # The benchmark-only choice of worst-case records is made before
    # the timed region, not inside the round.
    assert src.index("_worst_targets(") < t0
    assert src.count("_worst_targets(") == 1
    assert "bench_worst" in inspect.getsource(bench._worst_targets)
    # The diff keeps no set of the server's hashes: each scanned hash is
    # discarded from the upload, so memory is bounded by the upload.
    assert "server.add(" not in src
    assert "client.discard(h)" in src
    # RSS is sampled before the per-record sizes are serialized again.
    assert src.index("rss = _rss_kib()") < src.index('"record_bytes_max"')


def test_round_parse_time_excludes_the_request_file_read(bench):
    import inspect

    src = inspect.getsource(bench.phase_reconcile)
    assert src.index("read_bytes()") < src.index("t0 = time.perf_counter()")


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"--records": "cardinality", "--references": "1000", "--messages": "3", "--missing": "0"},
        {"--records": "worst", "--per-message": "5", "--chunks": "3", "--chunk-tokens": "250"},
        {"--identity": "ascii998", "--records": "mixed", "--missing": "3"},
    ],
)
def test_file_floor_is_below_every_file(bench, changes):
    args = bench.parse_args(_argv(changes))
    assert 0 < bench._file_floor(args) <= bench._largest_file(args)


def test_oversized_references_are_refused_without_rendering(bench, tmp_path, monkeypatch):
    # 2,500,000 entries render a References header of about 54 MB, over
    # the file limit: the floor must count the header's exact bytes so
    # the refusal comes before any message is rendered.
    rendered = []
    monkeypatch.setattr(bench, "render_eml", lambda *a, **k: rendered.append(a) or "")
    argv = {
        "--records": "cardinality",
        "--references": "2500000",
        "--messages": "3",
        "--missing": "0",
        "--workdir": str(tmp_path),
    }
    with pytest.raises(SystemExit, match="file limit"):
        bench.parse_args(_argv(argv))
    assert rendered == []


def test_an_oversized_body_is_refused_after_one_probe(bench, tmp_path, monkeypatch):
    # Every message of this corpus is over the file limit: validation
    # stops at the first oversized probe rather than rendering all of
    # the SHAPE_PERIOD probes.
    real = bench.render_eml
    rendered = []
    monkeypatch.setattr(bench, "render_eml", lambda *a: rendered.append(a[0]) or real(*a))
    argv = {
        "--messages": "50000",
        "--per-message": "0",
        "--missing": "0",
        "--chunks": "41666",
        "--chunk-tokens": "200",
        "--workdir": str(tmp_path),
    }
    with pytest.raises(SystemExit, match="file limit"):
        bench.parse_args(_argv(argv))
    assert len(rendered) == 1


@pytest.mark.parametrize("references", [1, 9, 10, 11, 99, 100, 12_345])
def test_file_floor_counts_the_references_header_exactly(bench, references):
    header = "References: " + "\n ".join(f"<r{n}@x.example>" for n in range(references))
    assert bench._references_bytes(references) == len(header) - len("References: ")


def test_writer_commits_and_stamps_only_under_the_order_lock(bench, tmp_path):
    # The round takes its snapshot and reads its clock under the same
    # lock the writer holds across COMMIT and its stamp, so no commit
    # can fall between a snapshot and the time that marks it.
    import fcntl
    import threading

    db = tmp_path / "order.db"
    bench.build(db, 8, 0, "typical", "typical")
    stop = tmp_path / "stop"
    order = tmp_path / "order.lock"
    out: dict = {}
    with open(order, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        writer = threading.Thread(
            target=lambda: out.update(
                bench.phase_writer(str(db), str(stop), 1, 0.0, order=str(order))
            )
        )
        writer.start()
        ready = Path(str(stop) + ".ready")
        deadline = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.3)
        released = time.monotonic()
        fcntl.flock(held, fcntl.LOCK_UN)
    time.sleep(0.2)
    stop.touch()
    writer.join()
    measured = out["commit_times"][1:]
    assert out["commits"] > 0
    assert all(when > released for when, _ in measured)


def test_the_round_takes_its_snapshot_under_the_order_lock(bench):
    import inspect

    src = inspect.getsource(bench.phase_reconcile)
    lock = src.index("with _ordered(order):")
    assert lock < src.index('"SELECT 1 FROM messages LIMIT 1"') < src.index("started_at = ")
    end = src.index("with _ordered(order):", lock + 1)
    assert end < src.index("ended_at = ")


def test_writer_wal_size_is_its_logical_end(bench, tmp_path):
    # An automatic checkpoint would restart the WAL and refill its
    # allocated file without growing it, hiding retained frames. With
    # it off, every commit grows the file, so its size is the WAL's end.

    db = tmp_path / "grow.db"
    bench.build(db, 8, 0, "typical", "typical")
    stop = tmp_path / "stop"
    # One whole batch (a lone message's ten commits), however busy the
    # host: a fixed delay could see too few commits on a loaded runner.
    timer = _stop_after_batch(db, stop, 9)
    out = bench.phase_writer(str(db), str(stop), 1, 3600.0)
    timer.join()
    sizes = [size for _, size in out["commit_times"]]
    assert len(sizes) == 1 + 10
    assert all(b > a for a, b in zip(sizes, sizes[1:]))


def test_writer_survives_a_tiny_interval(bench, tmp_path, monkeypatch):
    # The pause is computed from one clock read: a clock that passes
    # ``resume`` between reads never yields a negative sleep.

    db = tmp_path / "tiny.db"
    bench.build(db, 8, 0, "typical", "typical")
    stop = tmp_path / "stop"
    real = time.sleep

    def checked(seconds: float) -> None:
        assert seconds >= 0
        real(seconds)

    monkeypatch.setattr(bench.time, "sleep", checked)
    timer = _stop_after_ready(stop, 0.3)
    out = bench.phase_writer(str(db), str(stop), 1, 0.000001)
    timer.join()
    assert out["commits"] > 0


def test_the_round_closes_its_transaction_inside_the_end_boundary(bench):
    import inspect

    src = inspect.getsource(bench.phase_reconcile)
    end = src.index("with _ordered(order):", src.index("with _ordered(order):") + 1)
    block = src[end:].split("\n")
    body = []
    for line in block[1:]:
        if line.strip() and not line.startswith(" " * 12):
            break
        body.append(line.strip())
    assert "ended_at = time.monotonic()" in body
    assert "conn.rollback()" in body


def test_an_unbounded_wal_hold_is_refused_before_building(bench, tmp_path):
    with pytest.raises(SystemExit, match="wal-hold"):
        bench.parse_args([*_argv({"--wal": None, "--workdir": str(tmp_path)}), "--wal-hold=1e308"])
    assert not list(tmp_path.iterdir())


def test_writer_delay_ends_when_stop_appears(bench, tmp_path):
    db = tmp_path / "delay.db"
    bench.build(db, 8, 0, "typical", "typical")
    stop = tmp_path / "stop"

    timer = _stop_after_batch(db, stop, 9)
    started = time.monotonic()
    out = bench.phase_writer(str(db), str(stop), 1, 3600.0)
    timer.join()
    assert time.monotonic() - started < 5
    # One batch of one message: its ten commits.
    assert out["commits"] == 10


def test_writer_waits_only_the_remaining_interval(bench, tmp_path, monkeypatch):
    db = tmp_path / "interval.db"
    bench.build(db, 8, 0, "typical", "typical")
    stop = tmp_path / "stop"
    slept: list[float] = []
    # A clock that only sleeping advances, so a loaded host cannot
    # stretch one sleep past the whole interval.
    now = [time.monotonic()]

    def sleep(seconds):
        slept.append(seconds)
        now[0] += max(seconds, 0)
        if len(slept) >= 6:
            stop.touch()

    monkeypatch.setattr(bench.time, "sleep", sleep)
    monkeypatch.setattr(bench.time, "monotonic", lambda: now[0])
    bench.phase_writer(str(db), str(stop), 1, 0.06)
    # Each 0.06 s pause: 0.05, then only the 0.01 s left.
    assert len(slept) == 6
    assert slept[0::2] == [0.05] * 3
    assert all(0 < s < 0.0101 for s in slept[1::2])


def test_a_writer_that_dies_mid_round_fails_the_measurement(bench, tmp_path):
    # A trigger refuses the writer's first enqueue, so the writer exits
    # after its setup; the round itself succeeds.
    db = tmp_path / "dies.db"
    bench.build(db, 20, 0, "typical", "typical")
    with closing(sqlite3.connect(db)) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.executescript(bench._WRITER_SCHEMA)
        conn.execute(
            "CREATE TRIGGER refuse BEFORE INSERT ON indexing_jobs "
            "BEGIN SELECT RAISE(ABORT, 'refused'); END"
        )
        conn.commit()
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"hashes": ""}))
    with pytest.raises(RuntimeError, match="WAL writer exited with status 1"):
        bench.run_wal(str(db), "messages", 5, str(request), 1, 0.0)
    assert not (tmp_path / ("dies.db" + bench.WAL_COPY_SUFFIX + ".stop")).exists()


def test_wal_results_name_the_round_they_measured(bench, report):
    # Kind, scan method and K: the workload each WAL figure belongs to.
    assert all(w["k"] == max(report["config"]["k"]) for w in report["wal"])


def _stop_after_batch(db: Path, stop: Path, messages: int):
    """A started thread that creates ``stop`` once the writer's first
    batch is complete (``messages`` rows in all, no job left): the
    writer stops before any commit once ``stop`` exists, so a test that
    counts one batch waits for it, however slow the disk."""
    import threading

    def run() -> None:
        ready = Path(str(stop) + ".ready")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if ready.exists():
                with closing(sqlite3.connect(db, timeout=30)) as conn:
                    rows = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                    jobs = conn.execute("SELECT COUNT(*) FROM indexing_jobs").fetchone()[0]
                if rows == messages and jobs == 0:
                    break
            time.sleep(0.01)
        stop.touch()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def _stop_after_ready(stop: Path, delay: float):
    """A started thread that creates ``stop`` ``delay`` seconds after
    the writer is ready (``stop.ready``), so the delay counts commits,
    not the writer's setup, however slow the disk."""
    import threading

    def run() -> None:
        ready = Path(str(stop) + ".ready")
        deadline = time.monotonic() + 120
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(delay)
        stop.touch()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def test_writer_transactions_run_wholly_under_the_order_lock(bench):
    # A large commit spills dirty pages into the WAL before COMMIT, so
    # BEGIN as well as COMMIT and its stamp must be under the lock.
    import inspect

    src = inspect.getsource(bench.phase_writer)
    loop = src[src.index("def transaction(") :]
    # The only BEGIN and COMMIT after the writer is ready are these: every
    # measured commit runs through ``transaction()``.
    assert src.count('"BEGIN IMMEDIATE"') == src.count('"COMMIT"') == 1
    lock = loop.index("with _ordered(order):")
    assert lock < loop.index('"BEGIN IMMEDIATE"') < loop.index("yield")
    assert loop.index("yield") < loop.index('"COMMIT"') < loop.index("times.append")
    for helper in (bench._writer_phase1, bench._writer_phase2c, bench._write_thread_vector):
        helper_src = inspect.getsource(helper)
        assert "COMMIT" not in helper_src and "BEGIN" not in helper_src
