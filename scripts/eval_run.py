#!/usr/bin/env python3
"""Run the manual eval queries against Open WebUI's HTTP API.

Replaces the copy-paste loop. Reads the curated query list from
``mcp-server/tests/eval/eval-queries.md``, posts each prompt to Open
WebUI as a single-turn user message, captures the assistant response,
and writes a timestamped markdown report under ``.secrets/`` so
mailbox-sourced content stays out of the repo.

The orchestration LLM, MCP tool wiring, and any tool-routing biases
remain unchanged: we are driving the same surface a human user drives
through the browser, just programmatically.

Stdlib-only on purpose — no extra dep to maintain. Run from the repo
root::

    python3 scripts/eval_run.py [--tiers 1,2,3] [--model qwen2.5:32b-instruct]

Operator setup (one-time):

1. Open WebUI → Settings → Account → API Keys → Generate New API Key.
2. ``printf '%s\\n' "<paste-key>" > .secrets/open_webui_api_key.txt``
3. ``chmod 600 .secrets/open_webui_api_key.txt``
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
QUERIES_FILE = REPO_ROOT / "mcp-server" / "tests" / "eval" / "eval-queries.md"
API_KEY_FILE = REPO_ROOT / ".secrets" / "open_webui_api_key.txt"
RESULTS_DIR = REPO_ROOT / ".secrets"
DEFAULT_BASE_URL = "http://localhost:8080"
DEFAULT_MODEL = "qwen2.5:32b-instruct"
# Open WebUI binds tool sets to a chat conversation in the UI; the
# /api/chat/completions endpoint does NOT auto-inherit those bindings.
# Without ``tool_ids`` the model can't see the MCP tools and abdicates
# with "I don't have access to your email account" — confirmed by
# inspecting the raw API response. The default ID matches the entry
# returned by ``GET /api/v1/tools/`` on this stack
# (the MCP connection registered as ``protonmail-mcp``).
DEFAULT_TOOL_IDS = ["server:mcp:protonmail-mcp"]
# Per-query deadline. Tier 5 / 11 (ask_mailbox over many threads on a
# populated mailbox) can run several minutes on a 32B model. Set above
# the documented worst case so timeouts only fire on truly stuck calls.
PER_QUERY_TIMEOUT_S = 600


@dataclass
class EvalQuery:
    tier: int
    slug: str
    prompt: str


def load_api_key() -> str:
    if not API_KEY_FILE.exists():
        sys.exit(
            f"Missing {API_KEY_FILE}. Generate one in Open WebUI "
            "(Settings → Account → API Keys) and save it to that path."
        )
    key = API_KEY_FILE.read_text().strip()
    if not key:
        sys.exit(f"{API_KEY_FILE} is empty. Paste an API key into it and try again.")
    return key


# Markdown structure: queries appear under ``### tier{N}-{slug}``
# headers, with the prompt as the first blockquote line under the
# header. The eval file is hand-authored so we tolerate small variation
# (e.g. extra blank lines between header and quote).
QUERY_HEADER_RE = re.compile(r"^###\s+`?(tier\d+-[a-z0-9-]+|bonus-[a-z0-9-]+)`?\s*$")
PROMPT_LINE_RE = re.compile(r"^>\s?(.+?)\s*$")


def parse_queries(markdown_path: Path) -> list[EvalQuery]:
    """Pull tier/slug/prompt triples out of eval-queries.md.

    The file is gitignored and may evolve, so the parser is permissive:
    walk the lines, switch into "looking for prompt" mode when we hit a
    tier header, and capture the first blockquote we see in that mode.
    """
    if not markdown_path.exists():
        sys.exit(
            f"Missing {markdown_path}. This file is gitignored — "
            "create it from your real eval queries before running."
        )

    queries: list[EvalQuery] = []
    current_slug: str | None = None
    for raw in markdown_path.read_text().splitlines():
        header = QUERY_HEADER_RE.match(raw.strip())
        if header:
            current_slug = header.group(1)
            continue
        if current_slug is None:
            continue
        prompt = PROMPT_LINE_RE.match(raw.strip())
        if prompt:
            tier_match = re.match(r"tier(\d+)-", current_slug)
            tier = int(tier_match.group(1)) if tier_match else 0
            queries.append(EvalQuery(tier=tier, slug=current_slug, prompt=prompt.group(1)))
            current_slug = None
    return queries


def call_owui(
    *,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    tool_ids: list[str],
) -> dict:
    """POST a single user-turn message and return the parsed JSON.

    Open WebUI's ``/api/chat/completions`` is OpenAI-shaped — passing
    ``stream: false`` collapses the response to a single JSON object
    even when tool calls happen mid-conversation. ``tool_ids`` MUST be
    set explicitly: the API path does not inherit the tool bindings
    that the UI attaches to a conversation, so without it the model
    runs without MCP and abdicates on every mailbox query.
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "tool_ids": tool_ids,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=PER_QUERY_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = "<no body>"
        return {"_error": f"HTTP {e.code}: {err_body[:500]}"}
    except urllib.error.URLError as e:
        return {"_error": f"URLError: {e.reason}"}
    except TimeoutError:
        return {"_error": f"Timed out after {PER_QUERY_TIMEOUT_S}s"}
    return data


