"""Owner-approved direction truth table over synthetic claimed headers (#824)."""

import hashlib
import sqlite3
from contextlib import closing

import pytest
from src.lib.predicates import InvalidFilterError, Where, normalize_where
from src.lib.sqlite import Database

from tests.conftest import _insert_message
from tests.test_evidence_scope import _finish_threads
from tests.test_sqlite import _open_built_db_conn


def identity(conn, addresses):
    conn.execute("DELETE FROM operator_addresses")
    conn.executemany("INSERT INTO operator_addresses VALUES (?)", ((v,) for v in addresses))
    digest = hashlib.sha256("".join(v + "\n" for v in sorted(addresses)).encode()).hexdigest()
    conn.execute(
        "UPDATE operator_identity SET state=?, address_count=?, address_digest=?",
        ("configured" if addresses else "unconfigured", len(addresses), digest),
    )
    conn.commit()


@pytest.fixture
def direction_db(tmp_path):
    conn, path = _open_built_db_conn(tmp_path, "direction.db")
    cases = [
        ("in", ["external@example.test"], ["me@self.test"], [], {}),
        ("out-to", ["me@self.test"], ["external@example.test"], [], {}),
        ("out-cc", ["me@self.test"], [], ["external@example.test"], {}),
        ("own-only", ["me@self.test"], ["alias@self.test"], [], {}),
        ("own-empty", ["me@self.test"], [], [], {}),
        ("ambiguous", ["external@example.test"], [], [], {"sender_ambiguous": 1}),
        ("unchecked", ["external@example.test"], [], [], {"sender_ambiguous": None}),
        (
            "partial-from",
            ["external@example.test"],
            [],
            [],
            {"completeness": {"from_addresses_complete": 0}},
        ),
        ("multiple-from", ["me@self.test", "external@example.test"], [], [], {}),
        ("no-from", [], [], [], {}),
        (
            "out-partial",
            ["me@self.test"],
            ["external@example.test"],
            [],
            {"completeness": {"to_addresses_complete": 0}},
        ),
    ]
    for mid, authors, to, cc, kwargs in cases:
        _insert_message(
            conn,
            message_id=mid,
            thread_id=mid,
            sent_at="2024-01-01T00:00:00+00:00",
            from_=authors,
            to=to,
            cc=cc,
            **kwargs,
        )
    _finish_threads(conn)
    identity(conn, ["me@self.test", "alias@self.test"])
    conn.close()
    return Database(str(path))


def test_direction_projection_and_partial_leaf_knowledge(direction_db):
    rows = {m.message_id: m.direction for m in direction_db.query_messages(limit=100).messages}
    assert rows["in"] == "inbound"
    assert all(rows[k] == "outbound" for k in ("out-to", "out-cc", "out-partial"))
    assert all(
        v == "unknown"
        for k, v in rows.items()
        if k not in ("in", "out-to", "out-cc", "out-partial")
    )
    inbound = direction_db.query_messages(direction="inbound", limit=100)
    assert [m.message_id for m in inbound.messages] == ["in"]
    assert inbound.indeterminate == 5
    own_not_inbound = normalize_where(
        Where.model_validate(
            {"all": [{"leaf": "direction_is", "value": "inbound", "negate": True}]}
        )
    )
    negated = direction_db.query_messages(where=own_not_inbound, limit=100)
    assert {m.message_id for m in negated.messages} == {
        "out-to",
        "out-cc",
        "out-partial",
        "own-only",
        "own-empty",
    }
    assert direction_db.query_messages(direction="self").total_matches == 0
    assert direction_db.query_messages(direction="self").indeterminate == 7


def test_unconfigured_identity_and_direction_groups(direction_db):
    page = direction_db.aggregate_messages(group_by="direction")
    assert {g.value: g.messages for g in page.groups} == {"inbound": 1, "outbound": 3, None: 7}
    with closing(sqlite3.connect(direction_db.path)) as conn, conn:
        identity(conn, [])
    assert all(m.direction == "unknown" for m in direction_db.query_messages(limit=100).messages)
    page = direction_db.query_messages(direction="inbound")
    assert page.total_matches == 0 and page.indeterminate == 11


