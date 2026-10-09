"""The synthetic counting family (#1256) against the real index.

``tests/eval/counting_family.json`` holds the hand-written ground truth
for corpus threads 102-112 (apiary visit notices). These tests check
that every family message is indexed as the truth describes, and that
the real ``query_messages`` tool counts the family's lookups exactly:
paged to ``has_more: false`` at several page sizes, ``total_matches``
equal to the union of the pages and ``indeterminate`` 0, a page
boundary between a duplicate delivery's two claimants, overlapping
lookups unioned by claimant ID (a duplicate delivery is two claimants of
one Message-ID), Trash left out unless asked for, and date bounds
inclusive of whole days. Whether an agent counts the right unit or tells
notices from decoys is not checked here (#1257).

Skipped unless ``BASELINE_DIR`` is set; ``make baseline`` runs it.
"""

import asyncio
import json
import os
from collections import Counter
from collections.abc import Awaitable, Callable
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest
from mcp.types import CallToolResult
from src.lib.sqlite import Database
from src.tools.retrieval import register_retrieval_tools

from tests.conftest import FakeMCPServer

pytestmark = pytest.mark.baseline

TRUTH = json.loads(
    (Path(__file__).parent.parent / "eval" / "counting_family.json").read_text(encoding="utf-8")
)
LOOKUPS = TRUTH["lookups"]
# Page sizes: one row, sizes that split the duplicate delivery's tied
# rows across pages, and the tool's default.
PAGE_LIMITS = [1, 2, 3, None]
_DOMAIN = "@baseline.example"
DUPLICATED = [ref for ref, m in TRUTH["messages"].items() if m.get("deliveries", 1) > 1]
# The lookups that list the duplicate delivery's pair.
PAIR_LOOKUPS = [lookup for lookup in LOOKUPS if set(DUPLICATED) & set(lookup["expect"])]


def _message_ref(message_id: str) -> str:
    """``t103.1@baseline.example`` -> ``t103.1``."""
    return message_id.removesuffix(_DOMAIN)


@pytest.fixture(scope="module")
def baseline_dir() -> Path:
    baseline_dir = os.environ.get("BASELINE_DIR")
    if not baseline_dir:
        pytest.skip("BASELINE_DIR not set; run `make baseline`")
    return Path(baseline_dir)


@pytest.fixture(scope="module")
def baseline_db(baseline_dir: Path) -> Database:
    return Database(str(baseline_dir / "mail.db"))


def _lookup_id(lookup: dict) -> str:
    return "-".join(f"{k}={v}" for k, v in lookup["args"].items())


def _query_messages(baseline_db: Database) -> Callable[..., Awaitable[CallToolResult]]:
    server = FakeMCPServer()
    register_retrieval_tools(server, baseline_db)
    return cast(Callable[..., Awaitable[CallToolResult]], server.tools["query_messages"])


def _page_through(baseline_db: Database, args: dict, limit: int | None) -> list[dict]:
    """Every page of ``query_messages(**args)``, through the real tool,
    following ``next_cursor`` until ``has_more`` is false."""
    query = _query_messages(baseline_db)
    sized = args if limit is None else {**args, "limit": limit}
    pages: list[dict] = []
    cursor: str | None = None
    while True:
        result = asyncio.run(query(**sized, cursor=cursor))
        page = cast(dict, result.structured_content)
        pages.append(page)
        if not page["has_more"]:
            assert page["next_cursor"] is None
            return pages
        assert page["next_cursor"]
        cursor = page["next_cursor"]
        assert len(pages) < 100, "paging did not end"


def _rows(pages: list[dict]) -> list[dict]:
    return [row for page in pages for row in page["messages"]]


def _refs_by_claimant(pages: list[dict]) -> dict[str, str]:
    return {row["claimant_id"]: _message_ref(row["message_id"]) for row in _rows(pages)}


def test_family_messages_are_indexed_as_described(baseline_db: Database) -> None:
    """Each truth message is indexed in its own thread, in Trash when the
    truth says so, once per delivery (a claimant per file)."""
    with closing(baseline_db._connect()) as conn:
        rows = conn.execute("SELECT message_id, thread_id, folder FROM messages").fetchall()
    claimants: Counter[str] = Counter()
    for message_id, thread_id, folder in rows:
        ref = _message_ref(message_id)
        if ref in TRUTH["messages"]:
            claimants[ref] += 1
            assert _message_ref(thread_id) == f"{ref.split('.')[0]}.1", ref
            assert (folder == "Trash") is (TRUTH["messages"][ref].get("folder") == "Trash"), ref
    assert claimants == {ref: m.get("deliveries", 1) for ref, m in TRUTH["messages"].items()}


