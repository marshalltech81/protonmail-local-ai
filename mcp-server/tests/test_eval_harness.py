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


def _run_summary(capsys: pytest.CaptureFixture[str]) -> str:
    queries = eval_harness._load_queries()
    eval_harness.test_eval_summary(
        _NoResultsDb(),  # type: ignore[arg-type]
        lambda _text: [0.0],
        queries,
    )
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
        ],
        ids=["both-keys", "empty-group", "flat-list", "empty-id", "non-string-id", "not-a-list"],
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
        _FixedResultsDb(["t9", "t1"]),  # type: ignore[arg-type]
        lambda _text: [0.0],
        eval_harness._load_queries(),
    )
    out = capsys.readouterr().out

    # One of two required sources found at rank 2: a full hit, half the evidence.
    assert "Hit@10:          100.00% (1/1)" in out
    assert "MRR:             0.500" in out
    assert "Evidence recall@10: 50.00%" in out
    assert "Recall@10:" not in out.replace("Evidence recall@10:", "")
