"""
Per-stage timing for the indexing hot path.

The indexer's `_index_one_file` runs four serial stages (parse, thread,
embed, db_write) but until now exposed no per-stage cost. An operator
investigating "why is indexing slow?" had no way to distinguish a slow
embed call from a slow SQLite write. The aggregator collects timings in
a bounded ring buffer and emits p50/p95 summaries on demand so the
overhead stays trivial regardless of mailbox size.

The aggregator is intentionally tiny — a deque, a copy-and-sort
percentile, and a dict — so it can run on the indexing hot path without
allocating per-message dicts or pulling in numpy/scipy. Anything fancier
(prometheus exporter, histogram buckets) belongs in a follow-up once the
basic numbers are in front of an operator.
"""

from collections import deque
from dataclasses import dataclass
from threading import Lock

# Stages match the four try/except blocks in ``_index_one_file``. Adding
# a new stage there means adding it here too — the field set is the
# contract that ties the timer at the call site to the aggregator.
STAGES: tuple[str, ...] = ("parse", "thread", "embed", "db_write")


@dataclass(frozen=True)
class StageTimings:
    """Per-stage wall-clock costs for one ``_index_one_file`` invocation.

    Stages that did not run (because an earlier stage failed and the
    function returned early) report ``0.0`` rather than ``None`` so the
    aggregator math stays branch-free. Callers that want to distinguish
    "ran but was fast" from "did not run" should also look at the
    ``stage`` returned from ``_index_one_file``.
    """

    parse_ms: float = 0.0
    thread_ms: float = 0.0
    embed_ms: float = 0.0
    db_write_ms: float = 0.0

    @property
    def total_ms(self) -> float:
        return self.parse_ms + self.thread_ms + self.embed_ms + self.db_write_ms


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolation percentile over a copy of ``values``.

    ``p`` is in [0.0, 1.0]. Empty input returns 0.0. The implementation
    sorts a copy so the caller's deque/list is not mutated. Linear
    interpolation matches numpy's default ``method='linear'`` so the
    numbers reported here line up with what an operator would compute
    from a CSV dump of the same data.
    """
    if not values:
        return 0.0
    if not (0.0 <= p <= 1.0):
        raise ValueError(f"p must be in [0, 1], got {p}")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = p * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


class TimingAggregator:
    """Bounded ring of recent ``StageTimings`` with on-demand p50/p95.

    Thread-safe so the watchdog callback thread and the main drain loop
    can both feed the same aggregator without coordinating. The lock is
    only held for the deque mutation; percentile computation works on a
    snapshot copy.
    """

    def __init__(self, window: int = 200):
        if window <= 0:
            raise ValueError(f"window must be > 0, got {window}")
        self._window = window
        self._timings: deque[StageTimings] = deque(maxlen=window)
        self._lock = Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._timings)

    def record(self, timings: StageTimings) -> None:
        with self._lock:
            self._timings.append(timings)

    def reset(self) -> None:
        with self._lock:
            self._timings.clear()

    def summary(self) -> dict[str, dict[str, float]]:
        """Return ``{stage: {p50, p95, max}}`` plus ``{'total': ...}``.

        Returns an empty dict when no timings have been recorded so the
        caller can cheaply decide whether to emit a log line at all.
        """
        with self._lock:
            snapshot = list(self._timings)
        if not snapshot:
            return {}

        out: dict[str, dict[str, float]] = {}
        for stage in STAGES:
            values = [getattr(t, f"{stage}_ms") for t in snapshot]
            out[stage] = {
                "p50": percentile(values, 0.50),
                "p95": percentile(values, 0.95),
                "max": max(values),
            }
        totals = [t.total_ms for t in snapshot]
        out["total"] = {
            "p50": percentile(totals, 0.50),
            "p95": percentile(totals, 0.95),
            "max": max(totals),
        }
        out["_meta"] = {"sample_count": float(len(snapshot))}
        return out


def format_summary(summary: dict[str, dict[str, float]]) -> str:
    """Render a one-line, log-friendly summary string.

    Returns an empty string when ``summary`` is empty, so the caller can
    use ``if line: log.info(line)`` without an extra branch.
    """
    if not summary:
        return ""
    sample_count = int(summary.get("_meta", {}).get("sample_count", 0))
    parts = [f"indexed n={sample_count}"]
    for stage in (*STAGES, "total"):
        s = summary[stage]
        parts.append(f"{stage}=p50:{s['p50']:.1f}/p95:{s['p95']:.1f}/max:{s['max']:.1f}ms")
    return " ".join(parts)