@pytest.mark.parametrize("limit", PAGE_LIMITS, ids=lambda n: f"limit={n}")
@pytest.mark.parametrize("lookup", LOOKUPS, ids=_lookup_id)
def test_lookup_pages_to_the_exact_set(
    baseline_db: Database, lookup: dict, limit: int | None
) -> None:
    pages = _page_through(baseline_db, lookup["args"], limit)
    rows = _rows(pages)
    claimant_ids = [row["claimant_id"] for row in rows]
    # No row twice across pages, and none missing: the pages' union is
    # the truth's set, one row per claimant.
    assert len(claimant_ids) == len(set(claimant_ids))
    assert sorted(_message_ref(row["message_id"]) for row in rows) == sorted(lookup["expect"])
    for page in pages:
        assert page["total_matches"] == len(rows)
        assert page["indeterminate"] == 0
    assert sum(page["returned"] for page in pages) == len(rows)
    assert [page["offset"] for page in pages] == [
        sum(p["returned"] for p in pages[:i]) for i in range(len(pages))
    ]
    if limit is not None and limit < len(lookup["expect"]):
        assert len(pages) > 1, "the lookup fits one page; it tests no paging"


@pytest.mark.parametrize("lookup", PAIR_LOOKUPS, ids=_lookup_id)
def test_a_page_boundary_between_the_duplicate_pair_keeps_both(
    baseline_db: Database, lookup: dict
) -> None:
    """The duplicate delivery's two claimants share a Message-ID and a
    date, so they sit next to each other in the tool's order. A limit that
    ends a page between them must return one on each page: neither lost
    at the boundary nor repeated on the next page."""
    (ref,) = [ref for ref in DUPLICATED if ref in lookup["expect"]]
    order = [
        _message_ref(row["message_id"])
        for row in _rows(_page_through(baseline_db, lookup["args"], None))
    ]
    first = order.index(ref)
    assert order[first + 1] == ref, "the pair is not adjacent in the tool's order"
    # Page 0 ends with the first copy; page 1 starts with the second.
    pages = _page_through(baseline_db, lookup["args"], first + 1)
    assert _message_ref(pages[0]["messages"][-1]["message_id"]) == ref
    assert _message_ref(pages[1]["messages"][0]["message_id"]) == ref
    claimants = [
        row["claimant_id"] for row in _rows(pages) if _message_ref(row["message_id"]) == ref
    ]
    assert len(claimants) == 2
    assert len(set(claimants)) == 2
    assert pages[0]["messages"][-1]["claimant_id"] != pages[1]["messages"][0]["claimant_id"]
    assert [_message_ref(row["message_id"]) for row in _rows(pages)] == order


def test_the_default_limit_needs_no_second_page_for_a_family_lookup(
    baseline_db: Database,
) -> None:
    """The family's largest lookup fits the default 25 rows, so its paging
    is exercised with the small explicit limits above."""
    largest = max(LOOKUPS, key=lambda lookup: len(lookup["expect"]))
    assert len(largest["expect"]) < 25
    assert len(_page_through(baseline_db, largest["args"], None)) == 1


def test_overlapping_lookups_union_by_claimant(baseline_db: Database) -> None:
    """The "apiary" and "hive" lookups overlap; their union keyed by
    claimant ID is the truth's union, and keying by Message-ID instead
    would merge the duplicate delivery's two files into one."""
    lookups = [
        lookup for lookup in LOOKUPS if lookup["args"] in ({"text": "apiary"}, {"text": "hive"})
    ]
    assert len(lookups) == 2
    union: dict[str, str] = {}
    totals = 0
    for lookup in lookups:
        pages = _page_through(baseline_db, lookup["args"], 2)
        totals += pages[0]["total_matches"]
        union.update(_refs_by_claimant(pages))
    expected = Counter(lookups[0]["expect"]) | Counter(lookups[1]["expect"])
    assert Counter(union.values()) == expected
    # The lookups overlap, so adding their totals overcounts.
    assert totals > len(union)
    duplicated = {ref for ref, m in TRUTH["messages"].items() if m.get("deliveries", 1) > 1}
    assert duplicated
    assert len(set(union.values())) == len(union) - len(duplicated)


def test_trash_is_counted_only_when_asked_for(baseline_db: Database) -> None:
    trashed = {ref for ref, m in TRUTH["messages"].items() if m.get("folder") == "Trash"}
    assert trashed
    for lookup in LOOKUPS:
        refs = set(_refs_by_claimant(_page_through(baseline_db, lookup["args"], None)).values())
        if lookup["args"].get("folder") == "Trash":
            assert refs == trashed
        else:
            assert not refs & trashed, _lookup_id(lookup)


def test_date_bounds_cover_whole_days(baseline_db: Database) -> None:
    """The bounded lookup's first and last matches fall on its bound days,
    so date-only bounds must include the whole of each day."""
    (bounded,) = [lookup for lookup in LOOKUPS if "date_from" in lookup["args"]]
    rows = _rows(_page_through(baseline_db, bounded["args"], None))
    days = sorted(row["sent_at"][:10] for row in rows)
    assert days[0] == bounded["args"]["date_from"]
    assert days[-1] == bounded["args"]["date_to"]
    unbounded = {k: v for k, v in bounded["args"].items() if not k.startswith("date_")}
    everything = _rows(_page_through(baseline_db, unbounded, None))
    assert len(everything) > len(rows), "the bounds exclude nothing"
