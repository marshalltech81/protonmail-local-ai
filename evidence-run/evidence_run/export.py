"""The allowlisted export of a run (procedure v5 5.5).

Everything that leaves the run is a count, a boolean, a timing or a token
from a fixed vocabulary. There is no free-text field, so no identifier,
address, subject or label name can be carried: ``validate_export`` refuses
any key or value outside the allowlist, and ``to_json`` validates first.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping

REASON_TOKENS = frozenset(
    {
        "recovered_folder",
        "headers_unknown",
        "headers_incomplete",
        "no_identity_header",
        "duplicate_identity_header",
        "malformed_identity",
        "no_acceptance_rule",
    }
)
OUTCOME_TOKENS = frozenset({"unresolved", "rejected"})  # never "accepted"
PARSER_TOKENS = frozenset({"parse_none", "parse_nesting_error"})
PREDICTION_STATES = frozenset({"predicted", "observed", "failed", "unobservable"})
COUNT_KEYS = frozenset(
    {
        "submission_attempts",
        "retries",
        "ambiguous_outcomes",
        "isync_commands",
        "unique_bytes",
        "transfer_bytes",
        "service_messages",
        "messages_observed",
    }
)
BOOLEAN_KEYS = frozenset(
    {"isolation_passed", "tls_pin_checked", "stopped_early", "all_predictions_held"}
)
_PREDICTION_ID = re.compile(r"P(?:[1-9]|1[0-2])")
_TIMING_KEY = re.compile(r"g[1-8]_seconds")
_MAX_COUNT = 10**12
_MAX_SECONDS = 10**7

SECTIONS = ("reasons", "outcomes", "parser", "counts", "booleans", "predictions", "timings")


class ExportError(ValueError):
    """An export holds something outside the allowlist. The message is
    fixed text: it never repeats the offending key or value."""


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MAX_COUNT


def _seconds(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= _MAX_SECONDS
    )


def validate_export(data: Mapping[str, object]) -> None:
    if set(data) - set(SECTIONS):
        raise ExportError("an export section is not on the allowlist")
    checks = {
        "reasons": (lambda k: k in REASON_TOKENS, _count),
        "outcomes": (lambda k: k in OUTCOME_TOKENS, _count),
        "parser": (lambda k: k in PARSER_TOKENS, _count),
        "counts": (lambda k: k in COUNT_KEYS, _count),
        "booleans": (lambda k: k in BOOLEAN_KEYS, lambda v: isinstance(v, bool)),
        "predictions": (
            lambda k: bool(_PREDICTION_ID.fullmatch(k)),
            lambda v: isinstance(v, str) and v in PREDICTION_STATES,
        ),
        "timings": (lambda k: bool(_TIMING_KEY.fullmatch(k)), _seconds),
    }
    for section, (key_ok, value_ok) in checks.items():
        body = data.get(section, {})
        if not isinstance(body, Mapping):
            raise ExportError("an export section is not a mapping")
        for key, value in body.items():
            if not isinstance(key, str) or not key_ok(key):
                raise ExportError("an export key is not on the allowlist")
            if not value_ok(value):
                raise ExportError("an export value is not on the allowlist")


def build_export(
    *,
    reasons: Mapping[str, int] | None = None,
    outcomes: Mapping[str, int] | None = None,
    parser: Mapping[str, int] | None = None,
    counts: Mapping[str, int] | None = None,
    booleans: Mapping[str, bool] | None = None,
    predictions: Mapping[str, str] | None = None,
    timings: Mapping[str, float] | None = None,
) -> dict[str, dict]:
    data: dict[str, dict] = {
        "reasons": dict(reasons or {}),
        "outcomes": dict(outcomes or {}),
        "parser": dict(parser or {}),
        "counts": dict(counts or {}),
        "booleans": dict(booleans or {}),
        "predictions": dict(predictions or {}),
        "timings": dict(timings or {}),
    }
    validate_export(data)
    return data


def to_json(data: Mapping[str, object]) -> str:
    validate_export(data)
    return json.dumps(data, sort_keys=True)
