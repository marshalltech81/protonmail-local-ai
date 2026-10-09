"""The synthetic counting family's ground truth (#1256), checked for
shape without an index.

``tests/eval/counting_family.json`` classifies every family message by
hand; each scenario's expected sets must follow from that
classification, so a scenario cannot drift from the messages it counts.
``make baseline`` (``tests/baseline/test_counting_family_baseline.py``)
checks the same file against the real index.
"""

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from tests.agent_metrics import is_held_out

TRUTH = json.loads(
    (Path(__file__).parent / "eval" / "counting_family.json").read_text(encoding="utf-8")
)
MESSAGES: dict[str, dict] = TRUTH["messages"]
SCENARIOS: dict[str, dict] = TRUTH["scenarios"]

# The shapes #776 step 1 lists (paging is checked on the index): direct
# notices and other wording, unrelated mail sharing the keywords, forwards
# and replies, repeated notices for one item (a repeat and a duplicate
# delivery), a request and its confirmation, a look-alike of a different
# type and a message in two categories.
REQUIRED_SHAPES = {
    "direct",
    "other_wording",
    "shared_keywords",
    "forward",
    "reply",
    "repeat",
    "duplicate_delivery",
    "request",
    "confirmation",
    "look_alike",
    "two_categories",
}
# Shapes that are never a message of either category.
DECOY_SHAPES = {"shared_keywords", "forward", "reply", "request"}
_REF = re.compile(r"t[1-9][0-9]{2}\.[1-9][0-9]*")


def test_every_shape_is_present() -> None:
    assert {m["shape"] for m in MESSAGES.values()} == REQUIRED_SHAPES


def test_refs_folders_and_deliveries_are_well_formed() -> None:
    for ref, m in MESSAGES.items():
        assert _REF.fullmatch(ref), ref
        # Only a Trash message is outside query_messages' default scope.
        assert m.get("folder", "Trash") == "Trash", ref
        assert m.get("deliveries", 1) >= 1, ref
    assert [ref for ref, m in MESSAGES.items() if m.get("deliveries", 1) > 1] == [
        ref for ref, m in MESSAGES.items() if m["shape"] == "duplicate_delivery"
    ]


def test_shapes_carry_the_categories_they_stand_for() -> None:
    for ref, m in MESSAGES.items():
        categories = {c for c in ("notice", "report") if c in m}
        if m["shape"] in DECOY_SHAPES:
            assert not categories, ref
        elif m["shape"] == "look_alike":
            assert categories == {"report"}, ref
        elif m["shape"] == "two_categories":
            assert categories == {"notice", "report"}, ref
        else:
            assert categories == {"notice"}, ref
    # A repeat names an item another notice already names.
    items = Counter(m["notice"] for m in MESSAGES.values() if "notice" in m)
    for ref, m in MESSAGES.items():
        if m["shape"] == "repeat":
            assert items[m["notice"]] > 1, ref


@pytest.mark.parametrize("sid", SCENARIOS)
def test_expected_sets_follow_from_the_classification(sid: str) -> None:
    s = SCENARIOS[sid]
    category = s["category"]
    in_scope = sorted(
        ref for ref, m in MESSAGES.items() if category in m and m.get("folder") != "Trash"
    )
    in_trash = sorted(
        ref for ref, m in MESSAGES.items() if category in m and m.get("folder") == "Trash"
    )
    assert sorted(s["expected_messages"]) == in_scope
    assert sorted(s["trash_messages"]) == in_trash
    assert s["expected_items"] == len({MESSAGES[ref][category] for ref in in_scope})
    assert s["trap_terms"] and s["question"].strip()


def test_counting_units_differ() -> None:
    """Items, messages and claimants give three different numbers for the
    notices, so an answer in the wrong counting unit gets a wrong count."""
    notices = SCENARIOS["apiary-visit-notices"]
    refs = notices["expected_messages"]
    claimants = sum(MESSAGES[ref].get("deliveries", 1) for ref in refs)
    assert len({notices["expected_items"], len(refs), claimants}) == 3


def test_held_out_flags_follow_the_rule() -> None:
    for sid, s in SCENARIOS.items():
        assert s.get("held_out", False) is is_held_out(sid), sid
    # Both splits are in use.
    assert {s.get("held_out", False) for s in SCENARIOS.values()} == {True, False}


def test_lookups_name_family_messages_once_per_claimant() -> None:
    assert TRUTH["lookups"]
    for lookup in TRUTH["lookups"]:
        assert lookup["args"].get("text"), lookup
        counts = Counter(lookup["expect"])
        for ref, n in counts.items():
            assert ref in MESSAGES, ref
            assert n == MESSAGES[ref].get("deliveries", 1), ref
        # A Trash lookup lists Trash only; any other leaves Trash out.
        in_trash = lookup["args"].get("folder") == "Trash"
        for ref in counts:
            assert (MESSAGES[ref].get("folder") == "Trash") is in_trash, ref
