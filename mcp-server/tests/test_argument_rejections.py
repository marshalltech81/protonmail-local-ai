"""Rate-limited "rejected invalid argument" warnings (#1039).

A remote client can repeat a rejected request as fast as it likes, so
every tool's argument-rejection warning goes through a
``RateLimitedLog`` keyed by tool and field: the first rejection per key
and window is logged, the rest are counted into one summary line.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import re
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from src.lib.rate_limited_log import ArgumentRejections
from src.tools.brief import register_experimental_tools
from src.tools.intelligence import register_intelligence_tools
from src.tools.retrieval import register_retrieval_tools
from src.tools.search import register_search_tools

from tests.conftest import FakeEmbedClient, FakeInferenceClient

MARKER = "SYNTHETIC_REJECTION_MARKER"
_SRC = Path(__file__).resolve().parent.parent / "src"
_LOG_CALL = re.compile(
    r"\blog\.(?:debug|info|warning|error|exception)\(\s*f?[\"'][^\"']*rejected (?:invalid|an empty)"
)


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_no_rejection_warning_bypasses_the_limiter():
    """Every ``InvalidFilterError`` handler and every other "rejected
    invalid" or "rejected an empty" argument warning in ``src/`` goes
    through a rate limiter, derived from the code so a new site cannot
    log once per request unnoticed."""
    handlers = 0
    for path in sorted(_SRC.rglob("*.py")):
        text = path.read_text()
        assert not _LOG_CALL.search(text), f"{path.name} logs a rejection directly"
        for node in ast.walk(ast.parse(text)):
            if not (
                isinstance(node, ast.ExceptHandler)
                and node.type is not None
                and "InvalidFilterError" in ast.unparse(node.type)
            ):
                continue
            handlers += 1
            calls = [
                c.func
                for stmt in node.body
                for c in ast.walk(stmt)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            ]
            assert any(f.attr in {"record", "reject"} for f in calls), (
                f"{path.name}:{node.lineno} handles InvalidFilterError without the limiter"
            )
            assert not any(isinstance(f.value, ast.Name) and f.value.id == "log" for f in calls), (
                f"{path.name}:{node.lineno} logs InvalidFilterError directly"
            )
    assert handlers >= 18  # the sites on main when #1039 landed


def test_first_per_tool_and_field_then_a_summary_with_counts(caplog):
    clock = _Clock()
    log = logging.getLogger("mcp.test.argument_rejections")
    rejections = ArgumentRejections(log, ("search_emails", "query_messages"), clock=clock)
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            rejections.reject("search_emails", "date_from")
        rejections.reject("query_messages", "cursor")
        rejections.reject("query_messages", f"field-{MARKER}")
        clock.now += 60.0
        rejections.reject("search_emails", "date_from")
    assert _warnings(caplog) == [
        "rejected invalid argument: search_emails.date_from",
        "rejected invalid argument: query_messages.cursor",
        "rejected invalid argument: query_messages.other",
        "rejected invalid arguments in the last 60s: "
        "search_emails.date_from=3 query_messages.cursor=1 query_messages.other=1",
        "rejected invalid argument: search_emails.date_from",
    ]
    assert MARKER not in caplog.text


def test_an_unregistered_tool_raises_with_fixed_text():
    rejections = ArgumentRejections(logging.getLogger("mcp.test"), ("search_emails",))
    with pytest.raises(ValueError, match="unknown rate-limit key"):
        rejections.reject(f"tool-{MARKER}", "date_from")


_BAD_DATE = {"date_from": f"{MARKER}-01"}

# (tool, arguments, the key its rejection is logged under)
_REJECTED_CALLS = [
    ("search_emails", {"query": "invoice", "mode": "keyword", **_BAD_DATE}, "date_from"),
    ("get_evidence", {"query": "invoice", **_BAD_DATE}, "date_from"),
    ("search_attachments", {"query": "invoice", **_BAD_DATE}, "date_from"),
    ("query_messages", {"text": MARKER, **_BAD_DATE}, "date_from"),
    ("query_messages", {"cursor": MARKER}, "cursor"),
    ("query_attachments", {"filename": MARKER, **_BAD_DATE}, "date_from"),
    ("query_attachments", {"cursor": MARKER}, "cursor"),
    ("get_attachment", {"attachment_occurrence_id": MARKER, "offset": -1}, "offset"),
    ("get_message", {"message_id": "m2", "offset": -1}, "offset"),
    ("list_threads", {"filter_type": MARKER}, "filter_type"),
    ("find_contact", {"query": "   "}, "query"),
    ("ask_mailbox", {"question": f"What about {MARKER}?", **_BAD_DATE}, "date_from"),
    (
        "extract_from_emails",
        {"query": MARKER, "schema": {"vendor": "string"}, **_BAD_DATE},
        "date_from",
    ),
    ("brief_issue", {"topic": MARKER, **_BAD_DATE}, "date_from"),
    ("check_conclusion", {"conclusion": MARKER, **_BAD_DATE}, "date_from"),
]


@pytest.mark.parametrize(("tool", "args", "field"), _REJECTED_CALLS)
def test_a_repeated_rejection_logs_one_line(fake_server, seeded_db, caplog, tool, args, field):
    embed, llm = FakeEmbedClient(), FakeInferenceClient()
    register_search_tools(fake_server, seeded_db, embed)
    register_retrieval_tools(fake_server, seeded_db)
    register_intelligence_tools(fake_server, seeded_db, embed, llm)
    register_experimental_tools(fake_server, seeded_db, embed, llm)
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            with pytest.raises(ToolError):
                asyncio.run(fake_server.tools[tool](**args))
    rejected = [m for m in _warnings(caplog) if "rejected" in m]
    assert rejected == [f"rejected invalid argument: {tool}.{field}"]
    assert MARKER not in caplog.text
