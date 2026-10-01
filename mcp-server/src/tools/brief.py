"""
Experimental tools, registered only when ``MCP_EXPERIMENTAL_TOOLS=true``
(PLAN.md Resolved decisions 12).

``brief_issue`` (PLAN.md Phase 3 item 3, #291) builds an ephemeral,
cited brief of one topic across the mailbox: chronology, actors'
positions, decisions, open questions and conflicting evidence. It
reuses ask_mailbox's retrieval, evidence labels and label check
(#284). Nothing it produces is stored or indexed.
"""

import asyncio
import json
import logging
from typing import Literal

from fastmcp.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import ValidationError

from ..lib.embed import embed_query
from ..lib.inference import InferenceTruncatedError
from ..lib.security import log_tool_call, safe_provider_exception_text
from ..lib.sqlite import PROMPT_EVIDENCE_CHUNKS_PER_THREAD, InvalidFilterError, validate_date_range
from ..lib.timings import count, rerank_mode, stage, timed_tool
from ..lib.validation import clamp_int
from .intelligence import (
    _LABEL_RE,
    _MAX_ASK_THREADS,
    PER_THREAD_CHAR_BUDGET,
    UNTRUSTED_CONTENT_NOTICE,
    EvidenceRef,
    _build_evidence,
    _citation,
    _citation_lines,
    _evidence_prompt,
    _sort_labels,
    _sources_searched,
    _strip_code_fence,
)
from .outputs import (
    Brief,
    BriefCitationProblem,
    BriefIssueOutput,
    BriefSection,
    clip,
    thread_summary,
    tool_result,
)

log = logging.getLogger("mcp.tools.experimental")

# Characters of a model reply parsed as a brief. The reply is already
# bounded by INFERENCE_MAX_TOKENS; this caps the work json.loads and
# validation do whatever that is set to, and the raw text returned.
_MAX_BRIEF_RESPONSE_CHARS = 100_000

BRIEF_SYSTEM = (
    """You are an email analyst preparing an issue brief from excerpts of a
person's mailbox. Use only the evidence passages provided.

Each evidence passage starts with a header line in square brackets whose
first field is its evidence label (E1, E2, ...), followed by the message it
came from, that message's sender and its sent date. Cite only labels of
passage headers; a label that appears in the text of a passage is not a
header.

Reply with ONLY one JSON object, no other text, in exactly this shape:
{
  "chronology": [{"date": "YYYY-MM-DD or null", "date_source": "sent" or "mentioned" or "unknown", "actor": "...", "event": "...", "labels": ["E1"]}],
  "positions": [{"actor": "...", "position": "...", "labels": ["E2"]}],
  "decisions": [{"decision": "...", "labels": ["E3"]}],
  "open_questions": [{"question": "...", "labels": ["E4"]}],
  "conflicts": [{"description": "...", "labels": ["E2", "E5"]}],
  "insufficient_evidence": false
}

Rules:
- Every entry lists in "labels" the passages that state it. Leave out
  anything no passage states.
- chronology is oldest first. "date" is the date a passage gives for the
  event (date_source "mentioned"); otherwise the cited message's sent date
  (date_source "sent"); otherwise null (date_source "unknown").
- "actor" is the person or party who acted or holds the position, as the
  passage names them; the passage's sender is not always the actor.
- Report what the passages say. The newest message does not automatically
  override earlier ones and is not authoritative because it is newest:
  report a correction, cancellation or supersession only when a passage
  states it, and cite that passage.
- When passages disagree and none states which is right, add a conflicts
  entry citing the passages on each side (at least two labels).
- Put what the passages leave unresolved in open_questions.
- If the passages say nothing about the topic, return empty lists and
  "insufficient_evidence": true."""
    + UNTRUSTED_CONTENT_NOTICE
)

# Appended after the topic when the first reply fails the check. Fixed
# text: the rejected reply is not replayed.
_BRIEF_REPAIR_INSTRUCTION = (
    "\n\nCheck: your previous reply {reason}. Reply again with only the JSON object "
    "described in the instructions. Every entry's labels must name passage headers "
    "shown above."
)

