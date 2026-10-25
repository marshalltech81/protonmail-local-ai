"""The in-memory probe (procedure v5 5.1, 5.2).

One process holds every cross-step comparison in memory for the whole run.
For each delivered file it parses with the indexer's own parser, builds
``Evidence`` as the indexer's read of the stored rows builds it
(``database._IDENTITY_EVIDENCE_SQL``: the capture's completeness, the
count of ``x-pm-internal-id`` rows, the first one's raw value by ordinal)
and calls ``assess``. It records counts per reason, booleans and parser
tokens, never a value: a candidate is kept only as a keyed digest whose
key lives and dies with this process, so "equal within the run" can be
judged without the identifier ever being printed or written.

Parse path: ``parser.parse_email_bytes`` with ``capture_full_body=False``.
That skips the additive full-body HTML pass only; the ordinary body path
still reaches the HTML child for an HTML part (``parser.py``,
``_convert_html_parts``), which on macOS has CPU, wall-clock and output
limits but no address-space limit (D9 of the procedure).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

from src import parser
from src.provider_identity import Evidence, assess

from . import export

POLICY_REASONS = export.REASON_TOKENS
IDENTITY_NAME = "x-pm-internal-id"


class ProbeError(Exception):
    """An unexpected failure while parsing one file. Fixed text: the type
    name only, since a parser error can quote the message it rejected."""


@dataclass(frozen=True)
class Observation:
    outcome: str | None
    reason: str | None
    candidate_present: bool
    parse_token: str | None


def evidence_from_message(msg: parser.Message, folder: str | None = None) -> Evidence:
    """``Evidence`` as the indexer reads it from the stored rows."""
    fields = sorted(
        (h for h in msg.headers if h.name_normalized == IDENTITY_NAME), key=lambda h: h.ordinal
    )
    return Evidence(
        folder=msg.folder if folder is None else folder,
        headers_complete=msg.headers_complete,
        identity_count=len(fields),
        identity_value=fields[0].raw_value if fields else None,
    )


class Probe:
    def __init__(self) -> None:
        self._key = secrets.token_bytes(32)
        self._digests: dict[str, bytes] = {}
        self._reasons: Counter[str] = Counter()
        self._outcomes: Counter[str] = Counter()
        self._parser: Counter[str] = Counter()
        self.observed = 0

    def observe(self, raw: bytes, folder: str, *, token: str | None = None) -> Observation:
        source = parser.SourceMetadata(
            folder=folder, flags=frozenset(), size=len(raw), mtime_ns=None, path="probe"
        )
        self.observed += 1
        if token is not None:
            # A token seen again never keeps its earlier digest: a stale one
            # would let equal() report a match the latest observation does
            # not support (no candidate, a parse failure, a nesting error).
            self._digests.pop(token, None)
        try:
            msg = parser.parse_email_bytes(raw, source, capture_full_body=False)
        except parser.MessageNestingError:
            self._parser["parse_nesting_error"] += 1
            return Observation(None, None, False, "parse_nesting_error")
        except Exception as exc:
            raise ProbeError(type(exc).__name__) from None
        if msg is None:
            self._parser["parse_none"] += 1
            return Observation(None, None, False, "parse_none")
        assessment = assess(evidence_from_message(msg, folder))
        self._reasons[assessment.reason] += 1
        self._outcomes[assessment.outcome] += 1
        if assessment.candidate is not None and token is not None:
            self._digests[token] = hmac.new(
                self._key, assessment.candidate.encode(), hashlib.sha256
            ).digest()
        return Observation(
            assessment.outcome, assessment.reason, assessment.candidate is not None, None
        )

    def equal(self, token_a: str, token_b: str) -> bool | None:
        """Whether two observed messages carry the same candidate: ``None``
        when either has no candidate (not comparable)."""
        a, b = self._digests.get(token_a), self._digests.get(token_b)
        if a is None or b is None:
            return None
        return hmac.compare_digest(a, b)

    def reason_counts(self) -> dict[str, int]:
        return dict(self._reasons)

    def outcome_counts(self) -> dict[str, int]:
        return dict(self._outcomes)

    def parser_counts(self) -> dict[str, int]:
        return dict(self._parser)

    def export(
        self,
        *,
        counts: Mapping[str, int] | None = None,
        booleans: Mapping[str, bool] | None = None,
        predictions: Mapping[str, str] | None = None,
        timings: Mapping[str, float] | None = None,
    ) -> dict[str, dict]:
        """The allowlisted export; the other sections (counts, booleans,
        predictions, timings) come from the runner."""
        merged = {"messages_observed": self.observed, **(counts or {})}
        return export.build_export(
            reasons=self._reasons,
            outcomes=self._outcomes,
            parser=self._parser,
            counts=merged,
            booleans=booleans,
            predictions=predictions,
            timings=timings,
        )

    def export_json(
        self,
        *,
        counts: Mapping[str, int] | None = None,
        booleans: Mapping[str, bool] | None = None,
        predictions: Mapping[str, str] | None = None,
        timings: Mapping[str, float] | None = None,
    ) -> str:
        return export.to_json(
            self.export(counts=counts, booleans=booleans, predictions=predictions, timings=timings)
        )
