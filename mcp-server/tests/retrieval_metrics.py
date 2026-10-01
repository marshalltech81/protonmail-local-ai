"""Retrieval metrics shared by the baseline and the opt-in eval harness.

A question's expected evidence is a list of groups. Every group is
required (a question that needs two sources has two groups); the thread
ids inside one group are alternatives, any one of which satisfies it
(the same fact in two threads). A single expected thread is one group
with one member.

Two kinds of score come from that shape and must not be confused:

- **Hit rate and MRR** ask whether anything relevant surfaced and how
  high: the first thread from any group sets the rank.
- **Evidence recall@k** asks whether the top k hold everything the
  question needs: the fraction of groups with a member in the top k.

For a one-group question they agree; for a multi-source question a hit
can still leave half the evidence missing.
"""

from __future__ import annotations

from collections.abc import Sequence

EvidenceGroups = Sequence[Sequence[str]]


def group_ranks(ranked: Sequence[str], groups: EvidenceGroups) -> list[int | None]:
    """1-based best rank of each group in ``ranked``, or ``None`` if absent."""
    position: dict[str, int] = {}
    for i, thread_id in enumerate(ranked, start=1):
        position.setdefault(thread_id, i)
    out: list[int | None] = []
    for group in groups:
        ranks = [position[t] for t in group if t in position]
        out.append(min(ranks) if ranks else None)
    return out


def first_hit_rank(ranked: Sequence[str], groups: EvidenceGroups) -> int | None:
    """1-based rank of the first thread from any group, or ``None``."""
    ranks = [r for r in group_ranks(ranked, groups) if r is not None]
    return min(ranks) if ranks else None


def evidence_recall(ranked: Sequence[str], groups: EvidenceGroups, k: int) -> float:
    """Fraction of required groups with a member in the top ``k``."""
    if not groups:
        raise ValueError("evidence recall needs at least one evidence group")
    satisfied = sum(1 for r in group_ranks(ranked, groups) if r is not None and r <= k)
    return satisfied / len(groups)
