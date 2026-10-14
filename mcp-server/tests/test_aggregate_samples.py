"""Synthetic chronological group evidence (#1169), never a series classifier."""

import sqlite3

import pytest
from src.lib.predicates import InvalidFilterError
from src.lib.sqlite import AGGREGATE_DIMENSIONS, Database

from tests import test_aggregate_messages as aggregate_tests
from tests.conftest import _insert_message, claimant_of
from tests.test_evidence_scope import _finish_threads
from tests.test_sqlite import _open_built_db_conn


@pytest.fixture
def agg_db(tmp_path):
    return aggregate_tests.agg_db.__wrapped__(tmp_path)


@pytest.fixture
def sample_db(tmp_path):
    conn, path = _open_built_db_conn(tmp_path, "samples.db")
    for group, count in (("series", 11), ("once", 2)):
        for i in range(count):
            _insert_message(
                conn,
                message_id=f"{group}{i:02}",
                thread_id=f"{group}{i:02}",
                sent_at=f"2024-{i + 1:02}-01T12:00:00+00:00",
                from_=[f"{group}@example.test"],
                subject="recurring synthetic bulletin" if group == "series" else f"one-off {i}",
            )
    _finish_threads(conn)
    conn.close()
    return Database(str(path))


def test_uneven_groups_have_repeatable_chronological_evidence(sample_db):
    page = sample_db.aggregate_messages(group_by="sender_address", sample_per_group=4)
    assert page == sample_db.aggregate_messages(group_by="sender_address", sample_per_group=4)
    series, once = page.groups
    assert [s.claimant_id for s in series.samples] == [
        claimant_of(f"series{i:02}") for i in (0, 3, 6, 10)
    ]
    assert len(once.samples) == once.messages == 2
    assert {s.subject for s in series.samples} == {"recurring synthetic bulletin"}
    assert {s.subject for s in once.samples} == {"one-off 0", "one-off 1"}
    assert page.samples_returned == 6
    assert sample_db.aggregate_messages(group_by="sender_address").samples_returned == 0


def test_one_sample_uses_lower_median_and_paging_is_interchangeable(sample_db):
    page = sample_db.aggregate_messages(group_by="sender_address", sample_per_group=1, limit=1)
    assert page.groups[0].samples[0].claimant_id == claimant_of("series05")
    counts = sample_db.aggregate_messages(group_by="sender_address", limit=1)
    assert page.next_cursor == counts.next_cursor
    nxt = sample_db.aggregate_messages(
        group_by="sender_address", sample_per_group=1, limit=1, cursor=counts.next_cursor
    )
    assert nxt.groups[0].samples[0].claimant_id == claimant_of("once00")


