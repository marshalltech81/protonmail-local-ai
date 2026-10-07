"""Versioned run reports and run-to-run comparison.

The default report holds only opaque case IDs, categories, fixed
diagnostic categories, counts, rates, timings and safe configuration
labels: no question, answer, passage, claim or judge explanation. Those
go only to the optional detail artifact. Both are written mode 600 in a
mode 700 directory.

Every rate's denominator is the cases selected for the run: a case that
errored, timed out or was skipped counts as not passing, so failures can
never improve a score.
"""

import json
import os
import tempfile
from pathlib import Path
from statistics import fmean
from typing import Any

from tests.answer_eval.cases import DIMENSIONS, Case
from tests.answer_eval.graders import DeterministicResult, budget_omitted_facts
from tests.answer_eval.judge import CLAIM_VERDICTS, JudgeOutcome
from tests.answer_eval.runner import CaseRun

REPORT_SCHEMA_VERSION = 1
# Identity fields that must match for two runs' grades to be comparable.
_COMPARABLE = ("cases_sha256", "index_sha256", "rubric_version")
# Run settings (``identity.settings``) that bound how long cases may
# take. A case that times out or is skipped counts as failing, so a
# changed timeout can alter every rate: each one is both reported as
# changed and makes the runs incompatible (#997).
TIMEOUT_SETTINGS = ("case_timeout_secs", "max_runtime_secs")


def case_record(
    case: Case,
    run: CaseRun,
    det: DeterministicResult,
    judge: JudgeOutcome,
    causes: list[str],
) -> dict[str, Any]:
    grade = judge.grade
    return {
        "id": case.id,
        "category": case.category,
        "tool": case.tool,
        "held_out": case.held_out,
        "answerable": case.answerable,
        "required_groups": len(case.required_evidence),
        "facts_expected": len(case.expected_facts),
        "applicable_dimensions": [d for d in DIMENSIONS if case.criteria[d]],
        "review": case.review,
        "status": run.status,
        "error": run.error,
        "deterministic": {
            "passed": det.passed,
            "checks": det.checks,
            "abstained": det.abstained,
            "citation_problems": det.citation_problem_kinds,
            "evidence_groups": len(det.groups),
            "retrieval_recall": det.retrieval_recall,
            "prompt_coverage": det.prompt_coverage,
            "citation_coverage": det.citation_coverage,
        },
        "judge": {
            "status": judge.status,
            "error": judge.error,
            "detail": judge.detail,
            "groundedness_pass": grade.groundedness_pass if grade else None,
            "correctness_pass": grade.correctness_pass if grade else None,
            "claims": grade.claims if grade else None,
            "facts_covered": grade.facts_covered if grade else None,
            "facts_total": grade.facts_total if grade else None,
            "prohibited_asserted": grade.prohibited_asserted if grade else None,
            "dimensions": grade.dimensions if grade else None,
            "prompt_chars": judge.prompt_chars,
        },
        "attribution": causes,
        "inference_calls": len(run.calls),
        "repair_attempted": run.view.repair_attempted if run.output else None,
        "passages_supplied": len(run.passages),
        "timings_ms": {**run.timings_ms, "judge": judge.ms},
        # The inference client returns text only, so token usage is not
        # exposed; cost needs a dated price table and is not computed.
        "usage": {"answer": "unavailable", "judge": "unavailable"},
    }


def detail_record(case: Case, run: CaseRun, judge: JudgeOutcome) -> dict[str, Any]:
    """Content-bearing record for the opted-in detail artifact only."""
    verdict = judge.verdict
    view = run.view if run.output else None
    return {
        "id": case.id,
        "tool": case.tool,
        "question": case.question,
        "answer": view.answer if view else None,
        # extract_from_emails: the records themselves (provider output).
        "records": view.records if view else None,
        "tool_error": run.error_detail,
        # What the judge was allowed to excuse (review round 9).
        "coverage_note": view.coverage_note if view else None,
        "omitted_facts": budget_omitted_facts(case, run),
        "retrieved_threads": [t.thread_id for t in view.threads] if view else [],
        "passages": {
            label: {
                "thread_id": p.thread_id,
                "message_id": p.message_id,
                "claimant_id": p.claimant_id,
                "chunk_id": p.chunk_id,
                "source": p.source,
                "text": p.text,
                "truncated": p.truncated,
            }
            for label, p in run.passages.items()
        },
        "inference_requests": [
            {"user": c.user, "outcome": c.outcome, "response": c.response, "ms": c.ms}
            for c in run.calls
        ],
        "judge_claims": [
            {"claim": c.claim, "statement": c.statement, "cited": c.cited, "verdict": c.verdict}
            for c in verdict.claims
        ]
        if verdict
        else None,
        "judge_explanations": verdict.explanations if verdict else None,
    }


