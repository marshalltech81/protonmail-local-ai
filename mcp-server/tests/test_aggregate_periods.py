"""Synthetic temporal aggregates and explicit identity/coverage units (#1338)."""

import asyncio
import logging
import sqlite3
from contextlib import closing

import pytest
from src.lib.predicates import InvalidFilterError
from src.lib.sqlite import Database

from tests.conftest import _insert_attachment, _insert_message, claimant_of
from tests.test_aggregate_messages import _all_groups, _probe, _server
from tests.test_evidence_scope import _finish_threads
from tests.test_retrieval import _handlers, _text
from tests.test_sqlite import _open_built_db_conn
from tests.test_structured_output import _call


@pytest.fixture
def series_db(tmp_path):
    conn, path = _open_built_db_conn(tmp_path, "series.db")
    for mid, variant, date, authors, subject in [
        ("a", "", "2024-01-10", ["a@series.test", "b@series.test"], "series one"),
        ("a", "copy", "2024-01-11", ["a@series.test"], "series one"),
        ("b", "", "2024-02-10", ["a@series.test"], "series two"),
        ("c", "", "2025-01-10", ["a@series.test"], "series one"),
        ("reply", "", "2025-01-11", ["once@single.test"], "reply"),
        ("forward", "", "2025-01-12", ["once@single.test"], "forward"),
        ("undated", "", None, ["a@series.test"], "unknown"),
    ]:
        _insert_message(
            conn,
            message_id=mid,
            variant=variant,
            thread_id="t" if mid == "reply" else mid,
            sent_at=None if date is None else date + "T12:00:00+00:00",
            from_=authors,
            subject=subject,
            has_attachments=mid in ("a", "b", "forward"),
        )
        if mid in ("a", "b", "forward"):
            for slot, payload in enumerate(("same", "same", "other") if mid == "a" else ("same",)):
                _insert_attachment(
                    conn,
                    message_id=mid,
                    variant=variant,
                    thread_id=mid,
                    attachment_id=payload,
                    filename=f"file{slot}.pdf",
                    deferred=mid == "b",
                )
    conn.execute("UPDATE messages SET attachment_rows_current = 1")
    _finish_threads(conn)
    conn.close()
    return Database(str(path))


@pytest.mark.parametrize(("period", "active"), [("month", 3), ("year", 2)])
def test_series_identity_counts_are_not_reports(series_db, period, active):
    page = series_db.aggregate_messages(group_by="sender_address", period=period)
    row = next(g for g in page.groups if g.value == "a@series.test")
    assert (row.messages, row.distinct_message_ids, row.distinct_attachment_ids) == (5, 4, 2)
    assert (row.active_periods, row.undated_messages, row.eligible_total) == (active, 1, 5)
    assert row.attachment_inventory_unknown_messages == 0
    assert page.eligible_total == page.total_matches == 7
    assert sum(g.messages for g in page.groups) == 8  # overlapping From memberships
    once = next(g for g in page.groups if g.value == "once@single.test")
    assert (once.active_periods, once.messages, once.distinct_message_ids) == (1, 2, 2)
    assert (
        series_db.aggregate_messages(group_by="sender_address", subject="series one")
        .groups[0]
        .messages
        == 3
    )


@pytest.mark.parametrize("dimension", ["sender_address", "sender_domain"])
@pytest.mark.parametrize("period", ["month", "year"])
def test_flat_period_cells_page_and_reconcile(series_db, dimension, period):
    groups, page = _all_groups(series_db, dimension, period=period, period_breakdown=True)
    assert page.period_breakdown and page.period == period
    assert len(groups) == page.total_groups
    assert len({(g.value, g.period) for g in groups}) == len(groups)
    summary = series_db.aggregate_messages(group_by=dimension)
    for row in summary.groups:
        cells = [g for g in groups if g.value == row.value]
        assert sum(g.messages for g in cells) == row.messages
        assert sum(g.indeterminate for g in cells) == row.indeterminate
        assert (
            sum(g.active_periods for g in cells)
            == series_db.aggregate_messages(group_by=dimension, period=period)
            .groups[summary.groups.index(row)]
            .active_periods
        )
    assert any(g.period is None and g.undated_messages == 1 for g in groups)