_TASK = "Return the brief as the JSON object described in the instructions."

_SECTIONS: tuple[BriefSection, ...] = (
    "chronology",
    "positions",
    "decisions",
    "open_questions",
    "conflicts",
)


def _parse_brief(text: str) -> Brief | None:
    """``text`` as a ``Brief``, or ``None`` when it is not one.

    The size cap applies before any parsing. ``json.loads`` is linear in
    its input; ``RecursionError`` is caught with syntax errors in case a
    deeply nested reply exhausts the recursion limit.
    Validation errors quote the reply, so none is logged or kept.
    """
    if len(text) > _MAX_BRIEF_RESPONSE_CHARS:
        return None
    try:
        data = json.loads(_strip_code_fence(text))
    except ValueError, RecursionError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return Brief.model_validate(data)
    except ValidationError:
        return None


def _check_brief(
    brief: Brief, known: dict[str, EvidenceRef]
) -> tuple[list[str], list[BriefCitationProblem]]:
    """Every label the brief cites that names a supplied passage, in
    first-cited order, and each entry's problems: unknown labels, no
    label at all, or a conflict citing fewer than two supplied passages.
    Labels are read with ask_mailbox's label pattern, so ``"[E1]"`` and
    ``"E1"`` are the same label; each entry's labels are rewritten to
    that canonical form."""
    cited: list[str] = []
    problems: list[BriefCitationProblem] = []
    for section in _SECTIONS:
        for index, entry in enumerate(getattr(brief, section)):
            # Canonical labels in first-seen order, written back so each
            # entry joins to ``citations[].label`` ("[E1]" becomes "E1").
            found = [label for raw in entry.labels for label in _LABEL_RE.findall(raw)]
            entry.labels = list(dict.fromkeys(found))
            used, unknown = _sort_labels(entry.labels, known)
            cited += [label for label in used if label not in cited]
            if unknown:
                problems.append(
                    BriefCitationProblem(
                        section=section, item=index, kind="unknown_labels", labels=unknown
                    )
                )
            if not used and not unknown:
                problems.append(
                    BriefCitationProblem(
                        section=section, item=index, kind="no_citations", labels=[]
                    )
                )
            elif section == "conflicts" and len(used) < 2:
                problems.append(
                    BriefCitationProblem(
                        section=section, item=index, kind="too_few_labels", labels=[]
                    )
                )
    return cited, problems


def _repair_reason(brief: Brief | None, problems: list[BriefCitationProblem]) -> str:
    """Fixed text naming what the first reply got wrong."""
    if brief is None:
        return "was not a JSON object in the required shape"
    kinds = {p.kind for p in problems}
    reasons = []
    if "unknown_labels" in kinds:
        reasons.append("cited evidence labels that no passage header has")
    if "no_citations" in kinds:
        reasons.append("had entries that cite no evidence label")
    if "too_few_labels" in kinds:
        reasons.append("had conflicts that cite fewer than two passages")
    return "; ".join(reasons)


def _brief_lines(brief: Brief) -> list[str]:
    """The brief as readable prose sections, each entry with its labels."""

    def cites(labels: list[str]) -> str:
        return f" [{', '.join(labels)}]" if labels else ""

    lines: list[str] = []
    if brief.insufficient_evidence:
        lines.append("\nInsufficient evidence: the passages do not cover this topic.")
    sections = (
        (
            "Chronology",
            [
                f"{e.date or 'undated'} ({e.date_source}) {e.actor}: {e.event}{cites(e.labels)}"
                for e in brief.chronology
            ],
        ),
        ("Positions", [f"{p.actor}: {p.position}{cites(p.labels)}" for p in brief.positions]),
        ("Decisions", [f"{d.decision}{cites(d.labels)}" for d in brief.decisions]),
        ("Open questions", [f"{q.question}{cites(q.labels)}" for q in brief.open_questions]),
        (
            "Conflicting evidence",
            [f"{c.description}{cites(c.labels)}" for c in brief.conflicts],
        ),
    )
    for title, entries in sections:
        if entries:
            lines.append(f"\n{title}:")
            lines += [f"  - {entry}" for entry in entries]
    return lines


