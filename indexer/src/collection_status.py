"""mbsync's collection status file (#1592), format 1.

``mbsync/entrypoint.sh`` (``publish_collection_status``) publishes
``.mbsync-collection-status.json`` at the Maildir root before and after
every sync attempt, each time to a unique temporary file renamed into
place. It holds:

* ``format`` (1), ``scope_uuid`` (the account, ``ACCOUNT_SCOPE``),
  ``run_deadline_secs`` and ``sync_interval_secs``;
* ``attempt``: the latest attempt's ID, start time and outcome
  (``in_progress`` until its terminal outcome is published);
* ``last_terminal``: the last terminal outcome (``succeeded``,
  ``succeeded_with_withheld_folders`` or ``failed``) with its attempt ID,
  completion time, withheld far-side folder count (``null`` when unknown)
  and failure class (``null`` for a success);
* ``last_success``: the last success's attempt ID and completion time,
  which is the success stamp's for the same sync.

The records follow publication order, never their timestamps, so a clock
rollback cannot reorder them. The file names no folder: counts and fixed
tokens only. Reading it never acknowledges a sync; the success stamp
(``maildir.read_sync_stamp``) does that.

``read_collection_status`` returns the parsed status, or ``Unknown`` with
a fixed reason token for a missing, legacy, malformed, unsupported,
unreadable or other-account file. Nothing the file holds reaches an
exception or log message.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

COLLECTION_STATUS_NAME = ".mbsync-collection-status.json"
COLLECTION_STATUS_FORMAT = 1
# mbsync's file is a few hundred bytes; anything longer is not one.
MAX_STATUS_BYTES = 4096

TERMINAL_OUTCOMES = frozenset({"succeeded", "succeeded_with_withheld_folders", "failed"})
ATTEMPT_OUTCOMES = TERMINAL_OUTCOMES | {"in_progress"}
FAILURE_CLASSES = frozenset({"deadline", "killed", "error", "permission_repair", "interrupted"})
# Why a status is unknown: no file, a file without a ``format`` (written
# before the format existed), one that is not format-1 JSON in the
# expected shape, a format this release does not read, one naming another
# account, or one the indexer could not open or read.
UNKNOWN_REASONS = frozenset(
    {"missing", "legacy", "malformed", "unsupported", "account_mismatch", "unreadable"}
)

_ATTEMPT_ID_RE = re.compile(r"[0-9a-f]{12}-[1-9][0-9]{0,9}", re.ASCII)
_TIME_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", re.ASCII)
_MAX_COUNT = 999_999_999

_TOP_KEYS = frozenset(
    {
        "format",
        "scope_uuid",
        "run_deadline_secs",
        "sync_interval_secs",
        "attempt",
        "last_terminal",
        "last_success",
    }
)
_ATTEMPT_KEYS = frozenset({"id", "started_at", "outcome"})
_TERMINAL_KEYS = frozenset(
    {"attempt_id", "completed_at", "outcome", "withheld_far_side_folders", "failure_class"}
)
_SUCCESS_KEYS = frozenset({"attempt_id", "completed_at"})


class _Malformed(ValueError):
    """The file is not in the format-1 shape. Fixed text only."""


@dataclass(frozen=True)
class Terminal:
    attempt_id: str
    completed_at: str  # ISO 8601, UTC, in ``SyncStamp.completed_at``'s form
    outcome: str
    withheld_far_side_folders: int | None
    failure_class: str | None


@dataclass(frozen=True)
class Success:
    attempt_id: str
    completed_at: str  # ISO 8601, UTC, in ``SyncStamp.completed_at``'s form


@dataclass(frozen=True)
class CollectionStatus:
    run_deadline_secs: int
    sync_interval_secs: int
    attempt_id: str
    attempt_started_at: str
    attempt_outcome: str
    last_terminal: Terminal | None
    last_success: Success | None


@dataclass(frozen=True)
class Unknown:
    reason: str  # one of UNKNOWN_REASONS


def _time(value: object) -> str:
    """mbsync's UTC time, normalized as ``maildir._sync_stamp`` normalizes
    the success stamp's, so the two compare as strings."""
    if not isinstance(value, str) or _TIME_RE.fullmatch(value) is None:
        raise _Malformed("time")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _Malformed("time") from None
    return parsed.astimezone(UTC).isoformat()


def _attempt_id(value: object) -> str:
    if not isinstance(value, str) or _ATTEMPT_ID_RE.fullmatch(value) is None:
        raise _Malformed("attempt id")
    return value


