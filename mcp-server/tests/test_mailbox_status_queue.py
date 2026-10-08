"""Queue buckets in ``Database.get_mailbox_status`` (#1165).

A job the indexer deferred by design (a parked trashed file, an
unreadable file waiting for mbsync, an embedder outage or configuration
error, a job waiting for a rename) is not a failing job. Status
reports each kind in its own bucket, so parked trashed files cannot hold
the index non-current, and a deferral reads as such rather than as a
retry.
"""

import ast
from pathlib import Path

import pytest
from src.lib.sqlite import Database

from tests.conftest import write_ingestion

INDEXER_SRC = Path(__file__).resolve().parents[2] / "indexer" / "src"

PERMISSION_DEFERRED = "PermissionError: deferred until mbsync opens the file"
RENAME_DEFERRED = "FileNotFoundError: deferred until the rename is recorded"


def _queue(db: Database, *jobs: tuple) -> dict:
    write_ingestion(db.path, jobs=jobs)
    return db.get_mailbox_status()["queue"]


def test_one_job_of_each_kind_lands_in_its_bucket(empty_db: Database):
    queue = _queue(
        empty_db,
        # Never failed.
        ("queued", 0, None),
        # Failed once at parse, will retry.
        ("queued", 1, "retryable", "x", "parse", "ValueError"),
        # Indexed file now T-flagged, parked until reaped or restored.
        ("queued", 0, "retryable", "x", "trashed", "file is T-flagged; parked"),
        # Unreadable file waiting for mbsync's chmod.
        ("queued", 0, "retryable", "x", "parse", PERMISSION_DEFERRED),
        # Embedder outage: no attempt spent.
        ("queued", 0, "retryable", "x", "embed", "APIConnectionError"),
        # Embedder configuration error.
        ("queued", 0, "operator_action_required", "x", "embed", "AuthenticationError"),
        # Reparse waiting for the watcher to record a rename.
        ("queued", 0, "retryable", "reparse", "parse", RENAME_DEFERRED),
        # Gave up.
        ("dead", 5, "retryable", "x", "parse", "ValueError"),
    )
    assert queue == {
        "pending": 1,
        "retrying": 1,
        "deferred": 4,
        "parked_trashed": 1,
        "dead": 1,
        "reparse": 1,
    }


def test_deferral_after_earlier_failures_is_still_a_deferral(empty_db: Database):
    """``defer`` keeps the attempts a job already spent, so a deferral
    identified by its stage and fixed text or class stays deferred."""
    queue = _queue(
        empty_db,
        ("queued", 2, "retryable", "x", "trashed", "file is T-flagged; parked"),
        ("queued", 2, "retryable", "x", "parse", PERMISSION_DEFERRED),
        ("queued", 2, "operator_action_required", "x", "embed", "AuthenticationError"),
    )
    assert (queue["parked_trashed"], queue["deferred"], queue["retrying"]) == (1, 2, 0)


def test_embed_failure_with_attempts_is_retrying(empty_db: Database):
    """A per-message embed failure spends an attempt and records
    ``retryable``; stored columns cannot tell it from an outage deferral
    of a job that had already failed, so both read as retrying."""
    queue = _queue(empty_db, ("queued", 1, "retryable", "x", "embed", "BadRequestError"))
    assert (queue["retrying"], queue["deferred"]) == (1, 0)


def test_permission_error_past_its_window_is_retrying(empty_db: Database):
    """Past the deferral window the indexer records the error itself
    (``_stage_error``), not the deferral text, and spends an attempt."""
    queue = _queue(
        empty_db,
        ("queued", 1, "retryable", "x", "parse", "PermissionError: [Errno 13]"),
    )
    assert (queue["retrying"], queue["deferred"]) == (1, 0)


def test_requeued_dead_job_is_pending_whatever_its_last_stage(empty_db: Database):
    """``make requeue-dead`` clears attempts and class but keeps the last
    stage and error, so a deferral stage alone does not make a deferral."""
    queue = _queue(
        empty_db,
        ("queued", 0, None, "x", "embed", "APIConnectionError"),
        ("queued", 0, None, "x", "parse", PERMISSION_DEFERRED),
        ("queued", 0, None, "x", "trashed", "file is T-flagged; parked"),
    )
    assert queue["pending"] == 3
    assert queue["deferred"] == queue["parked_trashed"] == queue["retrying"] == 0


def test_reparse_counts_waiting_jobs_only(empty_db: Database):
    """``reparse`` counts reparse jobs in the buckets that wait to be
    indexed (pending, retrying, deferred), not parked or dead ones."""
    queue = _queue(
        empty_db,
        ("queued", 0, None, "reparse"),
        ("queued", 1, "retryable", "reparse", "parse", "ValueError"),
        ("queued", 0, "retryable", "reparse", "parse", RENAME_DEFERRED),
        ("queued", 0, "retryable", "reparse", "trashed", "file is T-flagged; parked"),
        ("dead", 5, "retryable", "reparse", "parse", "ValueError"),
    )
    assert queue["reparse"] == 3


# --- every indexer deferral call site is a deferral in status ----------


def _module_strings(path: Path) -> dict[str, str]:
    """Module-level ``NAME = "text"`` assignments."""
    out = {}
    for node in ast.parse(path.read_text()).body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            out[node.targets[0].id] = node.value.value
    return out


def _defer_call_sites() -> list[dict]:
    """Each ``<queue>.defer(...)`` call in the indexer, with its ``stage``,
    ``error`` and ``error_class`` resolved to text where they are
    module constants (``None`` where the value is computed at run time)."""
    strings: dict[str, str] = {}
    for path in sorted(INDEXER_SRC.glob("*.py")):
        strings.update(_module_strings(path))
    sites = []
    for path in sorted(INDEXER_SRC.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "defer"
            ):
                continue
            site = {"where": f"{path.name}:{node.lineno}"}
            for kw in node.keywords:
                value = kw.value
                if isinstance(value, ast.Constant):
                    site[kw.arg] = value.value
                elif isinstance(value, ast.Name) and value.id in strings:
                    site[kw.arg] = strings[value.id]
                else:
                    site[kw.arg] = None
            sites.append(site)
    return sites


_SITES = _defer_call_sites()
_ERROR_CLASSES = sorted(
    value
    for name, value in _module_strings(INDEXER_SRC / "queue.py").items()
    if name.startswith("ERROR_CLASS_")
)


def test_the_indexer_has_deferral_call_sites():
    # Guards the derivation below against matching nothing.
    assert len(_SITES) >= 4
    assert all(site["stage"] is not None for site in _SITES), _SITES


@pytest.mark.parametrize("site", _SITES, ids=lambda site: site["where"])
def test_every_indexer_deferral_is_reported_as_deferred(empty_db: Database, site):
    """A new ``defer`` call site whose stage or text status does not know
    would fall back into ``retrying``; this fails instead (#1165).

    The rows come from the indexer's own call sites, not from the
    status code under test. A computed error text gets a synthetic
    value; a computed error class is tried with every class the queue
    defines.
    """
    error = site["error"] if site["error"] is not None else "SYNTHETIC_PROVIDER_ERROR_1165"
    classes = [site["error_class"]] if site["error_class"] is not None else _ERROR_CLASSES
    jobs = tuple(("queued", 0, cls, "x", site["stage"], error) for cls in classes)
    queue = _queue(empty_db, *jobs)
    assert queue["pending"] == queue["retrying"] == 0, site
    assert queue["deferred"] + queue["parked_trashed"] == len(jobs), site
