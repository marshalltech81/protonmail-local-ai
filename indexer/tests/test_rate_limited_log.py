"""The indexer's shared rate-limited logger (#889).

``warn_rate_limited`` gives every repeated indexer line one budget of
``_WARNINGS_PER_WINDOW`` lines per ``_WARNING_WINDOW_SECS``. A withheld
attachment line is counted in the attachments aggregate's
``warnings_suppressed``; any other withheld line in the queue
heartbeat's ``suppressed_lines``. The tests in ``TestWarnRateLimited``
were written against main before the limiter moved into
``src.rate_limited_log``, to pin its behaviour across the move.
"""

from __future__ import annotations

import logging
import threading

import pytest
from src import extractors, rate_limited_log
from src.rate_limited_log import LineBudget

MARKER = "SYNTHETIC_MAIL_MARKER"
log = logging.getLogger("indexer.test.rate_limited")


@pytest.fixture(autouse=True)
def _drained():
    """Start from zero suppressed counts (the window is reset by conftest)."""
    extractors.drain_extractor_counts()
    extractors.drain_suppressed_lines()


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(rate_limited_log.time, "monotonic", lambda: now["t"])
    return now


def _lines(caplog, text):
    return [r for r in caplog.records if r.name == log.name and text in r.getMessage()]


class TestWarnRateLimited:
    def test_returns_whether_logged_and_keeps_the_level(self, caplog):
        caplog.set_level(logging.DEBUG)
        limit = extractors._WARNINGS_PER_WINDOW
        logged = [
            extractors.warn_rate_limited(
                log, "synthetic line %d", i, level=logging.INFO, attachment=False
            )
            for i in range(limit + 3)
        ]
        assert logged == [True] * limit + [False] * 3
        records = _lines(caplog, "synthetic line")
        assert len(records) == limit
        assert {r.levelno for r in records} == {logging.INFO}
        assert extractors.drain_suppressed_lines() == 3
        assert extractors.drain_extractor_counts()["warnings_suppressed"] == 0

    def test_default_is_a_counted_attachment_warning(self, caplog):
        caplog.set_level(logging.DEBUG)
        for _ in range(extractors._WARNINGS_PER_WINDOW + 2):
            extractors.warn_rate_limited(log, "synthetic attachment line")
        records = _lines(caplog, "synthetic attachment line")
        assert {r.levelno for r in records} == {logging.WARNING}
        assert extractors.drain_extractor_counts()["warnings_suppressed"] == 2
        assert extractors.drain_suppressed_lines() == 0

    def test_both_kinds_share_one_budget(self, caplog):
        caplog.set_level(logging.DEBUG)
        for _ in range(extractors._WARNINGS_PER_WINDOW - 1):
            assert extractors.warn_rate_limited(log, "attachment")
        assert extractors.warn_rate_limited(log, "other", attachment=False)
        assert not extractors.warn_rate_limited(log, "other", attachment=False)
        assert not extractors.warn_rate_limited(log, "attachment")
        assert extractors.drain_suppressed_lines() == 1
        assert extractors.drain_extractor_counts()["warnings_suppressed"] == 1

    def test_a_new_window_restores_the_budget(self, caplog, clock):
        caplog.set_level(logging.DEBUG)
        limit = extractors._WARNINGS_PER_WINDOW
        for _ in range(limit + 1):
            extractors.warn_rate_limited(log, "synthetic line", attachment=False)
        clock["t"] += extractors._WARNING_WINDOW_SECS - 1
        assert not extractors.warn_rate_limited(log, "synthetic line", attachment=False)
        clock["t"] += 1
        assert extractors.warn_rate_limited(log, "synthetic line", attachment=False)
        assert len(_lines(caplog, "synthetic line")) == limit + 1
        # Withheld counts survive the window change until drained.
        assert extractors.drain_suppressed_lines() == 2
        assert extractors.drain_suppressed_lines() == 0

    @pytest.mark.parametrize("attachment", [True, False])
    def test_totals_are_exact_across_threads(self, caplog, attachment):
        caplog.set_level(logging.DEBUG)
        threads, per_thread = 8, 200
        logged: list[bool] = []
        lock = threading.Lock()

        def hammer():
            mine = [
                extractors.warn_rate_limited(log, "synthetic threaded line", attachment=attachment)
                for _ in range(per_thread)
            ]
            with lock:
                logged.extend(mine)

        workers = [threading.Thread(target=hammer) for _ in range(threads)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        limit = extractors._WARNINGS_PER_WINDOW
        assert logged.count(True) == limit
        assert len(_lines(caplog, "synthetic threaded line")) == limit
        withheld = threads * per_thread - limit
        suppressed = extractors.drain_extractor_counts()["warnings_suppressed"]
        lines = extractors.drain_suppressed_lines()
        assert (suppressed, lines) == ((withheld, 0) if attachment else (0, withheld))


class TestLineBudget:
    def test_unknown_bucket_is_rejected_without_its_value(self):
        budget = LineBudget(limit=1, window_secs=60.0, buckets=("a",))
        with pytest.raises(ValueError) as info:
            budget.log(log, MARKER, "msg")
        assert MARKER not in str(info.value)
        with pytest.raises(ValueError) as info:
            budget.drain(MARKER)
        assert MARKER not in str(info.value)

    def test_state_is_one_counter_per_fixed_bucket(self, caplog):
        caplog.set_level(logging.DEBUG)
        budget = LineBudget(limit=1, window_secs=60.0, buckets=("a", "b"))
        for _ in range(1000):
            budget.log(log, "a", "synthetic line")
        assert set(budget._suppressed) == {"a", "b"}
        assert budget.drain("a") == 999
        assert budget.drain("b") == 0


class TestPdfOcrFallbackLine:
    def test_ocr_failures_share_the_limit(self, monkeypatch, caplog):
        """#889: the OCR fallback line logged once per scanned PDF whose
        OCR raised (Tesseract timing out on every page, for example),
        bypassing the limit. It is an attachment line now."""
        from src.extractors import extract, pdf

        caplog.set_level(logging.DEBUG)
        # A mixed PDF: enough digital text to keep, one scanned page.
        monkeypatch.setattr(pdf, "_extract_digital_pages", lambda payload, **_: ["d" * 100, ""])

        def fail_ocr(payload, **_):
            raise RuntimeError(MARKER)

        monkeypatch.setattr(pdf, "_extract_ocr", fail_ocr)
        limit = extractors._WARNINGS_PER_WINDOW
        for i in range(limit + 4):
            result = extract(content_type="application/pdf", filename="x.pdf", payload=b"%d" % i)
            assert result.extractor.startswith("pdf-digital")
        records = [r for r in caplog.records if "PDF OCR fallback failed" in r.getMessage()]
        assert len(records) == limit
        assert {r.levelno for r in records} == {logging.WARNING}
        assert extractors.drain_extractor_counts()["warnings_suppressed"] == 4
        assert extractors.drain_suppressed_lines() == 0
        assert MARKER not in caplog.text