def _positive(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_COUNT:
        raise _Malformed("positive integer")
    return value


def _object(value: object, keys: frozenset[str]) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise _Malformed("object")
    return value


def _terminal(value: object) -> Terminal | None:
    if value is None:
        return None
    record = _object(value, _TERMINAL_KEYS)
    outcome = record["outcome"]
    withheld = record["withheld_far_side_folders"]
    failure_class = record["failure_class"]
    if outcome not in TERMINAL_OUTCOMES:
        raise _Malformed("outcome")
    if withheld is not None and (type(withheld) is not int or not 0 <= withheld <= _MAX_COUNT):
        raise _Malformed("count")
    if outcome == "failed":
        if failure_class not in FAILURE_CLASSES:
            raise _Malformed("failure class")
    elif failure_class is not None:
        raise _Malformed("failure class")
    # A clean success withheld nothing (an unknown count is allowed: the
    # error filter's counts may not arrive); a degraded one withheld some.
    if outcome == "succeeded" and withheld not in (0, None):
        raise _Malformed("count")
    if outcome == "succeeded_with_withheld_folders" and not withheld:
        raise _Malformed("count")
    return Terminal(
        attempt_id=_attempt_id(record["attempt_id"]),
        completed_at=_time(record["completed_at"]),
        outcome=outcome,
        withheld_far_side_folders=withheld,
        failure_class=failure_class,
    )


def _success(value: object) -> Success | None:
    if value is None:
        return None
    record = _object(value, _SUCCESS_KEYS)
    return Success(
        attempt_id=_attempt_id(record["attempt_id"]),
        completed_at=_time(record["completed_at"]),
    )


def parse_collection_status(raw: bytes, scope_uuid: str) -> CollectionStatus | Unknown:
    """The status in ``raw`` (the file's bytes), checked against the
    configured account ``scope_uuid``."""
    if len(raw) > MAX_STATUS_BYTES:
        return Unknown("malformed")
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:  # UnicodeDecodeError and JSONDecodeError included
        return Unknown("malformed")
    if not isinstance(data, dict):
        return Unknown("malformed")
    if "format" not in data:
        return Unknown("legacy")
    if type(data["format"]) is not int:
        return Unknown("malformed")
    if data["format"] != COLLECTION_STATUS_FORMAT:
        return Unknown("unsupported")
    try:
        if set(data) != _TOP_KEYS or not isinstance(data["scope_uuid"], str):
            raise _Malformed("object")
        if data["scope_uuid"] != scope_uuid:
            return Unknown("account_mismatch")
        attempt = _object(data["attempt"], _ATTEMPT_KEYS)
        if attempt["outcome"] not in ATTEMPT_OUTCOMES:
            raise _Malformed("outcome")
        status = CollectionStatus(
            run_deadline_secs=_positive(data["run_deadline_secs"]),
            sync_interval_secs=_positive(data["sync_interval_secs"]),
            attempt_id=_attempt_id(attempt["id"]),
            attempt_started_at=_time(attempt["started_at"]),
            attempt_outcome=attempt["outcome"],
            last_terminal=_terminal(data["last_terminal"]),
            last_success=_success(data["last_success"]),
        )
    except _Malformed:
        return Unknown("malformed")
    # The records agree with each other as mbsync writes them: a finished
    # attempt is the last terminal one, and a success is the last success.
    terminal, success = status.last_terminal, status.last_success
    if status.attempt_outcome != "in_progress" and (
        terminal is None
        or terminal.attempt_id != status.attempt_id
        or terminal.outcome != status.attempt_outcome
    ):
        return Unknown("malformed")
    if (
        terminal is not None
        and terminal.outcome != "failed"
        and (
            success is None
            or success.attempt_id != terminal.attempt_id
            or success.completed_at != terminal.completed_at
        )
    ):
        return Unknown("malformed")
    return status


def read_collection_status(root: Path, scope_uuid: str) -> CollectionStatus | Unknown:
    """The status file at ``root``, or ``Unknown`` with its reason.

    mbsync replaces the file by rename, so the descriptor opened here
    reads one whole publication even while a newer one replaces it. Only
    a regular file is read (opened non-blocking, so a FIFO in its place
    cannot stall the caller), and at most ``MAX_STATUS_BYTES`` + 1 bytes.
    """
    try:
        fd = os.open(root / COLLECTION_STATUS_NAME, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return Unknown("missing")
    except OSError:
        return Unknown("unreadable")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return Unknown("unreadable")
        chunks = []
        remaining = MAX_STATUS_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        return Unknown("unreadable")
    finally:
        os.close(fd)
    return parse_collection_status(b"".join(chunks), scope_uuid)