@pytest.mark.parametrize("dimension", AGGREGATE_DIMENSIONS)
def test_samples_use_definite_memberships_and_preserve_counts(agg_db, dimension):
    counts = agg_db.aggregate_messages(group_by=dimension, subject="budget")
    sampled = agg_db.aggregate_messages(group_by=dimension, subject="budget", sample_per_group=3)
    assert (sampled.total_matches, sampled.indeterminate) == (
        counts.total_matches,
        counts.indeterminate,
    )
    for group, original in zip(sampled.groups, counts.groups, strict=True):
        assert (group.value, group.messages, group.indeterminate) == (
            original.value,
            original.messages,
            original.indeterminate,
        )
        assert len(group.samples) == min(3, group.messages)
        assert len({s.claimant_id for s in group.samples}) == len(group.samples)
        assert all(s.subject.startswith("budget") for s in group.samples)
        if group.value is not None:
            filters, where = aggregate_tests._group_filters(
                dimension, group.value, {"subject": "budget"}, []
            )
            members, _ = aggregate_tests._paged_messages(agg_db, filters, where)
            ordered = sorted(members, key=lambda m: (m.effective_at, m.claimant_id))
            count = min(3, len(ordered))
            positions = (
                [(len(ordered) - 1) // 2]
                if count == 1
                else [i * (len(ordered) - 1) // (count - 1) for i in range(count)]
            )
            assert [s.claimant_id for s in group.samples] == [
                ordered[i].claimant_id for i in positions
            ]


@pytest.mark.parametrize("value", [True, False, -1, 11, 1.5, "2", None])
def test_invalid_sample_count_is_rejected(sample_db, value):
    with pytest.raises(InvalidFilterError, match="sample_per_group"):
        sample_db.aggregate_messages(group_by="folder", sample_per_group=value)


@pytest.fixture
def cap_db(tmp_path):
    conn, path = _open_built_db_conn(tmp_path, "cap.db")
    for g in range(11):
        for i in range(10):
            _insert_message(
                conn,
                message_id=f"{g}-{i}",
                thread_id=f"{g}-{i}",
                sent_at="2024-01-01T12:00:00+00:00",
                from_=[f"g{g:02}@example.test"],
            )
    _finish_threads(conn)
    conn.close()
    return Database(str(path))


def test_actual_page_cap_rejects_without_starving_groups(cap_db):
    db = cap_db
    with pytest.raises(InvalidFilterError, match=r"limit.*10"):
        db.aggregate_messages(group_by="sender_address", sample_per_group=10, limit=100)
    page = db.aggregate_messages(group_by="sender_address", sample_per_group=10, limit=10)
    assert page.samples_returned == 100 and page.has_more
    assert all(len(g.samples) == 10 for g in page.groups)
    # Oversized requested product is accepted when the actual page is small.
    last = db.aggregate_messages(
        group_by="sender_address", sample_per_group=10, limit=100, cursor=page.next_cursor
    )
    assert last.samples_returned == 10


def test_subject_projection_is_bounded_and_completeness_is_separate(sample_db):
    with sqlite3.connect(sample_db.path) as conn:
        conn.execute("UPDATE messages SET subject = ?, subject_complete = NULL", ("x" * 1_000_000,))
    row = sample_db.aggregate_messages(group_by="folder", sample_per_group=1).groups[0].samples[0]
    assert row.subject == "x" * 500 and row.subject_clipped
    assert row.subject_complete is None


def test_dates_ties_and_period_cells(sample_db):
    with sqlite3.connect(sample_db.path) as conn:
        conn.execute(
            "UPDATE messages SET sent_at = NULL, sent_at_status = 'missing', occurred_at = NULL WHERE message_id = 'series10'"
        )
        conn.execute(
            "UPDATE messages SET sent_at = '2024-01-01T12:00:00+00:00' WHERE message_id = 'series01'"
        )
    effective = sample_db.aggregate_messages(group_by="sender_address", sample_per_group=2)
    assert effective.groups[0].samples[-1].at is None
    sent = sample_db.aggregate_messages(
        group_by="sender_address", sample_per_group=2, date_basis="sent"
    )
    assert sent.indeterminate == 1
    assert all(s.at is not None for g in sent.groups for s in g.samples)
    cells = sample_db.aggregate_messages(
        group_by="sender_address", period_breakdown=True, sample_per_group=10
    )
    assert sum(len(g.samples) for g in cells.groups) == sum(g.messages for g in cells.groups)
    tied = next(
        g for g in cells.groups if g.value == "series@example.test" and g.period == "2024-01"
    )
    assert [s.claimant_id for s in tied.samples] == sorted(
        [claimant_of("series00"), claimant_of("series01")]
    )
    assert any(g.period is None and g.samples[0].at is None for g in cells.groups)


def test_full_mime_keys_and_overlapping_memberships(tmp_path):
    from tests.test_aggregate_dimensions import build

    prefix = "application/" + "x" * 2100
    db = build(tmp_path, types=[prefix + "a", prefix + "b", prefix + "a"])
    page = db.aggregate_messages(group_by="attachment_content_type", sample_per_group=2)
    assert len(page.groups) == page.samples_returned == 2
    assert page.groups[0].value == page.groups[1].value
    assert all(g.value_clipped and len(g.samples) == g.messages == 1 for g in page.groups)
    assert page.groups[0].samples == page.groups[1].samples


def test_tool_samples_and_private_metadata_stay_out_of_logs(sample_db, caplog):
    from tests.test_aggregate_messages import _server
    from tests.test_structured_output import _call

    marker = "SYNTHETIC_PRIVATE_SAMPLE_SUBJECT"
    with sqlite3.connect(sample_db.path) as conn:
        conn.execute("UPDATE messages SET subject = ?", (marker,))
    with caplog.at_level("INFO"):
        output = _call(
            _server(sample_db), "aggregate_messages", group_by="sender_address", sample_per_group=2
        )
    assert output["samples_returned"] == 4
    assert all(g["samples_returned"] == len(g["samples"]) == 2 for g in output["groups"])
    assert output["groups"][0]["samples"][0]["subject"] == marker
    assert marker not in caplog.text
    assert "samples_returned" in caplog.text


def test_sample_subject_clipping_is_visible_without_content(sample_db, caplog):
    from tests.test_aggregate_messages import _server
    from tests.test_structured_output import _call

    marker = "SYNTHETIC_CLIPPED_SUBJECT"
    with sqlite3.connect(sample_db.path) as conn:
        conn.execute("UPDATE messages SET subject = ?", (marker * 1000,))
    with caplog.at_level("INFO"):
        result = _call(
            _server(sample_db), "aggregate_messages", group_by="folder", sample_per_group=1
        )
    assert result["groups"][0]["samples"][0]["subject_clipped"]
    assert "'sample_subjects_clipped': 1" in caplog.text
    assert marker not in caplog.text


@pytest.mark.parametrize("sample_per_group", [0, 3])
def test_sampling_does_not_reevaluate_predicates(tmp_path, monkeypatch, sample_per_group):
    shapes = []
    for size in (40, 400):
        with monkeypatch.context() as patch:
            calls, page, connections, statements = aggregate_tests._probe(
                aggregate_tests._work_corpus(tmp_path, size),
                patch,
                "sender_domain",
                [{"leaf": "body_words", "value": "budget"}],
                sample_per_group=sample_per_group,
            )
        assert calls["body_words"] <= 2 * size
        assert page.samples_returned == (6 if sample_per_group else 0)
        shapes.append((connections, statements))
    assert shapes[0] == shapes[1]
    assert shapes[0][0] == shapes[0][1][0] == 1


def sample_work(db, monkeypatch, *, remove_gate=False):
    """Count executed membership tests and selected subject projections."""
    counts = {"memberships": 0, "subjects": 0, "statements": 0, "connections": 0}
    connect = db._connect

    def probe(name, value):
        counts[name] += 1
        return value

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, params):
            counts["statements"] += 1
            sql = sql.replace(
                "k.ok IS 1 AND k.value IS sp.value",
                "sample_membership(k.ok) IS 1 AND k.value IS sp.value",
            )
            sql = sql.replace(
                "hex(substr(CAST(m.subject AS BLOB), 1, 2001))",
                "hex(sample_subject(substr(CAST(m.subject AS BLOB), 1, 2001)))",
            )
            if remove_gate:
                sql = sql.replace("sb.requested <= 100", "1 = 1")
            return self.conn.execute(sql, params)

        def close(self):
            self.conn.close()

    def traced():
        counts["connections"] += 1
        conn = connect()
        conn.create_function("sample_membership", 1, lambda v: probe("memberships", v))
        conn.create_function("sample_subject", 1, lambda v: probe("subjects", v))
        return Connection(conn)

    monkeypatch.setattr(db, "_connect", traced)
    return counts


def test_rejection_precedes_membership_and_subject_work(cap_db, monkeypatch):
    for remove_gate in (False, True):
        with monkeypatch.context() as patch:
            counts = sample_work(cap_db, patch, remove_gate=remove_gate)
            with pytest.raises(InvalidFilterError, match="100-row"):
                cap_db.aggregate_messages(group_by="sender_address", sample_per_group=10)
        assert counts["connections"] == counts["statements"] == 1
        # The deliberately ungated shape proves these counters detect the work.
        assert (counts["subjects"] > 0) is remove_gate
        assert (counts["memberships"] > 0) is remove_gate


def test_only_selected_subjects_are_projected(sample_db, monkeypatch):
    counts = sample_work(sample_db, monkeypatch)
    page = sample_db.aggregate_messages(group_by="sender_address", sample_per_group=3, limit=1)
    assert counts["subjects"] == page.samples_returned == 3
    assert counts["memberships"] > page.samples_returned
    assert counts["connections"] == counts["statements"] == 1


def test_empty_and_unknown_only_groups_have_no_samples(sample_db):
    assert (
        sample_db.aggregate_messages(
            group_by="folder", subject="not present", sample_per_group=10
        ).samples_returned
        == 0
    )
    with sqlite3.connect(sample_db.path) as conn:
        conn.execute("UPDATE messages SET subject_complete = NULL")
    page = sample_db.aggregate_messages(
        group_by="sender_address", subject="not present", sample_per_group=10
    )
    assert page.total_matches == page.samples_returned == 0 and page.indeterminate == 13
    assert all(not g.samples and g.messages == 0 for g in page.groups)


@pytest.mark.parametrize("value", [True, -1, 11, "2", 2.0, None])
def test_tool_rejects_non_strict_sample_counts(fake_server, sample_db, value):
    from tests.test_retrieval import _error, _handlers

    handler = _handlers(fake_server, sample_db)["aggregate_messages"]
    result = _error(handler(group_by="folder", sample_per_group=value))
    assert "sample_per_group" in result