def extract_assistant_text(response: dict) -> str:
    """Walk the OpenAI-shaped response down to the assistant message text.

    OWUI mirrors the standard ``choices[0].message.content`` shape but
    occasionally wraps things differently across versions; fall back to
    a JSON dump when the expected path is missing so the report still
    captures *something* the operator can read.
    """
    if "_error" in response:
        return f"[error] {response['_error']}"
    try:
        return response["choices"][0]["message"]["content"] or ""
    except KeyError, IndexError, TypeError:
        return (
            "[unexpected response shape]\n\n```json\n"
            + json.dumps(response, indent=2)[:4000]
            + "\n```"
        )


def extract_tool_calls(response: dict) -> list[dict]:
    """Pull tool_calls from the response if present.

    Useful for spotting fabrication-vs-real-call without having to read
    every full response. Older OWUI versions may surface these inside
    ``message.tool_calls``; newer ones sometimes attach them to a
    sources/citations field. Return both shapes if found.
    """
    out: list[dict] = []
    try:
        msg = response["choices"][0]["message"]
        if msg.get("tool_calls"):
            out.extend(msg["tool_calls"])
    except KeyError, IndexError, TypeError:
        pass
    if isinstance(response.get("sources"), list):
        out.extend(response["sources"])
    return out


def heuristic_grade(response_text: str, tool_calls: list[dict]) -> str:
    """Quick triage so the operator can scan the report for likely fails.

    Not a substitute for the human PASS/PARTIAL/FAIL judgment in
    eval-queries.md — synthesis quality still has to come from a
    person reading the answer. The heuristics catch only the easy
    mechanical failures (missing tool call, "thread not found",
    abdication phrasing).
    """
    text_lower = response_text.lower()
    if response_text.startswith("[error]"):
        return "ERROR"
    # Tool calls are the strongest positive signal — if any tool fired,
    # the response is grounded regardless of the prose, and abdication
    # phrasing is at most a hedge inside a real answer.
    if tool_calls:
        return "needs human review"
    # Abdication phrases observed in real model output during eval
    # runs. Cover the major surface forms; new variants get added
    # here as we see them in reports rather than in the model. Each
    # phrase is a fragment so the model can wrap it in any sentence
    # ("I'm sorry, but I don't have direct access...") and still match.
    abdication_phrases = (
        # Direct denial forms
        "i don't have access",
        "i do not have access",
        "i don't have direct access",
        "i do not have direct access",
        "i cannot access",
        "i can't access",
        "i'm unable to access",
        "i am unable to access",
        # "real-time" / "specific" hedges that always precede an
        # abdication-style follow-up
        "i don't have real-time",
        "i do not have real-time",
        "i don't have specific information",
        "i do not have specific information",
        "i don't have any specific information",
        # Redirect-to-client patterns
        "you can typically",
        "use your email client",
        "check your email client",
        "your email client",
        "your task management",
    )
    if any(phrase in text_lower for phrase in abdication_phrases):
        return "ABDICATION (likely FAIL)"
    if "thread not found" in text_lower:
        return "MISS (likely FAIL or PARTIAL)"
    if "no results found" in text_lower or "no contacts found" in text_lower:
        return "EMPTY (PARTIAL — verify if expected)"
    if not tool_calls and "[1]" not in response_text and "source" not in text_lower:
        return "NO TOOL CITATION (suspicious)"
    return "needs human review"