@pytest.mark.parametrize("basis", ["effective", "sent", "occurred"])
def test_clock_totals_follow_message_listing(series_db, basis):
    page = series_db.aggregate_messages(group_by="month", date_basis=basis)
    listing = series_db.query_messages(date_basis=basis)
    assert (page.total_matches, page.indeterminate) == (
        listing.total_matches,
        listing.indeterminate,
    )
    assert page.eligible_total == listing.total_matches + listing.indeterminate
    assert sum(g.messages for g in page.groups) == page.total_matches
    assert sum(g.indeterminate for g in page.groups) == page.indeterminate
    if basis != "effective":
        assert page.undated_messages == 0
        assert page.indeterminate > 0


def test_clock_changes_period_and_dates(series_db):
    with closing(sqlite3.connect(series_db.path)) as conn, conn:
        conn.execute(
            "UPDATE messages SET occurred_at = '2024-02-01T00:00:00+00:00' WHERE message_id='a'"
        )
    for basis, expected in [("sent", "2024-01"), ("occurred", "2024-02"), ("effective", "2024-02")]:
        page = series_db.aggregate_messages(
            group_by="month", subject="series one", date_basis=basis
        )
        assert any(g.value == expected and g.messages == 2 for g in page.groups)


@pytest.mark.parametrize(
    ("stamp", "manifest", "has", "identities", "unknown"),
    [
        (1, 1, 1, 2, 0),
        (1, 0, 1, 2, 1),
        (1, None, 1, 2, 1),
        (None, 1, 1, 0, 1),
        (0, 1, 1, 0, 1),
        (0, 1, 0, 0, 0),
        (None, None, 0, 0, 1),
    ],
)
def test_inventory_currentness_and_completeness_are_separate(
    series_db, stamp, manifest, has, identities, unknown
):
    with closing(sqlite3.connect(series_db.path)) as conn, conn:
        conn.execute(
            "UPDATE messages SET attachment_rows_current=?, attachments_manifest_complete=?, has_attachments=? WHERE claimant_id=?",
            (stamp, manifest, has, claimant_of("a")),
        )
    row = series_db.aggregate_messages(group_by="sender_address", sender="b@series.test").groups[0]
    assert (row.distinct_attachment_ids, row.attachment_inventory_unknown_messages) == (
        identities,
        unknown,
    )


def test_unknown_inventory_counts_only_definite_matches(series_db):
    with closing(sqlite3.connect(series_db.path)) as conn, conn:
        conn.execute(
            "UPDATE messages SET attachment_rows_current=NULL, subject_complete=NULL WHERE message_id='a'"
        )
    page = series_db.aggregate_messages(group_by="sender_domain", subject="unknown")
    assert page.total_matches == 1 and page.indeterminate == 2
    assert page.attachment_inventory_unknown_messages == 0
    assert all(g.distinct_attachment_ids == 0 for g in page.groups)


@pytest.mark.parametrize(
    "change",
    [
        dict(period="year"),
        dict(period_breakdown=True),
        dict(date_basis="sent"),
        dict(group_by="sender_domain"),
    ],
)
def test_cursor_binds_temporal_contract(series_db, change):
    args = dict(group_by="sender_address", limit=1)
    cursor = series_db.aggregate_messages(**args).next_cursor
    with pytest.raises(InvalidFilterError, match="cursor"):
        series_db.aggregate_messages(**(args | change), cursor=cursor)


@pytest.mark.parametrize(
    "args",
    [
        dict(period="day"),
        dict(period_breakdown=True, group_by="folder"),
        dict(date_basis="internal"),
    ],
)
def test_invalid_temporal_parameters(series_db, args):
    with pytest.raises(InvalidFilterError):
        series_db.aggregate_messages(**(dict(group_by="sender_address") | args))


