"""#1592: get_mailbox_status's ``collection`` block (mbsync's latest
collection outcome, from the indexer's ``account_collection_state`` row),
its effect on ``current``, and ``upstream_currency``, always unknown."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastmcp.exceptions import ToolError
from src.tools.outputs import MailboxStatusOutput
from src.tools.system import get_mailbox_status, register_system_tools

from tests.conftest import write_ingestion

MARKER = "Folders/MarkerZq9-1592"


def _ago(**kw) -> str:
    return (datetime.now(UTC) - timedelta(**kw)).isoformat()


def _known(**overrides) -> dict:
    """A recorded success: the attempt finished, delivered."""
    row = {
        "status": "known",
        "run_deadline_secs": 86400,
        "sync_interval_secs": 60,
        "attempt_id": "0123456789ab-2",
        "attempt_started_at": _ago(seconds=50),
        "attempt_outcome": "succeeded",
        "terminal_attempt_id": "0123456789ab-2",
        "terminal_completed_at": _ago(seconds=30),
        "terminal_outcome": "succeeded",
        "terminal_withheld_folders": 0,
        "terminal_failure_class": None,
        "success_attempt_id": "0123456789ab-2",
        "success_completed_at": _ago(seconds=30),
        "success_delivered": 1,
        "unrecovered_failure_attempt_id": None,
    }
    return row | overrides


def _failed(**overrides) -> dict:
    return (
        _known(
            attempt_outcome="failed",
            terminal_outcome="failed",
            terminal_failure_class="deadline",
            success_attempt_id="0123456789ab-1",
            success_completed_at=_ago(minutes=5),
            unrecovered_failure_attempt_id="0123456789ab-2",
        )
        | overrides
    )


def _status(fake_server, db, collection):
    write_ingestion(
        db.path,
        sync_completed_at=_ago(seconds=30),
        sync_interval_secs=60,
        indexer_seen_at=_ago(seconds=5),
        collection=collection,
    )
    register_system_tools(fake_server, db)
    out = asyncio.run(fake_server.tools["get_mailbox_status"]())
    MailboxStatusOutput.model_validate(out.structured_content)
    return out.structured_content, out.content[0].text


def test_no_recorded_row_is_an_unknown_outcome(fake_server, seeded_db):
    data, text = _status(fake_server, seeded_db, None)
    assert data["collection"]["outcome"] == "unknown"
    assert data["collection"]["unknown_reason"] == "not_recorded"
    assert data["collection"]["failure_unrecovered"] is False
    assert data["upstream_currency"] == "unknown"
    # Freshness keeps its existing criteria.
    assert data["current"] is True
    assert "Mail collection: unknown (the indexer has not recorded it yet)" in text
    assert "Upstream (Bridge/Proton) currency: unknown" in text


def test_a_clean_success(fake_server, seeded_db):
    data, text = _status(fake_server, seeded_db, _known())
    c = data["collection"]
    assert (c["outcome"], c["withheld_far_side_folders"], c["failure_class"]) == (
        "succeeded",
        0,
        None,
    )
    assert c["latest_attempt"]["outcome"] == "succeeded"
    assert c["last_success_at"] is not None and c["outcome_at"] is not None
    assert data["current"] is True
    assert "Mail collection: succeeded, " in text
    assert "far-side folder" not in text


def test_a_success_that_withheld_folders_keeps_current_and_shows_the_count(fake_server, seeded_db):
    data, text = _status(
        fake_server,
        seeded_db,
        _known(
            attempt_outcome="succeeded_with_withheld_folders",
            terminal_outcome="succeeded_with_withheld_folders",
            terminal_withheld_folders=2,
        ),
    )
    assert data["collection"]["outcome"] == "succeeded_with_withheld_folders"
    assert data["collection"]["withheld_far_side_folders"] == 2
    assert data["current"] is True
    assert "2 far-side folders could not be opened" in text


@pytest.mark.parametrize("withheld", [1, 3])
def test_a_failure_with_withheld_folders_does_not_claim_the_rest_synced(
    fake_server, seeded_db, withheld
):
    """A far-side failure together with another error is a failure: the
    other error may have stopped the rest, so the count is reported
    without saying anything else synced."""
    data, text = _status(
        fake_server,
        seeded_db,
        _failed(terminal_failure_class="error", terminal_withheld_folders=withheld),
    )
    assert data["collection"]["withheld_far_side_folders"] == withheld
    folders = "folder" if withheld == 1 else "folders"
    assert f"  {withheld} far-side {folders} could not be opened in that failed collection." in text
    assert "the rest synced" not in text


@pytest.mark.parametrize(
    "failure_class", ["deadline", "killed", "error", "permission_repair", "interrupted"]
)
def test_a_failure_makes_the_index_not_current(fake_server, seeded_db, failure_class):
    data, text = _status(
        fake_server, seeded_db, _failed() | {"terminal_failure_class": failure_class}
    )
    assert data["collection"]["outcome"] == "failed"
    assert data["collection"]["failure_class"] == failure_class
    assert data["collection"]["failure_unrecovered"] is True
    assert data["current"] is False
    assert data["not_current_reasons"] == [
        f"mbsync's latest collection attempt failed ({failure_class}); the index is not "
        "current until a later successful sync is delivered to it"
    ]
    assert f"Mail collection: failed ({failure_class})" in text


def test_a_later_delivered_sync_recovers_a_stale_failed_publication(fake_server, seeded_db):
    data, text = _status(fake_server, seeded_db, _failed(unrecovered_failure_attempt_id=None))
    assert data["collection"]["outcome"] == "failed"
    assert data["collection"]["failure_unrecovered"] is False
    assert data["current"] is True
    assert data["not_current_reasons"] == []
    assert "recovered by a later delivered sync" in text


def test_a_retry_in_progress_keeps_the_failure(fake_server, seeded_db):
    data, text = _status(
        fake_server,
        seeded_db,
        _failed(attempt_id="0123456789ab-3", attempt_outcome="in_progress"),
    )
    assert data["collection"]["latest_attempt"]["outcome"] == "in_progress"
    assert data["collection"]["outcome"] == "failed"
    assert data["current"] is False
    assert "with no outcome recorded yet; this does not show whether mbsync is still running" in (
        text
    )


def test_a_success_not_yet_delivered_after_a_failure_is_not_current(fake_server, seeded_db):
    data, _ = _status(
        fake_server,
        seeded_db,
        _known(success_delivered=0, unrecovered_failure_attempt_id="0123456789ab-1"),
    )
    assert data["collection"]["outcome"] == "succeeded"
    assert data["collection"]["failure_unrecovered"] is True
    assert data["current"] is False
    assert data["not_current_reasons"] == [
        "mbsync's last failed collection attempt has not yet been followed by a successful "
        "sync delivered to the index"
    ]


def test_a_success_not_yet_delivered_with_no_observed_failure_is_not_current(
    fake_server, seeded_db
):
    """#1612 round 12: a watcher delayed or behind enough that it reads
    the collection status file only after a failed publication was
    already overwritten by a later successful one never observes the
    failure at all (``unrecovered_failure_attempt_id`` stays null) — the
    case above (an *observed* failure, still unrecovered) does not cover
    this. ``success_delivered`` is checked on its own so the gap closes
    whether or not the intervening failure was ever seen."""
    data, _ = _status(fake_server, seeded_db, _known(success_delivered=0))
    assert data["collection"]["outcome"] == "succeeded"
    assert data["collection"]["failure_unrecovered"] is False
    assert data["collection"]["success_undelivered"] is True
    assert data["current"] is False
    assert data["not_current_reasons"] == [
        "mbsync's latest successful collection has not yet been confirmed delivered to the index"
    ]


def test_the_first_attempt_in_progress_has_no_outcome_yet(fake_server, seeded_db):
    data, text = _status(
        fake_server,
        seeded_db,
        _known(
            attempt_outcome="in_progress",
            terminal_attempt_id=None,
            terminal_completed_at=None,
            terminal_outcome=None,
            terminal_withheld_folders=None,
            success_attempt_id=None,
            success_completed_at=None,
            success_delivered=None,
        ),
    )
    assert data["collection"]["outcome"] == "unknown"
    assert data["collection"]["unknown_reason"] == "no_completed_attempt"
    assert "Mail collection: unknown (no collection attempt has completed yet)" in text


@pytest.mark.parametrize(
    "reason", ["missing", "legacy", "malformed", "unsupported", "account_mismatch", "unreadable"]
)
def test_an_unknown_status_is_never_a_success(fake_server, seeded_db, reason):
    data, text = _status(fake_server, seeded_db, {"status": "unknown", "unknown_reason": reason})
    assert data["collection"]["outcome"] == "unknown"
    assert data["collection"]["unknown_reason"] == reason
    assert data["collection"]["latest_attempt"] is None
    assert data["current"] is True
    assert "Mail collection: unknown (" in text


def test_an_unknown_status_keeps_an_unrecovered_failure(fake_server, seeded_db):
    data, _ = _status(
        fake_server,
        seeded_db,
        {
            "status": "unknown",
            "unknown_reason": "malformed",
            "unrecovered_failure_attempt_id": "0123456789ab-1",
        },
    )
    assert data["collection"]["failure_unrecovered"] is True
    assert data["current"] is False


def test_an_unexpected_stored_value_reports_its_type_only(fake_server, seeded_db, caplog):
    write_ingestion(
        seeded_db.path,
        indexer_seen_at=_ago(seconds=5),
        collection=_failed(terminal_failure_class=MARKER),
    )
    register_system_tools(fake_server, seeded_db)
    with caplog.at_level("DEBUG"), pytest.raises(ToolError) as excinfo:
        asyncio.run(fake_server.tools["get_mailbox_status"]())
    assert str(excinfo.value) == "Mailbox status error: ValidationError"
    assert MARKER not in caplog.text


def test_make_status_reports_the_block(seeded_db, monkeypatch):
    write_ingestion(
        seeded_db.path,
        sync_completed_at=_ago(seconds=30),
        sync_interval_secs=60,
        indexer_seen_at=_ago(seconds=5),
        collection=_failed(),
    )
    monkeypatch.setenv("SQLITE_PATH", seeded_db.path)
    status = get_mailbox_status()
    assert status["status"] == "ok"
    assert status["collection"]["outcome"] == "failed"
    assert status["upstream_currency"] == "unknown"
    assert status["current"] is False