def _mean(values: list[float]) -> float | None:
    return round(fmean(values), 4) if values else None


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _coverage(records: list[dict[str, Any]], stage: str) -> float | None:
    """Mean evidence coverage over every case that needs evidence; a case
    that did not complete counts as 0, so a failure cannot raise it."""
    values = [
        (r["deterministic"][stage] or 0.0) if r["status"] == "ok" else 0.0
        for r in records
        if r["required_groups"]
    ]
    return _mean(values)


def aggregate(records: list[dict[str, Any]], judge_configured: bool) -> dict[str, Any]:
    n = len(records)
    done = [r for r in records if r["status"] == "ok"]
    det = [r["deterministic"] for r in done]
    out: dict[str, Any] = {
        "cases": n,
        "completed": len(done),
        "deterministic_pass_rate": _rate(sum(d["passed"] for d in det), n),
        "retrieval_evidence_recall": _coverage(records, "retrieval_recall"),
        "prompt_evidence_coverage": _coverage(records, "prompt_coverage"),
        "citation_evidence_coverage": _coverage(records, "citation_coverage"),
        "citation_failure_cases": sum(bool(d["citation_problems"]) for d in det),
        "abstention": {
            "expected": sum(not r["answerable"] for r in records),
            "correct": sum(
                not r["answerable"] and r["deterministic"]["checks"].get("abstention") == "pass"
                for r in done
            ),
            "wrongly_abstained": sum(
                d["abstained"] and d["checks"].get("abstention") == "fail" for d in det
            ),
            "missed_abstention": sum(
                not d["abstained"] and d["checks"].get("abstention") == "fail" for d in det
            ),
        },
        "answer_ms_mean": _mean([r["timings_ms"]["answer_total"] for r in done]),
    }
    if not judge_configured:
        out["judge"] = None
        return out
    judged = [r["judge"] for r in records if r["judge"]["status"] == "ok"]
    unjudged = [r for r in records if r["judge"]["status"] != "ok"]
    claims = {v: sum(j["claims"][v] for j in judged) for v in CLAIM_VERDICTS}
    total_claims = sum(claims.values())
    # A case the judge did not assess (judge error, failed answer, skip)
    # keeps its expected facts and applicable dimensions in the
    # denominators, as missing and failed: an error never raises a rate.
    facts_total = sum(j["facts_total"] for j in judged) + sum(r["facts_expected"] for r in unjudged)
    dims: dict[str, float | None] = {}
    for d in DIMENSIONS:
        results = [j["dimensions"][d] for j in judged if j["dimensions"][d] != "not_applicable"]
        results += ["fail" for r in unjudged if d in r["applicable_dimensions"]]
        dims[d] = _rate(results.count("pass"), len(results))
    out["judge"] = {
        "completed": len(judged),
        "errors": sum(r["judge"]["status"] == "error" for r in records),
        "groundedness_pass_rate": _rate(sum(j["groundedness_pass"] for j in judged), n),
        "correctness_pass_rate": _rate(sum(j["correctness_pass"] for j in judged), n),
        "unsupported_claim_rate": _rate(
            claims["contradicted"] + claims["insufficient_evidence"], total_claims
        ),
        "missing_fact_rate": _rate(
            facts_total - sum(j["facts_covered"] for j in judged), facts_total
        ),
        "prohibited_assertions": sum(j["prohibited_asserted"] for j in judged),
        "dimension_pass_rates": dims,
        "judge_ms_mean": _mean(
            [r["timings_ms"]["judge"] for r in records if r["judge"]["status"] == "ok"]
        ),
    }
    return out


def counts(records: list[dict[str, Any]], judge_configured: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "selected": len(records),
        "attempted": sum(r["status"] != "skipped" for r in records),
        "completed": sum(r["status"] == "ok" for r in records),
        "skipped": sum(r["status"] == "skipped" for r in records),
        "errors_by_status": {},
    }
    out["errors"] = out["attempted"] - out["completed"]
    for r in records:
        if r["status"] not in ("ok", "skipped"):
            out["errors_by_status"][r["status"]] = out["errors_by_status"].get(r["status"], 0) + 1
    if judge_configured:
        errors: dict[str, int] = {}
        for r in records:
            if r["judge"]["status"] == "error":
                errors[r["judge"]["error"]] = errors.get(r["judge"]["error"], 0) + 1
        out["judge_errors_by_category"] = errors
    return out


def is_incomplete(report: dict[str, Any]) -> bool:
    """True when any case did not complete or (with a judge) was not judged."""
    c = report["counts"]
    judge_errors = sum(c.get("judge_errors_by_category", {}).values())
    return bool(c["errors"] or c["skipped"] or judge_errors)


