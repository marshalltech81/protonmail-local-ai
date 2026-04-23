"""
Tests for src/timings.py.

Covers the pure percentile math, ``StageTimings`` arithmetic, the
ring-buffer aggregator's window/reset/snapshot semantics, and the
log-line formatter. Concurrency is exercised with a small thread pool
since the aggregator is fed from both the watchdog thread and the main
drain loop in production.
"""

import threading

import pytest
from src.timings import (
    STAGES,
    StageTimings,
    TimingAggregator,
    format_summary,
    percentile,
)


class TestStageTimings:
    def test_default_construction_is_all_zeros(self):
        t = StageTimings()
        assert t.parse_ms == 0.0
        assert t.thread_ms == 0.0
        assert t.embed_ms == 0.0
        assert t.db_write_ms == 0.0
        assert t.total_ms == 0.0

    def test_total_sums_all_stages(self):
        t = StageTimings(parse_ms=1.0, thread_ms=2.0, embed_ms=3.0, db_write_ms=4.0)
        assert t.total_ms == 10.0

    def test_is_frozen(self):
        t = StageTimings()
        with pytest.raises(Exception):
            t.parse_ms = 5.0  # type: ignore[misc]


class TestPercentile:
    def test_empty_returns_zero(self):
        assert percentile([], 0.5) == 0.0

    def test_single_value_returns_that_value_at_any_p(self):
        assert percentile([7.0], 0.0) == 7.0
        assert percentile([7.0], 0.5) == 7.0
        assert percentile([7.0], 1.0) == 7.0

    def test_p50_of_sorted_odd_length(self):
        assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0

    def test_p50_of_sorted_even_length_interpolates(self):
        # rank = 0.5 * (4 - 1) = 1.5 → midway between sorted[1]=2 and [2]=3.
        assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5

    def test_p0_returns_min(self):
        assert percentile([5.0, 1.0, 3.0], 0.0) == 1.0

    def test_p100_returns_max(self):
        assert percentile([5.0, 1.0, 3.0], 1.0) == 5.0

    def test_does_not_mutate_input(self):
        values = [3.0, 1.0, 2.0]
        percentile(values, 0.5)
        assert values == [3.0, 1.0, 2.0]

    def test_invalid_p_raises(self):
        with pytest.raises(ValueError):
            percentile([1.0, 2.0], -0.1)
        with pytest.raises(ValueError):
            percentile([1.0, 2.0], 1.1)


class TestTimingAggregator:
    def test_starts_empty(self):
        agg = TimingAggregator(window=10)
        assert len(agg) == 0
        assert agg.summary() == {}

    def test_invalid_window_raises(self):
        with pytest.raises(ValueError):
            TimingAggregator(window=0)
        with pytest.raises(ValueError):
            TimingAggregator(window=-5)

    def test_record_grows_until_window(self):
        agg = TimingAggregator(window=3)
        for i in range(2):
            agg.record(StageTimings(parse_ms=float(i)))
        assert len(agg) == 2

    def test_window_drops_oldest_entries(self):
        agg = TimingAggregator(window=3)
        for i in range(10):
            agg.record(StageTimings(parse_ms=float(i)))
        assert len(agg) == 3
        # Only the last three (parse=7, 8, 9) should contribute to the
        # summary — older entries must have been evicted by the deque.
        summary = agg.summary()
        assert summary["parse"]["max"] == 9.0
        assert summary["parse"]["p50"] == 8.0

    def test_reset_clears_buffer(self):
        agg = TimingAggregator(window=10)
        for i in range(5):
            agg.record(StageTimings(parse_ms=float(i)))
        agg.reset()
        assert len(agg) == 0
        assert agg.summary() == {}

    def test_summary_includes_all_stages_and_total(self):
        agg = TimingAggregator(window=10)
        for i in range(1, 6):
            agg.record(
                StageTimings(
                    parse_ms=float(i),
                    thread_ms=float(i) * 2,
                    embed_ms=float(i) * 10,
                    db_write_ms=float(i) * 0.5,
                )
            )
        summary = agg.summary()
        for stage in STAGES:
            assert stage in summary
            assert {"p50", "p95", "max"} <= summary[stage].keys()
        assert "total" in summary
        # Embed should dominate the totals because its multiplier is largest.
        assert summary["embed"]["max"] == 50.0

    def test_summary_meta_sample_count_matches_len(self):
        agg = TimingAggregator(window=100)
        for i in range(7):
            agg.record(StageTimings(parse_ms=float(i)))
        summary = agg.summary()
        assert summary["_meta"]["sample_count"] == 7.0

    def test_concurrent_record_does_not_lose_entries(self):
        # The watchdog thread and the main drain loop both write into the
        # same aggregator; verify the lock is doing its job.
        agg = TimingAggregator(window=1000)

        def writer():
            for _ in range(100):
                agg.record(StageTimings(parse_ms=1.0))

        threads = [threading.Thread(target=writer) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(agg) == 500


class TestFormatSummary:
    def test_empty_summary_returns_empty_string(self):
        assert format_summary({}) == ""

    def test_format_includes_sample_count_and_all_stages(self):
        agg = TimingAggregator(window=10)
        for i in range(1, 4):
            agg.record(
                StageTimings(
                    parse_ms=float(i),
                    thread_ms=float(i),
                    embed_ms=float(i),
                    db_write_ms=float(i),
                )
            )
        line = format_summary(agg.summary())
        assert "n=3" in line
        for stage in (*STAGES, "total"):
            assert stage in line
        assert "p50" in line
        assert "p95" in line
        assert "max" in line

    def test_format_uses_one_decimal_place(self):
        agg = TimingAggregator(window=10)
        agg.record(StageTimings(parse_ms=1.234, thread_ms=0.0, embed_ms=0.0, db_write_ms=0.0))
        line = format_summary(agg.summary())
        # p50 of one sample is the value itself; format should round to .1f
        assert "1.2" in line
