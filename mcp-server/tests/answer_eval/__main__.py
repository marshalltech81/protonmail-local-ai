"""Command line: ``python -m tests.answer_eval run|compare`` (from ``mcp-server/``).

``make eval-answers`` builds the synthetic index and calls ``run``;
``make eval-answers-compare`` calls ``compare``. Exit codes:

- ``run``: 0 every case completed (and was judged, with a judge), 2 the
  evaluation is incomplete (errors, timeouts, skipped cases, judge
  errors), 3 configuration or contract error before any provider call.
- ``compare``: 0 compared, 1 a per-case regression with
  ``--fail-on-regression``, 2 the runs are not comparable (different
  cases, index or judge/rubric) without ``--allow-incompatible``.

Quality is advisory until thresholds are calibrated: ``run`` never
fails on a low score.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.lib.inference import PromptBudget
from src.lib.sqlite import Database

from tests.answer_eval.cases import CASES_PATH, CASES_SCHEMA_VERSION, CaseError, load_cases
from tests.answer_eval.config import ConfigError, load_layer
from tests.answer_eval.harness import evaluate
from tests.answer_eval.judge import RUBRIC_VERSION
from tests.answer_eval.report import (
    REPORT_SCHEMA_VERSION,
    build_report,
    compare_reports,
    is_incomplete,
    render_comparison,
    render_summary,
    write_private_json,
)
from tests.answer_eval.runner import (
    NonSyntheticIndexError,
    PrecomputedEmbedder,
    RunContext,
    index_identity,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
# The one place inside the repository a report may go (git-ignored).
REPORT_DIR = REPO_ROOT / ".answer-eval"

EXIT_OK, EXIT_REGRESSION, EXIT_INCOMPLETE, EXIT_CONFIG = 0, 1, 2, 3


def _check_output_path(path: Path) -> Path:
    resolved = path.resolve()
    inside_repo = REPO_ROOT == resolved or REPO_ROOT in resolved.parents
    if inside_repo and REPORT_DIR not in resolved.parents:
        raise ConfigError(
            "reports go outside the repository or under its git-ignored .answer-eval/ directory"
        )
    return resolved


def _run(args: argparse.Namespace) -> int:
    out = _check_output_path(args.out)
    detail = _check_output_path(args.detail) if args.detail else None
    if detail == out:
        raise ConfigError("--detail must be a different file from --out")
    cases = load_cases(args.cases)
    if args.case:
        unknown = set(args.case) - {c.id for c in cases}
        if unknown:
            raise CaseError(f"unknown case ids: {sorted(unknown)}")
        cases = [c for c in cases if c.id in set(args.case)]

    # Check the build's outputs exist before opening anything, so a
    # mistyped directory is a configuration error, not a new empty file.
    for name in ("mail.db", "query_vectors.json"):
        if not (args.index_dir / name).is_file():
            raise ConfigError(f"--index-dir has no {name}; build it with tests.baseline.build")
    db = Database(str(args.index_dir / "mail.db"))
    index = index_identity(db)  # refuses anything but the synthetic corpus
    vectors = json.loads((args.index_dir / "query_vectors.json").read_text(encoding="utf-8"))
    if any(c.question not in vectors for c in cases):
        raise CaseError("some case questions have no query vector; rebuild the index with cases")

    answerer = load_layer("INFERENCE", os.environ, args.secrets_dir)
    judge = load_layer("JUDGE", os.environ, args.secrets_dir)
    assert answerer is not None  # load_layer raises when INFERENCE_MODE=none
    print(
        "Answer evaluation on the synthetic corpus: case questions and retrieved synthetic "
        f"evidence go to the answering model ({answerer.mode}, {answerer.endpoint_kind()})"
        + (
            f"; questions, evidence and answers go to the judge ({judge.mode}, "
            f"{judge.endpoint_kind()})."
            if judge
            else "; no judge configured (JUDGE_MODE=none), deterministic checks only."
        ),
        file=sys.stderr,
    )

    ctx = RunContext(
        db=db,
        embed_client=PrecomputedEmbedder(vectors),
        inference_client=answerer.client(),
        prompt_budget=PromptBudget(
            context_tokens=answerer.context_tokens, max_output_tokens=answerer.max_tokens
        ),
        expected_embed_dim=db.get_embedding_dim(),
        secret_values=[k for k in (answerer.api_key, judge.api_key if judge else "") if k],
        case_timeout_secs=args.case_timeout_secs,
    )
    started = datetime.now(UTC).isoformat(timespec="seconds")
    records, details = asyncio.run(
        evaluate(
            cases,
            ctx,
            judge_client=judge.client() if judge else None,
            judge_config=judge,
            max_runtime_secs=args.max_runtime_secs,
        )
    )
    identity: dict[str, Any] = {
        "source_commit": args.source_commit,
        "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "cases_schema_version": CASES_SCHEMA_VERSION,
        "rubric_version": RUBRIC_VERSION,
        **index,
        "answerer": answerer.label(),
        "judge": judge.label() if judge else None,
        "retrieval": {"embedder": "hashed-baseline (precomputed)", "reranker": "none"},
        "settings": {
            "case_timeout_secs": args.case_timeout_secs,
            "max_runtime_secs": args.max_runtime_secs,
            "selected_cases": len(cases),
            "repetitions": 1,
        },
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    report = build_report(identity, records, judge is not None)
    write_private_json(out, report)
    if detail:
        write_private_json(
            detail,
            {
                "schema_version": REPORT_SCHEMA_VERSION,
                "kind": "answer_eval_detail",
                "identity": identity,
                "cases": details,
            },
        )
    print(render_summary(report))
    print(f"Report: {out}" + (f"\nDetail: {detail}" if detail else ""))
    return EXIT_INCOMPLETE if is_incomplete(report) else EXIT_OK


def _load_report(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or data.get("kind") != "answer_eval_run"
        or data.get("schema_version") != REPORT_SCHEMA_VERSION
    ):
        raise ConfigError(f"not an answer evaluation report (schema v{REPORT_SCHEMA_VERSION})")
    return data


def _compare(args: argparse.Namespace) -> int:
    cmp = compare_reports(_load_report(args.baseline), _load_report(args.candidate))
    print(render_comparison(cmp))
    if args.out:
        write_private_json(_check_output_path(args.out), cmp)
    if cmp["incompatible"] and not args.allow_incompatible:
        return EXIT_INCOMPLETE
    if cmp["regressions"] and args.fail_on_regression:
        return EXIT_REGRESSION
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.answer_eval")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="answer and grade every case against a synthetic index")
    run.add_argument("--index-dir", type=Path, required=True, help="tests.baseline.build output")
    run.add_argument("--out", type=Path, required=True, help="report path (mode 600)")
    run.add_argument("--detail", type=Path, help="opt-in content-bearing detail artifact")
    run.add_argument("--cases", type=Path, default=CASES_PATH)
    run.add_argument("--case", action="append", help="run only this case id (repeatable)")
    run.add_argument("--secrets-dir", type=Path, default=REPO_ROOT / ".secrets")
    run.add_argument("--source-commit", default=None)
    run.add_argument("--case-timeout-secs", type=float, default=900.0)
    run.add_argument("--max-runtime-secs", type=float, default=3600.0)
    cmp = sub.add_parser("compare", help="compare two run reports")
    cmp.add_argument("baseline", type=Path)
    cmp.add_argument("candidate", type=Path)
    cmp.add_argument("--out", type=Path, help="also write the comparison as JSON")
    cmp.add_argument("--fail-on-regression", action="store_true")
    cmp.add_argument("--allow-incompatible", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        return _run(args) if args.command == "run" else _compare(args)
    except (CaseError, ConfigError, NonSyntheticIndexError) as e:
        print(f"answer evaluation: {e}", file=sys.stderr)
        return EXIT_CONFIG
    except (OSError, json.JSONDecodeError, sqlite3.Error, KeyError, TypeError) as e:
        # Unreadable or malformed case, report or index files. The type
        # only: these messages can quote file contents.
        print(f"answer evaluation: unreadable input ({type(e).__name__})", file=sys.stderr)
        return EXIT_CONFIG


if __name__ == "__main__":
    sys.exit(main())
