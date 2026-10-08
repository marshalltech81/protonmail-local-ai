"""Run every case: answer, deterministic checks, then the judge.

Cases run one at a time (concurrency 1), each under the case timeout,
and the whole run under ``max_runtime_secs``, each call capped by what is
left of it: once it is spent, the
remaining cases are recorded as ``skipped`` (and unjudged answers as
``judge_runtime_budget_exhausted``), which counts against every rate.
A provider's billing or credit refusal (``runner.is_billing_error``)
stops the run with ``ProviderBillingError`` instead (#839).
Log lines carry case IDs and fixed categories only.
"""

import dataclasses
import logging
import time
from collections.abc import Callable
from typing import Any

from tests.answer_eval.cases import Case
from tests.answer_eval.config import LayerConfig
from tests.answer_eval.graders import attribute, budget_omitted_facts, grade_run
from tests.answer_eval.judge import JudgeOutcome, judge_answer
from tests.answer_eval.report import case_record, detail_record
from tests.answer_eval.runner import CaseRun, ProviderBillingError, RunContext, run_case

log = logging.getLogger("answer_eval")


async def evaluate(
    cases: list[Case],
    ctx: RunContext,
    *,
    judge_client: Any = None,
    judge_config: LayerConfig | None = None,
    max_runtime_secs: float = 3600.0,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return the report records and the detail records, one per case."""
    records, details = [], []
    deadline = clock() + max_runtime_secs
    for case in cases:
        remaining = deadline - clock()
        if remaining <= 0:
            run = CaseRun(case.id, status="skipped", error="runtime_budget_exhausted")
        else:
            # No call may outlive the run's budget.
            timeout = min(ctx.case_timeout_secs, remaining)
            run = await run_case(case, dataclasses.replace(ctx, case_timeout_secs=timeout))
            if run.billing_error:
                # Out of credit: every later call would fail too (#839).
                raise ProviderBillingError("answering") from None
        det = grade_run(case, run)

        remaining = deadline - clock()
        if judge_config is None:
            judge = JudgeOutcome(status="not_configured")
        elif run.status != "ok" or run.output is None:
            judge = JudgeOutcome(status="not_run")
        elif remaining <= 0:
            judge = JudgeOutcome(status="error", error="judge_runtime_budget_exhausted")
        else:
            view = run.view
            judge = await judge_answer(
                judge_client,
                judge_config,
                case,
                view.answer,
                run.passages,
                bool(det.abstained),
                statements=view.statements,
                coverage_note=view.coverage_note,
                omitted_facts=budget_omitted_facts(case, run),
                timeout_secs=min(judge_config.timeout_secs, remaining),
            )
        semantic_failed = judge.grade is not None and not judge.grade.passed
        causes = attribute(case, run, det, semantic_failed, judge.status == "error")
        records.append(case_record(case, run, det, judge, causes))
        details.append(detail_record(case, run, judge))
        log.info(
            "case=%s status=%s deterministic=%s judge=%s",
            case.id,
            run.status,
            "pass" if det.passed else "fail",
            judge.error or judge.status,
        )
    return records, details
