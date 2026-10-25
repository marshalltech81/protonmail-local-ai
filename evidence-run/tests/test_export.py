"""The allowlisted export and its canary check (#1759, procedure v5 5.5)."""

import json

import pytest
from evidence_run import export
from evidence_run import probe as probe_mod

CANARY = "SYNTHCANARY1759"


def test_a_valid_export_round_trips_as_counts_booleans_and_tokens():
    data = export.build_export(
        reasons={"no_acceptance_rule": 3},
        outcomes={"unresolved": 3},
        parser={"parse_none": 1},
        counts={"submission_attempts": 12, "isync_commands": 4},
        booleans={"isolation_passed": True},
        predictions={"P1": "observed", "P2": "unobservable"},
        timings={"g1_seconds": 12.5},
    )
    text = export.to_json(data)
    assert json.loads(text) == data


@pytest.mark.parametrize(
    "kwargs",
    [
        {"reasons": {f"{CANARY}-reason": 1}},
        {"outcomes": {"accepted": 1}},
        {"parser": {CANARY: 1}},
        {"counts": {CANARY: 1}},
        {"booleans": {CANARY: True}},
        {"predictions": {"P1": CANARY}},
        {"predictions": {CANARY: "observed"}},
        {"counts": {"submission_attempts": "12"}},
        {"counts": {"submission_attempts": True}},
        {"counts": {"submission_attempts": -1}},
        {"timings": {"g1_seconds": float("nan")}},
        {"booleans": {"isolation_passed": 1}},
    ],
)
def test_anything_outside_the_allowlist_is_refused(kwargs):
    with pytest.raises(export.ExportError):
        export.build_export(**kwargs)


def test_a_free_text_field_can_not_be_added_to_an_export():
    data = export.build_export()
    data["subject"] = f"{CANARY} private"
    with pytest.raises(export.ExportError):
        export.to_json(data)


def test_canary_content_in_a_probed_message_never_reaches_the_export():
    """Canary test: the marker is in the subject, the addresses, the body,
    the Message-ID, the folder name and the Proton ID; the export carries
    none of it, whatever the probe observed."""
    raw = (
        f"From: {CANARY} <{CANARY}@mail.example>\r\n"
        f"To: {CANARY} <{CANARY}-to@mail.example>\r\n"
        f"Subject: {CANARY} subject\r\n"
        "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
        f"Message-ID: <{CANARY}-id@mail.example>\r\n"
        f"X-Pm-Internal-Id: {CANARY}-proton-id\r\n"
        f"\r\n{CANARY} body\r\n"
    ).encode()
    p = probe_mod.Probe()
    p.observe(raw, folder=f"{CANARY}-folder", token=f"{CANARY}-token")
    p.observe(raw.replace(b"-id@", b"-id2@"), folder="Recovered Messages", token="T2")
    text = p.export_json()
    assert CANARY not in text
    assert CANARY.lower() not in text.lower()
    export.validate_export(json.loads(text))