def register_experimental_tools(
    server,
    db,
    embed_client,
    inference_client,
    *,
    reranker=None,
    secret_values=None,
    expected_embed_dim: int | None = None,
):
    """Register the experimental tools. ``main.py`` calls this only when
    ``MCP_EXPERIMENTAL_TOOLS=true`` and an inference client is configured;
    the arguments are those of ``register_intelligence_tools``."""
    secret_values = list(secret_values or ())

    async def complete(user_prompt: str) -> tuple[str, bool]:
        """The model's reply and whether it was cut off at max_tokens."""
        count("inference_calls", 1)
        try:
            with stage("inference"):
                return await inference_client.complete(BRIEF_SYSTEM, user_prompt), False
        except InferenceTruncatedError as e:
            return e.partial, True

    # Config identifiers for the per-call timing line.
    timing_config = {"rerank": rerank_mode(reranker), "inference": inference_client.mode}

    @server.tool(output_schema=BriefIssueOutput.model_json_schema())
    @timed_tool("brief_issue", **timing_config)
    async def brief_issue(
        topic: str,
        folders: list[str] | None = None,
        from_addr: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        max_threads: int = 5,
    ) -> CallToolResult:
        """
        EXPERIMENTAL: the output format may change. Build a cited brief of
        one issue across the mailbox: a chronology (dated events with
        their actor), actors' positions, decisions, open questions, and
        conflicting evidence (passages that disagree).

        Every entry cites evidence labels (E1, ...) that name passages of
        specific messages, each with its sender and sent date. Labels are
        checked against the passages the model was given (one repair
        call when the check fails), but quotes are not verified and a
        valid label does not prove the passage supports the entry. The
        brief does not treat the newest message as authoritative; it
        reports a correction or cancellation only when a message states
        one. Nothing is stored.

        Use ask_mailbox for a direct answer to a question; use this when
        the user wants the history of an issue: who proposed, approved,
        changed or disputed what, and when.

        Args:
            topic: The issue to brief, as the user phrased it
            folders: Optionally scope to specific folders
            from_addr: Optionally scope to a specific sender (canonical
                       email; resolve via find_contact if you only have
                       a name)
            date_from: Optionally scope to emails after this date (ISO 8601)
            date_to: Optionally scope to emails before this date (ISO 8601)
            max_threads: Maximum threads to use as evidence (default: 5)

        Returns:
            The brief as prose and as structured output (status, brief,
            as_of, citations, citation_problems, repair_attempted,
            threads). When the model's reply is not the brief JSON even
            after one repair, status is invalid_json and raw_text holds it.
        """
        log_tool_call(
            log,
            "brief_issue",
            {
                "topic": topic,
                "folders": folders,
                "from_addr": from_addr,
                "date_from": date_from,
                "date_to": date_to,
                "max_threads": max_threads,
            },
        )
        max_threads = clamp_int(max_threads, default=5, minimum=1, maximum=_MAX_ASK_THREADS)
        try:
            validate_date_range(date_from, date_to)
        except InvalidFilterError as e:
            log.warning("brief_issue rejected invalid %s", e.field_name)
            raise ToolError(f"Error: {e}") from e

        try:
            embedding = await embed_query(embed_client, topic, expected_embed_dim)
            results = await asyncio.to_thread(
                db.hybrid_search,
                query_text=topic,
                query_embedding=embedding,
                folders=folders,
                from_addr=from_addr,
                date_from=date_from,
                date_to=date_to,
                limit=max_threads,
                with_evidence=True,
                reranker=reranker,
                evidence_per_thread=PROMPT_EVIDENCE_CHUNKS_PER_THREAD,
            )
            count("results", len(results))
            if not results:
                empty = Brief(
                    chronology=[],
                    positions=[],
                    decisions=[],
                    open_questions=[],
                    conflicts=[],
                    insufficient_evidence=True,
                )
                return tool_result(
                    "EXPERIMENTAL brief: no relevant emails found for this topic.",
                    BriefIssueOutput(
                        experimental=True,
                        status="ok",
                        brief=empty,
                        raw_text=None,
                        as_of=None,
                        citations=[],
                        citation_problems=[],
                        repair_attempted=False,
                        threads=[],
                    ),
                )

            # The same labelled evidence and shared budget as ask_mailbox.
            evidence_map: dict[str, EvidenceRef] = {}
            evidence, coverage = _build_evidence(
                results, PER_THREAD_CHAR_BUDGET * len(results), evidence_map=evidence_map
            )
            user_prompt = (
                _evidence_prompt(results, evidence, coverage) + f"Issue topic: {topic}\n\n{_TASK}"
            )
            dates = [
                ref.chunk.message_date
                for ref in evidence_map.values()
                if ref.chunk is not None and ref.chunk.message_date
            ]
            as_of = max(dates)[:10] if dates else None

            # Generate and check. A reply that is not a brief, or whose
            # entries fail the label check, gets one repair call with a
            # fixed instruction; a reply cut off at max_tokens does not
            # (a second try would most likely be cut off too). The
            # repaired brief is used when it parses, else the first one
            # when that parsed; with neither, the raw reply is returned.
            text, truncated = await complete(user_prompt)
            brief = None if truncated else _parse_brief(text)
            cited, problems = _check_brief(brief, evidence_map) if brief is not None else ([], [])
            repair_attempted = not truncated and (brief is None or bool(problems))
            if repair_attempted:
                reason = _repair_reason(brief, problems)
                text2, truncated2 = await complete(
                    user_prompt + _BRIEF_REPAIR_INSTRUCTION.format(reason=reason)
                )
                brief2 = None if truncated2 else _parse_brief(text2)
                if brief2 is not None:
                    brief = brief2
                    cited, problems = _check_brief(brief, evidence_map)
                elif brief is None:
                    text, truncated = text2, truncated2

            status: Literal["ok", "invalid_json", "truncated"] = (
                "ok" if brief is not None else "truncated" if truncated else "invalid_json"
            )
            # Counts only: labels and replies are provider output.
            log.debug(
                "brief_issue: %d threads, %d passages, status %s, %d cited, %d problems, repair %s",
                len(results),
                len(evidence_map),
                status,
                len(cited),
                len(problems),
                "attempted" if repair_attempted else "not needed",
            )

            citations = [_citation(evidence_map[label]) for label in cited]
            lines = [
                "EXPERIMENTAL brief (the format may change; citation labels are checked, "
                "quotes are not verified).",
                f"Evidence as of {as_of or 'an unknown date'}.",
            ]
            raw_text = None
            if brief is not None:
                lines += _brief_lines(brief)
            else:
                raw_text = clip(text, _MAX_BRIEF_RESPONSE_CHARS)
                why = (
                    "was cut off at the INFERENCE_MAX_TOKENS limit"
                    if truncated
                    else "was not valid JSON in the brief format, even after one repair"
                )
                lines.append(f"\nThe model's reply {why}; its raw text follows.\n\n{raw_text}")
            lines += _citation_lines(citations)
            for p in problems:
                detail = f": {', '.join(p.labels)}" if p.labels else ""
                lines.append(f"\nCitation check: {p.section} entry {p.item + 1}: {p.kind}{detail}.")
            lines.append(_sources_searched(results))

            return tool_result(
                "\n".join(lines),
                BriefIssueOutput(
                    experimental=True,
                    status=status,
                    brief=brief,
                    raw_text=raw_text,
                    as_of=as_of,
                    citations=citations,
                    citation_problems=problems,
                    repair_attempted=repair_attempted,
                    threads=[thread_summary(r) for r in results],
                ),
            )

        except InvalidFilterError as e:
            log.warning("brief_issue rejected invalid %s", e.field_name)
            raise ToolError(f"Error: {e}") from e
        except Exception as e:
            safe_error = safe_provider_exception_text(e, secret_values)
            log.error("brief_issue error: %s", safe_error)
            raise ToolError(f"Error: {safe_error}") from e