def write_report(
    *,
    results: list[tuple[EvalQuery, dict, str, list[dict], str, float]],
    out_dir: Path,
    model: str,
    base_url: str,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"eval-results-{stamp}.md"

    lines = [
        f"# Eval run — {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"- Model: `{model}`",
        f"- Endpoint: {base_url}/api/chat/completions",
        f"- Queries: {len(results)}",
        "",
        "Heuristic grades are mechanical (did the model error, abdicate, "
        "report empty, fail to cite a tool?). Full PASS/PARTIAL/FAIL "
        "judgment still requires reading each answer against the criteria "
        "in `mcp-server/tests/eval/eval-queries.md`.",
        "",
        "## Summary",
        "",
        "| Slug | Heuristic | Latency |",
        "|---|---|---|",
    ]
    for q, _resp, _text, _tool_calls, grade, latency in results:
        lines.append(f"| `{q.slug}` | {grade} | {latency:.1f}s |")
    lines.append("")

    for q, response, text, tool_calls, grade, latency in results:
        lines.extend(
            [
                "---",
                "",
                f"## `{q.slug}` (Tier {q.tier}) — {grade} — {latency:.1f}s",
                "",
                "**Prompt:**",
                "",
                f"> {q.prompt}",
                "",
                "**Response:**",
                "",
                text.strip() or "(empty)",
                "",
            ]
        )
        if tool_calls:
            lines.extend(
                [
                    "**Tool calls observed:**",
                    "",
                    "```json",
                    json.dumps(tool_calls, indent=2)[:3000],
                    "```",
                    "",
                ]
            )
        if "_error" in response:
            lines.extend(["**Raw error:**", "", f"`{response['_error']}`", ""])

    # Reports embed mailbox-derived content (the operator's real
    # email subjects, sender addresses, message snippets returned by
    # the MCP tools). The .secrets directory itself is operator-
    # configured to 700, but file-level mode under default umask is
    # often 0644 — readable by other local users on a shared host.
    # Force 0600 so the report stays user-private regardless of
    # umask. ``write_text`` doesn't expose a mode arg, so write via
    # an explicit ``open`` with ``opener`` honoring 0600 from the
    # start (more secure than write-then-chmod, which leaves a brief
    # window where the file is world-readable).
    def _opener(path_arg: str, flags: int) -> int:
        return os.open(path_arg, flags, 0o600)

    with open(path, "w", encoding="utf-8", opener=_opener) as fh:
        fh.write("\n".join(lines))
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tiers",
        help="Comma-separated tier numbers to run (e.g. '1,2,3'). Default: all.",
    )
    parser.add_argument(
        "--slugs",
        help="Comma-separated slugs to run (e.g. 'tier1-index-status'). Overrides --tiers.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Model name (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Open WebUI base URL (default: {DEFAULT_BASE_URL}).",
    )
    parser.add_argument(
        "--tool-ids",
        default=",".join(DEFAULT_TOOL_IDS),
        help=(
            "Comma-separated Open WebUI tool IDs to attach to each request "
            f"(default: {','.join(DEFAULT_TOOL_IDS)}). Without these the API "
            "path runs without MCP and the model abdicates."
        ),
    )
    args = parser.parse_args()
    tool_ids = [t.strip() for t in args.tool_ids.split(",") if t.strip()]

    api_key = load_api_key()
    queries = parse_queries(QUERIES_FILE)
    if not queries:
        sys.exit(f"No queries parsed from {QUERIES_FILE}. Check the headers/blockquotes.")

    if args.slugs:
        wanted = {s.strip() for s in args.slugs.split(",")}
        queries = [q for q in queries if q.slug in wanted]
    elif args.tiers:
        wanted_tiers = {int(t.strip()) for t in args.tiers.split(",")}
        queries = [q for q in queries if q.tier in wanted_tiers]

    if not queries:
        sys.exit("No queries match the given --tiers / --slugs filter.")

    print(f"Running {len(queries)} queries against {args.base_url} (model={args.model})...")
    results: list[tuple[EvalQuery, dict, str, list[dict], str, float]] = []
    for i, q in enumerate(queries, 1):
        print(f"  [{i}/{len(queries)}] {q.slug} ...", end="", flush=True)
        t0 = time.monotonic()
        response = call_owui(
            base_url=args.base_url,
            api_key=api_key,
            model=args.model,
            prompt=q.prompt,
            tool_ids=tool_ids,
        )
        latency = time.monotonic() - t0
        text = extract_assistant_text(response)
        tool_calls = extract_tool_calls(response)
        grade = heuristic_grade(text, tool_calls)
        print(f" {grade} ({latency:.1f}s)")
        results.append((q, response, text, tool_calls, grade, latency))

    report = write_report(
        results=results,
        out_dir=RESULTS_DIR,
        model=args.model,
        base_url=args.base_url,
    )
    print(f"\nReport written to {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
