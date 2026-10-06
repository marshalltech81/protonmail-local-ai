"""Unit tests for the opt-in retrieval eval harness in ``tests/eval``.

The harness itself only runs against a real index (``MCP_EVAL_DB``);
these tests drive its summary with a stub database so the fallback
notice is pinned in the default suite.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import tests.eval.test_retrieval_eval as eval_harness


class _NoResultsDb:
    def hybrid_search(self, **_kwargs: object) -> list:
        return []


class _FixedResultsDb:
    def __init__(self, thread_ids: list[str]) -> None:
        self._results = [SimpleNamespace(thread_id=t) for t in thread_ids]

    def hybrid_search(self, **_kwargs: object) -> list:
        return self._results


def _write_queries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rows: list[dict]) -> None:
    queries_file = tmp_path / "queries.json"
    queries_file.write_text(json.dumps(rows))
    monkeypatch.setattr(eval_harness, "DEFAULT_QUERY_FILE", queries_file)


class _CountingDb(_FixedResultsDb):
    def __init__(self, thread_ids: list[str]) -> None:
        super().__init__(thread_ids)
        self.searches: list[str] = []

    def hybrid_search(self, **kwargs: object) -> list:
        self.searches.append(str(kwargs["query_text"]))
        return super().hybrid_search(**kwargs)


def _hybrid(db: object, embed=lambda _text: [0.0]) -> eval_harness.HybridResults:
    return eval_harness.HybridResults(db, embed)  # type: ignore[arg-type]


def _run_summary(capsys: pytest.CaptureFixture[str]) -> str:
    queries = eval_harness._load_queries()
    eval_harness.test_eval_summary(_hybrid(_NoResultsDb()), queries)
    return capsys.readouterr().out


def test_summary_flags_fallback_to_example_queries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(eval_harness, "DEFAULT_QUERY_FILE", tmp_path / "queries.json")

    out = _run_summary(capsys)

    assert "queries.example.json" in out
    assert "placeholder" in out


def test_summary_omits_notice_with_operator_queries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    queries_file = tmp_path / "queries.json"
    queries_file.write_text(
        '[{"id": "q1", "search_query": "synthetic", "expected_thread_ids": ["t1"]}]'
    )
    monkeypatch.setattr(eval_harness, "DEFAULT_QUERY_FILE", queries_file)

    out = _run_summary(capsys)

    assert "Hit@10" in out
    assert "queries.example.json" not in out


class TestLoadQueries:
    def test_legacy_expected_ids_become_one_alternative_group(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _write_queries(
            monkeypatch,
            tmp_path,
            [{"id": "q1", "search_query": "s", "expected_thread_ids": ["t1", "t2"]}],
        )

        (q,) = eval_harness._load_queries()

        assert q.evidence_groups == [["t1", "t2"]]

    def test_required_evidence_keeps_groups(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _write_queries(
            monkeypatch,
            tmp_path,
            [{"id": "q1", "search_query": "s", "required_evidence": [["t1", "t2"], ["t3"]]}],
        )

        (q,) = eval_harness._load_queries()

        assert q.evidence_groups == [["t1", "t2"], ["t3"]]

    def test_missing_evidence_is_no_groups(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _write_queries(monkeypatch, tmp_path, [{"id": "q1", "search_query": "s"}])

        (q,) = eval_harness._load_queries()

        assert q.evidence_groups == []

    @pytest.mark.parametrize(
        "row",
        [
            {"expected_thread_ids": ["t1"], "required_evidence": [["t1"]]},
            {"required_evidence": [["t1"], []]},
            {"required_evidence": ["flight-id", "hotel-id"]},
            {"required_evidence": [["t1", ""]]},
            {"required_evidence": [["t1", 7]]},
            {"required_evidence": "t1"},
            {"required_evidence": []},
        ],
        ids=[
            "both-keys",
            "empty-group",
            "flat-list",
            "empty-id",
            "non-string-id",
            "not-a-list",
            "no-groups",
        ],
    )
    def test_ambiguous_or_unsatisfiable_evidence_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, row: dict
    ) -> None:
        _write_queries(monkeypatch, tmp_path, [{"id": "q1", "search_query": "s", **row}])

        with pytest.raises(ValueError, match="q1"):
            eval_harness._load_queries()


def test_summary_separates_hit_rate_from_evidence_recall(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_queries(
        monkeypatch,
        tmp_path,
        [{"id": "q1", "search_query": "s", "required_evidence": [["t1"], ["t2"]]}],
    )

    eval_harness.test_eval_summary(
        _hybrid(_FixedResultsDb(["t9", "t1"])), eval_harness._load_queries()
    )
    out = capsys.readouterr().out

    # One of two required sources found at rank 2: a full hit, half the evidence.
    assert "Hit@10:          100.00% (1/1)" in out
    assert "MRR:             0.500" in out
    assert "Evidence recall@10: 50.00%" in out
    assert "Recall@10:" not in out.replace("Evidence recall@10:", "")


class TestSharedHybridResults:
    """#840: the per-query hybrid test and the summary share each query's
    results within a run, so a query costs one embed and one search."""

    def _rows(self) -> list[dict]:
        return [
            {"id": "q1", "search_query": "first", "expected_thread_ids": ["t1"]},
            {"id": "q2", "search_query": "second", "expected_thread_ids": ["t-missing"]},
            {"id": "q3", "search_query": "no evidence"},
        ]

    def _run(self, monkeypatch, tmp_path, capsys) -> tuple[list[str], _CountingDb, list, str]:
        _write_queries(monkeypatch, tmp_path, self._rows())
        queries = eval_harness._load_queries()
        embedded: list[str] = []

        def embed(text: str) -> list[float]:
            embedded.append(text)
            return [0.0]

        db = _CountingDb(["t9", "t1"])
        hybrid = _hybrid(db, embed)
        failures = []
        # The order pytest runs them: every per-query case, then the summary.
        for q in queries:
            try:
                eval_harness.test_hybrid_search_finds_expected_thread(hybrid, q)
            except AssertionError as e:
                failures.append((q.id, str(e)))
            except pytest.skip.Exception:
                failures.append((q.id, "skipped"))
        eval_harness.test_eval_summary(hybrid, queries)
        return embedded, db, failures, capsys.readouterr().out

    def test_each_query_is_embedded_and_searched_once(self, monkeypatch, tmp_path, capsys):
        embedded, db, _, _ = self._run(monkeypatch, tmp_path, capsys)

        # q3 has no evidence: skipped by both tests, never embedded.
        assert embedded == ["first", "second"]
        assert db.searches == ["first", "second"]

    def test_failures_and_summary_are_unchanged(self, monkeypatch, tmp_path, capsys):
        _, _, failures, out = self._run(monkeypatch, tmp_path, capsys)

        assert [qid for qid, _ in failures] == ["q2", "q3"]
        assert "q2: no thread from groups [['t-missing']]" in failures[0][1]
        assert failures[1][1] == "skipped"
        # The summary still prints after a per-query assertion failed.
        assert "Hit@10:          50.00% (1/2)" in out
        assert "MRR:             0.250" in out
        assert "Evidence recall@10: 50.00%" in out
        assert "q1                             rank=2 groups=1/1" in out
        assert "q2                             rank=None groups=0/1" in out

    def test_summary_alone_searches_each_query_once(self, monkeypatch, tmp_path, capsys):
        """Run with ``-k summary``: the summary does the work itself."""
        _write_queries(monkeypatch, tmp_path, self._rows())
        db = _CountingDb(["t1"])
        eval_harness.test_eval_summary(_hybrid(db), eval_harness._load_queries())

        assert db.searches == ["first", "second"]
        assert "Hit@10:          50.00% (1/2)" in capsys.readouterr().out

    @pytest.mark.parametrize("fails", ["embed", "search"])
    def test_a_failed_attempt_is_replayed_without_another_call(self, fails):
        """Codex review round 1 on #866: a failed embed or search was
        retried by the summary, a second provider call for the query."""
        q = eval_harness.EvalQuery(id="q1", search_query="first", evidence_groups=[["t1"]])
        calls: list[str] = []

        class _Failing:
            def hybrid_search(self, **_kwargs: object) -> list:
                calls.append("search")
                raise RuntimeError("search failed")

        def embed(_text: str) -> list[float]:
            calls.append("embed")
            if fails == "embed":
                raise RuntimeError("embed failed")
            return [0.0]

        hybrid = _hybrid(_Failing(), embed)
        for _ in range(2):  # the per-query test, then the summary
            with pytest.raises(RuntimeError, match=f"{fails} failed"):
                hybrid(q)

        assert calls == (["embed"] if fails == "embed" else ["embed", "search"])

    def test_results_are_not_shared_between_runs(self):
        """Each run builds its own ``HybridResults``; nothing persists."""
        q = eval_harness.EvalQuery(id="q1", search_query="first", evidence_groups=[["t1"]])
        db = _CountingDb(["t1"])
        _hybrid(db)(q)
        _hybrid(db)(q)

        assert db.searches == ["first", "first"]