def build_report(
    identity: dict[str, Any], records: list[dict[str, Any]], judge_configured: bool
) -> dict[str, Any]:
    by_category: dict[str, list[dict[str, Any]]] = {}
    by_tool: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by_category.setdefault(r["category"], []).append(r)
        by_tool.setdefault(r["tool"], []).append(r)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "answer_eval_run",
        "identity": identity,
        "counts": counts(records, judge_configured),
        "aggregates": {
            "all": aggregate(records, judge_configured),
            "dev": aggregate([r for r in records if not r["held_out"]], judge_configured),
            "held_out": aggregate([r for r in records if r["held_out"]], judge_configured),
            "by_category": {
                cat: aggregate(rs, judge_configured) for cat, rs in sorted(by_category.items())
            },
            "by_tool": {
                tool: aggregate(rs, judge_configured) for tool, rs in sorted(by_tool.items())
            },
        },
        "cases": records,
    }


def write_private_json(path: Path, data: Any) -> None:
    """Write ``data`` as JSON, mode 600, creating the directory mode 700."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Write a new mode-600 file and rename it over the target: rewriting
    # an existing file in place would put the content in a file that may
    # be world-readable, or already open in another process, while it is
    # written.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def render_summary(report: dict[str, Any]) -> str:
    """Human-readable summary: identity labels, counts, rates, failing IDs."""
    ident, c = report["identity"], report["counts"]
    lines = [
        f"Answer evaluation ({ident['rubric_version']}, cases v{ident['cases_schema_version']}, "
        f"index {ident['index_sha256'][:12]}, commit {ident.get('source_commit') or 'unknown'})",
        f"Answerer: {ident['answerer']}",
        f"Judge:    {ident['judge'] or 'not configured (deterministic checks only)'}",
        f"Cases: {c['selected']} selected, {c['completed']} completed, {c['errors']} errors "
        f"{c['errors_by_status']}, {c['skipped']} skipped",
    ]
    if "judge_errors_by_category" in c:
        lines.append(f"Judge errors: {c['judge_errors_by_category'] or 'none'}")
    for split in ("dev", "held_out"):
        a = report["aggregates"][split]
        lines.append(
            f"{split:8} deterministic {_fmt(a['deterministic_pass_rate'])} | retrieval recall "
            f"{_fmt(a['retrieval_evidence_recall'])} | prompt coverage "
            f"{_fmt(a['prompt_evidence_coverage'])} | citation coverage "
            f"{_fmt(a['citation_evidence_coverage'])}"
        )
        if a["judge"]:
            j = a["judge"]
            lines.append(
                f"{'':8} grounded {_fmt(j['groundedness_pass_rate'])} | correct "
                f"{_fmt(j['correctness_pass_rate'])} | unsupported claims "
                f"{_fmt(j['unsupported_claim_rate'])} | missing facts "
                f"{_fmt(j['missing_fact_rate'])}"
            )
    failing = [r for r in report["cases"] if r["attribution"] or r["status"] != "ok"]
    if failing:
        lines.append("Failing cases (held-out tagged):")
        for r in failing:
            tag = " [held-out]" if r["held_out"] else ""
            failed = [k for k, v in r["deterministic"]["checks"].items() if v == "fail"]
            judge = r["judge"]["error"] or (
                "semantic fail"
                if r["judge"]["status"] == "ok"
                and not (r["judge"]["groundedness_pass"] and r["judge"]["correctness_pass"])
                else ""
            )
            lines.append(
                f"  {r['id']}{tag} ({r['category']}): status={r['status']} "
                f"checks={failed} judge={judge or '-'} causes={r['attribution']}"
            )
    lines.append("Advisory only: no quality threshold is calibrated yet.")
    return "\n".join(lines)


def _case_flags(r: dict[str, Any]) -> dict[str, bool | None]:
    judged = r["judge"]["status"] == "ok"
    return {
        "completed": r["status"] == "ok",
        "deterministic": r["deterministic"]["passed"],
        "groundedness": r["judge"]["groundedness_pass"] if judged else None,
        "correctness": r["judge"]["correctness_pass"] if judged else None,
    }


def compare_reports(base: dict[str, Any], cand: dict[str, Any]) -> dict[str, Any]:
    """Per-case and per-category changes from ``base`` to ``cand``."""
    bi, ci = base["identity"], cand["identity"]
    incompatible = [k for k in _COMPARABLE if bi.get(k) != ci.get(k)]
    if bi.get("judge") != ci.get("judge"):
        incompatible.append("judge")
    # Aggregates over different populations are not comparable either.
    if {r["id"] for r in base["cases"]} != {r["id"] for r in cand["cases"]}:
        incompatible.append("case_selection")
    changed = [k for k in ("answerer", "retrieval", "source_commit") if bi.get(k) != ci.get(k)]
    bs, cs = bi.get("settings") or {}, ci.get("settings") or {}
    timeouts = [k for k in TIMEOUT_SETTINGS if bs.get(k) != cs.get(k)]
    changed += timeouts
    incompatible += timeouts
    b_cases = {r["id"]: r for r in base["cases"]}
    c_cases = {r["id"]: r for r in cand["cases"]}
    regressions, improvements = [], []
    for cid in sorted(b_cases.keys() & c_cases.keys()):
        bf, cf = _case_flags(b_cases[cid]), _case_flags(c_cases[cid])
        worse = [k for k in bf if bf[k] is True and cf[k] is not True]
        better = [k for k in bf if bf[k] is not True and cf[k] is True]
        row = {
            "id": cid,
            "category": c_cases[cid]["category"],
            "held_out": c_cases[cid]["held_out"],
        }
        if worse:
            regressions.append({**row, "what": worse})
        if better:
            improvements.append({**row, "what": better})
    categories = sorted(
        base["aggregates"]["by_category"].keys() | cand["aggregates"]["by_category"].keys()
    )
    by_category = {}
    for cat in categories:
        b = base["aggregates"]["by_category"].get(cat)
        c = cand["aggregates"]["by_category"].get(cat)
        by_category[cat] = {
            "deterministic_pass_rate": [
                b["deterministic_pass_rate"] if b else None,
                c["deterministic_pass_rate"] if c else None,
            ],
            "correctness_pass_rate": [
                b["judge"]["correctness_pass_rate"] if b and b["judge"] else None,
                c["judge"]["correctness_pass_rate"] if c and c["judge"] else None,
            ],
        }
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "answer_eval_comparison",
        "incompatible": incompatible,
        "changed": changed,
        "counts": {"baseline": base["counts"], "candidate": cand["counts"]},
        "only_in_baseline": sorted(b_cases.keys() - c_cases.keys()),
        "only_in_candidate": sorted(c_cases.keys() - b_cases.keys()),
        "regressions": regressions,
        "improvements": improvements,
        "by_split": {
            split: {
                "baseline": base["aggregates"][split],
                "candidate": cand["aggregates"][split],
            }
            for split in ("dev", "held_out")
        },
        "by_category": by_category,
        "variation": "not measured: one run per configuration (repetition is a follow-up)",
    }


def render_comparison(cmp: dict[str, Any]) -> str:
    lines = []
    if cmp["incompatible"]:
        lines.append(
            f"WARNING: runs are not comparable; these identities differ: {cmp['incompatible']}"
        )
    if cmp["changed"]:
        lines.append(f"Changed between runs: {cmp['changed']}")
    for side in ("baseline", "candidate"):
        c = cmp["counts"][side]
        lines.append(
            f"{side:9} {c['selected']} selected, {c['completed']} completed, "
            f"{c['errors']} errors, {c['skipped']} skipped"
        )
    for split, data in cmp["by_split"].items():
        b, c = data["baseline"], data["candidate"]
        lines.append(
            f"{split:8} deterministic {_fmt(b['deterministic_pass_rate'])} -> "
            f"{_fmt(c['deterministic_pass_rate'])} | prompt coverage "
            f"{_fmt(b['prompt_evidence_coverage'])} -> {_fmt(c['prompt_evidence_coverage'])} | "
            f"answer ms {_fmt(b['answer_ms_mean'])} -> {_fmt(c['answer_ms_mean'])}"
        )
        if b["judge"] and c["judge"]:
            lines.append(
                f"{'':8} correct {_fmt(b['judge']['correctness_pass_rate'])} -> "
                f"{_fmt(c['judge']['correctness_pass_rate'])} | grounded "
                f"{_fmt(b['judge']['groundedness_pass_rate'])} -> "
                f"{_fmt(c['judge']['groundedness_pass_rate'])}"
            )
    for cat, rates in cmp["by_category"].items():
        d = rates["deterministic_pass_rate"]
        lines.append(f"  {cat}: deterministic {_fmt(d[0])} -> {_fmt(d[1])}")
    for title, rows in (("Regressions", cmp["regressions"]), ("Improvements", cmp["improvements"])):
        lines.append(f"{title}: {len(rows)}")
        for r in rows:
            tag = " [held-out]" if r["held_out"] else ""
            lines.append(f"  {r['id']}{tag} ({r['category']}): {r['what']}")
    for key in ("only_in_baseline", "only_in_candidate"):
        if cmp[key]:
            lines.append(f"{key}: {cmp[key]}")
    lines.append(f"Variation: {cmp['variation']}")
    return "\n".join(lines)
