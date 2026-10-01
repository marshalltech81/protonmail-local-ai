"""Unit tests for the opt-in retrieval eval harness in ``tests/eval``.

The harness itself only runs against a real index (``MCP_EVAL_DB``);
these tests drive its summary with a stub database so the fallback
notice is pinned in the default suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tests.eval.test_retrieval_eval as eval_harness


class _NoResultsDb:
    def hybrid_search(self, **_kwargs: object) -> list:
        return []


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

    assert "Recall@10" in out
    assert "queries.example.json" not in out