def test_every_message_cursor_binds_identity_inside_read_snapshot(direction_db):
    cursor = direction_db.query_messages(limit=1).next_cursor
    assert cursor
    with closing(sqlite3.connect(direction_db.path)) as conn, conn:
        identity(conn, ["external@example.test"])
    with pytest.raises(InvalidFilterError, match="cursor"):
        direction_db.query_messages(limit=1, cursor=cursor)


@pytest.mark.parametrize("operand", ["inbound", "outbound", "self"])
@pytest.mark.parametrize("negate", [False, True])
def test_population_tools_share_truth_and_negation(direction_db, operand, negate):
    where = normalize_where(
        Where.model_validate(
            {"all": [{"leaf": "direction_is", "value": operand, "negate": negate}]}
        )
    )
    messages = direction_db.query_messages(where=where, limit=100)
    threads = direction_db.query_threads(where=where, limit=100)
    groups = direction_db.aggregate_messages(group_by="direction", where=where, sample_per_group=2)
    assert threads.total_matching_messages == groups.total_matches == messages.total_matches
    assert threads.indeterminate_messages == groups.indeterminate == messages.indeterminate
    assert sum(g.messages for g in groups.groups) == messages.total_matches
    assert {t.thread_id for t in threads.threads} == {m.thread_id for m in messages.messages}


@pytest.mark.parametrize("tool", ["messages", "threads", "aggregate"])
def test_stale_identity_cursor_rejected_before_population_scan(direction_db, monkeypatch, tool):
    def read(cursor=None):
        if tool == "messages":
            return direction_db.query_messages(limit=1, cursor=cursor)
        if tool == "threads":
            return direction_db.query_threads(direction="outbound", limit=1, cursor=cursor)
        return direction_db.aggregate_messages(group_by="direction", limit=1, cursor=cursor)

    cursor = read().next_cursor
    assert cursor
    with closing(sqlite3.connect(direction_db.path)) as conn, conn:
        identity(conn, ["external@example.test"])
    seen = []
    connect = direction_db._connect

    def traced():
        conn = connect()
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(direction_db, "_connect", traced)
    with pytest.raises(InvalidFilterError, match="cursor"):
        read(cursor)
    assert not any("FROM messages" in sql for sql in seen)


def test_direction_free_population_cursors_survive_identity_replacement(direction_db):
    thread_cursor = direction_db.query_threads(limit=1).next_cursor
    group_cursor = direction_db.aggregate_messages(group_by="sender_address", limit=1).next_cursor
    with closing(sqlite3.connect(direction_db.path)) as conn, conn:
        identity(conn, ["external@example.test"])
    assert direction_db.query_threads(limit=1, cursor=thread_cursor).offset == 1
    assert (
        direction_db.aggregate_messages(
            group_by="sender_address", limit=1, cursor=group_cursor
        ).offset
        == 1
    )


def test_direction_status_is_count_only_and_conservative(direction_db):
    import json

    status = direction_db.get_mailbox_status()
    coverage = status["query_capabilities"]["direction_is"]
    assert coverage["available_messages"] == 4
    assert coverage["unassessed_messages"] == 1
    assert coverage["missing_messages"] == 6
    assert status["operator_identity"] == {
        "state": "configured",
        "address_count": 2,
        "sent_conflicts": 0,
    }
    assert "@" not in json.dumps(status["operator_identity"])
    with closing(sqlite3.connect(direction_db.path)) as conn, conn:
        identity(conn, [])
    coverage = direction_db.get_mailbox_status()["query_capabilities"]["direction_is"]
    assert coverage["status"] == "unavailable"
    assert coverage["available_messages"] is None
    assert "config/identity.toml" in coverage["condition"]


