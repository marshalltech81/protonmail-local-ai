"""Provider-identity assessment under ``proton-bridge-v1`` (#1598).

The Proton Mail Bridge writes Proton's message ID into the messages it
builds as ``X-Pm-Internal-Id``. Every message is attacker-controlled
input, so a header is not an identity until a policy accepts it, and the
owner's decision of 2026-10-23 (option A) is that this policy accepts
nothing: its evidence is a source review and a harness that never sent
an APPEND through Bridge's connector (#1664), and a failed import is kept
unchanged in ``Recovered Messages``, so a forged ID can pass any shape
check. ``assess`` therefore returns ``unresolved`` or ``rejected`` for
every input, and the database refuses to store ``accepted``.

What an assessment records, per occurrence and account: the candidate
(the one complete ``X-Pm-Internal-Id`` field, when it has the canonical
form), the outcome, and a fixed-token reason. It is a reading of stored
header evidence, never of the file, and binds nothing to the bytes the
Bridge served (the Maildir file is not the served literal; H3 in
``docs/architecture.md``). ``X-Pm-Gluon-Id`` is not read at all.

Provider IDs are mailbox content: they appear in rows, never in a log
line or an exception message. Everything logged here is a count.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .database import Database

POLICY_VERSION = "proton-bridge-v1"

# The provider whose accounts this policy reads. An account of another
# provider is never assessed under it.
POLICY_PROVIDER = "proton-bridge"

# Recovery mail keeps the client's own headers (S7, #1317); the folder is
# the provider's name for it, the same one mcp-server's provider policy
# uses (``sources.PROVIDER_POLICIES``).
RECOVERED_FOLDER = "Recovered Messages"

# The outcomes the write path stores. ``accepted`` is in the schema's
# vocabulary and in the effective relation, and is not writable.
WRITABLE_OUTCOMES = frozenset({"unresolved", "rejected"})

# The canonical form of a provider ID, as ``database.PROVIDER_ID_CHECK``
# states it: 1 to 256 printable ASCII characters, no whitespace.
_CANONICAL_ID = re.compile(r"[!-~]{1,256}")


@dataclass(frozen=True)
class Evidence:
    """What the policy reads for one occurrence: its folder, whether the
    claimant's header capture was whole (``None``: not recorded), how
    many ``X-Pm-Internal-Id`` fields it holds and the first one's raw
    value."""

    folder: str
    headers_complete: bool | None
    identity_count: int
    identity_value: str | None


@dataclass(frozen=True)
class Assessment:
    outcome: str
    reason: str
    candidate: str | None


@dataclass(frozen=True)
class Pending:
    """An occurrence waiting for an assessment under ``policy_version``,
    with the evidence read for it. The write path re-reads both and
    refuses the assessment if either has changed (a stale worker)."""

    occurrence_id: int
    policy_version: str
    evidence: Evidence


@dataclass(frozen=True)
class BatchResult:
    counts: dict[str, int]
    stale: int
    remaining: int

    @property
    def assessed(self) -> int:
        return sum(self.counts.values())


def assess(evidence: Evidence) -> Assessment:
    """The assessment of one occurrence's evidence. Pure: no input yields
    ``accepted``."""
    if evidence.folder == RECOVERED_FOLDER:
        # Its own untrusted domain: rejected whatever it carries, and the
        # value is not kept.
        return Assessment("rejected", "recovered_folder", None)
    if evidence.headers_complete is None:
        return Assessment("unresolved", "headers_unknown", None)
    if not evidence.headers_complete:
        # A capture that stopped may have stopped before the field, or
        # before a second one.
        return Assessment("unresolved", "headers_incomplete", None)
    if evidence.identity_count == 0:
        return Assessment("unresolved", "no_identity_header", None)
    if evidence.identity_count > 1:
        return Assessment("unresolved", "duplicate_identity_header", None)
    value = evidence.identity_value
    if value is None or _CANONICAL_ID.fullmatch(value) is None:
        return Assessment("unresolved", "malformed_identity", None)
    return Assessment("unresolved", "no_acceptance_rule", value)


def reassess_batch(db: Database, *, limit: int) -> BatchResult:
    """Assess up to ``limit`` active occurrences that have no current
    assessment under the selected policy version (never assessed, or
    invalidated by a change of their evidence). The pending set is the
    cursor, so a restart resumes where the rows stop; each write is its
    own transaction and a stale one is counted, left pending and retried
    in a later batch."""
    pending = db.pending_identity_evidence(limit=limit)
    counts: Counter[str] = Counter()
    stale = 0
    for item in pending:
        assessment = assess(item.evidence)
        if db.record_identity_assessment(item, assessment):
            counts[assessment.outcome] += 1
        else:
            stale += 1
    # A short batch took everything that was pending, apart from what
    # was stale; only a full one needs the count.
    remaining = db.count_pending_identity() if len(pending) >= limit else stale
    return BatchResult(counts=dict(counts), stale=stale, remaining=remaining)