def test_structured_output_and_lower_bound_prose(series_db):
    with closing(sqlite3.connect(series_db.path)) as conn, conn:
        conn.execute("UPDATE messages SET attachments_manifest_complete=0 WHERE message_id='a'")
    out = _call(
        _server(series_db), "aggregate_messages", group_by="sender_domain", period_breakdown=True
    )
    assert out["date_basis"] == "effective" and out["period"] == "month"
    assert out["eligible_total"] == 7 and out["attachment_inventory_unknown_messages"] == 2
    assert all("period" in row and "distinct_attachment_ids" in row for row in out["groups"])


def test_fanout_does_not_repeat_predicate_evaluation(series_db, monkeypatch):
    items = [{"leaf": "subject_contains", "value": "series"}]

    def measure():
        with monkeypatch.context() as patch:
            return _probe(series_db, patch, "sender_domain", items, period_breakdown=True)

    calls, page, connections, statements = measure()
    with closing(sqlite3.connect(series_db.path)) as conn, conn:
        for i in range(300):
            _insert_attachment(
                conn,
                message_id="a",
                thread_id="a",
                attachment_id=f"payload{i}",
                filename=f"item{i}.txt",
            )
    large_calls, large_page, large_connections, large_statements = measure()
    assert large_calls == calls and calls["subject_contains"] > 0
    assert connections == large_connections == 1
    assert statements == large_statements == (1, 1)
    assert large_page.total_matches == page.total_matches
    assert (
        large_page.groups[0].distinct_attachment_ids == page.groups[0].distinct_attachment_ids + 300
    )


def test_more_than_one_hundred_period_cells_are_bounded_and_exhaustive(tmp_path):
    conn, path = _open_built_db_conn(tmp_path, "many-periods.db")
    for i in range(125):
        _insert_message(
            conn,
            message_id=f"m{i}",
            thread_id=f"t{i}",
            from_=["a@period.test"],
            sent_at=f"{2000 + i // 12:04d}-{i % 12 + 1:02d}-01T00:00:00+00:00",
        )
    _finish_threads(conn)
    conn.close()
    db = Database(str(path))
    first = db.aggregate_messages(group_by="sender_address", period_breakdown=True, limit=100)
    assert len(first.groups) == 100 and first.total_groups == 125 and first.has_more
    last = db.aggregate_messages(
        group_by="sender_address", period_breakdown=True, limit=100, cursor=first.next_cursor
    )
    assert len(last.groups) == 25 and not last.has_more
    assert len({g.period for g in first.groups + last.groups}) == 125


def test_legacy_default_cursor_still_works(series_db):
    import base64
    import hashlib
    import json

    from src.lib.predicates import leaf_digest, query_messages_leaves

    leaves = query_messages_leaves(
        sender=None,
        recipient=None,
        participant=None,
        subject=None,
        text=None,
        folder=None,
        date_from=None,
        date_to=None,
        has_attachments=None,
        authority_class=None,
        seen=None,
        flagged=None,
    )
    digest = hashlib.sha256(
        json.dumps(["aggregate_messages", "sender_address", leaf_digest(leaves, where=())]).encode()
    ).hexdigest()[:16]
    cursor = (
        base64.urlsafe_b64encode(
            json.dumps({"v": 1, "t": "aggregate", "q": digest, "o": 1}).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    page = series_db.aggregate_messages(group_by="sender_address", cursor=cursor)
    assert page.offset == 1 and page.groups[0].value == "once@single.test"


def test_lower_bounds_visible_in_prose_and_timing_without_mail(series_db, fake_server, caplog):
    marker = "SYNTHETIC_TEMPORAL_MAIL_MARKER"
    with closing(sqlite3.connect(series_db.path)) as conn, conn:
        conn.execute(
            "UPDATE messages SET subject=?, attachment_rows_current=NULL WHERE message_id='a'",
            (marker,),
        )
    caplog.set_level(logging.INFO)
    result = asyncio.run(
        _handlers(fake_server, series_db)["aggregate_messages"](
            group_by="sender_domain", subject=marker, period_breakdown=True
        )
    )
    assert "lower bound" in _text(result)
    assert "attachment_inventory_unknown_messages': 2" in caplog.text
    assert marker not in caplog.text