def test_query_only_output_and_projection(direction_db, fake_server):
    import asyncio

    from src.tools.retrieval import register_retrieval_tools

    register_retrieval_tools(fake_server, direction_db)
    for name in ("query_messages", "query_threads", "aggregate_messages"):
        kwargs = {"direction": "outbound"}
        if name == "aggregate_messages":
            kwargs["group_by"] = "direction"
        if name == "query_messages":
            kwargs["fields"] = ["direction"]
        output = asyncio.run(fake_server.tools[name](**kwargs))
        assert "outbound" in output.content[0].text
        assert output.structured_content
        if name == "query_messages":
            assert all(
                set(m) == {"direction", "claimant_id", "thread_id"}
                for m in output.structured_content["messages"]
            )
    from src.tools.outputs import ListedMessage

    assert "direction" not in ListedMessage.model_fields


def test_invalid_direction_operands_rejected(direction_db):
    for value in ("unknown", "OUTBOUND", "", "SYNTHETIC_PRIVATE_MARKER"):
        with pytest.raises(InvalidFilterError):
            direction_db.query_messages(direction=value)


@pytest.mark.parametrize("role", ["from", "to", "cc", "bcc"])
def test_direction_reader_work_tracks_only_relevant_participant_fanout(tmp_path, monkeypatch, role):
    """Measure real readers, every connection, and separate From/To/Cc/Bcc work."""
    import time

    from src.lib.predicates import DIRECTION_STATE_SQL

    from tests.conftest import claimant_of

    measurements = []
    for count in (20, 800):
        conn, path = _open_built_db_conn(tmp_path, f"work-{role}-{count}.db")
        _insert_message(
            conn,
            message_id="fanout",
            thread_id="fanout",
            sent_at=None,
            folder="Sent",
            from_=["own@self.test"],
            body=" ".join(f"syntheticword{i}" for i in range(30)),
        )
        addresses = [f"a{i:05}@self.test" for i in range(count)]
        conn.executemany(
            "INSERT INTO message_participants VALUES (?, ?, ?, NULL)",
            [(claimant_of("fanout"), role, a) for a in addresses],
        )
        identity(conn, ["own@self.test", *addresses])
        conn.close()
        db = Database(str(path))
        calls = [
            lambda: db.query_messages(direction="inbound", limit=1),
            lambda: db.query_threads(direction="inbound", limit=1),
            lambda: db.aggregate_messages(group_by="direction", direction="inbound", limit=1),
            db.get_mailbox_status,
        ]
        started = time.perf_counter()
        for call in calls:
            call()
        assert time.perf_counter() - started < 15
        work = {"connections": 0, "steps": 0}
        connect = sqlite3.connect

        def progress():
            work["steps"] += 1
            return 0

        def counted(*args, **kwargs):
            c = connect(*args, **kwargs)
            c.set_progress_handler(progress, 1)
            work["connections"] += 1
            return c

        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", counted)
            for call in calls:
                call()
        assert work["connections"] == 4

        # Direct cardinality probe also demonstrates the gate rejects a
        # full COUNT: the old/unbounded shape must grow with From fanout.
        def steps(sql):
            value = 0

            def tick():
                nonlocal value
                value += 1
                return 0

            with closing(connect(path)) as c, c:
                c.set_progress_handler(tick, 1)
                c.execute("SELECT " + sql + " FROM messages m").fetchall()
            return value

        direct = steps(DIRECTION_STATE_SQL)
        bad = steps(DIRECTION_STATE_SQL.replace(" LIMIT 2", ""))
        measurements.append((work["steps"], direct, bad))
    small, large = measurements
    if role == "from":
        assert large[0] < small[0] * 2, measurements
        assert large[1] == small[1], measurements
        assert large[2] > small[2] * 2, measurements
    else:
        # To/Cc inventories may be large; one indexed pass, never a
        # From x recipient cross product or a scan of unrelated messages.
        assert large[0] < small[0] * 80, measurements
        assert large[1] < small[1] * 80, measurements
