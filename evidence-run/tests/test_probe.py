"""The in-memory probe (#1759, procedure v5 5.1-5.2)."""

import re

import pytest
from conftest import INDEXER
from evidence_run import probe as probe_mod
from evidence_run.fixtures import control_message, forged_message
from src import parser
from src.database import EMBEDDING_DIM, Database
from src.provider_identity import Evidence, assess
from tests.conftest import TEST_ACCOUNT, make_thread

ID = "SYNTHPMID1759aaaa=="


def _raw(fields=(), *, message_id="probe1@mail.example", body="body"):
    headers = [
        "From: Synthetic One <one@mail.example>",
        "To: Synthetic Two <two@mail.example>",
        "Subject: Probe fixture",
        "Date: Mon, 01 Jan 2024 12:00:00 +0000",
        f"Message-ID: <{message_id}>",
        *(f"{name}: {value}" for name, value in fields),
    ]
    return ("\r\n".join(headers) + f"\r\n\r\n{body}\r\n").encode()


# Every shape the policy reads, with the production reason it must get.
SHAPES = {
    "clean": ((("X-Pm-Internal-Id", ID),), "INBOX", "unresolved", "no_acceptance_rule"),
    "none": ((), "INBOX", "unresolved", "no_identity_header"),
    "gluon_only": (
        (("X-Pm-Gluon-Id", "SYNTHGLUON-1"),),
        "INBOX",
        "unresolved",
        "no_identity_header",
    ),
    "duplicate": (
        (("X-Pm-Internal-Id", ID), ("X-Pm-Internal-Id", "SYNTHPMID1759bbbb==")),
        "INBOX",
        "unresolved",
        "duplicate_identity_header",
    ),
    "empty": ((("X-Pm-Internal-Id", ""),), "INBOX", "unresolved", "malformed_identity"),
    "recovered": (
        (("X-Pm-Internal-Id", ID), ("X-Pm-Gluon-Id", "SYNTHGLUON-2")),
        "Recovered Messages",
        "rejected",
        "recovered_folder",
    ),
}


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_each_shape_gets_the_policys_reason(name):
    fields, folder, outcome, reason = SHAPES[name]
    p = probe_mod.Probe()
    seen = p.observe(_raw(fields, message_id=f"{name}@mail.example"), folder=folder)
    assert (seen.outcome, seen.reason) == (outcome, reason)
    assert p.reason_counts() == {reason: 1}
    assert p.outcome_counts() == {outcome: 1}


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_evidence_equals_what_production_builds(tmp_path, name):
    """Differential check (the panel's concern, #1584): the probe builds
    ``Evidence`` as the indexer's own read of the stored rows does."""
    fields, folder, _outcome, _reason = SHAPES[name]
    raw = _raw(fields, message_id=f"{name}-diff@mail.example")
    source = parser.SourceMetadata(
        folder=folder,
        flags=frozenset("S"),
        size=len(raw),
        mtime_ns=None,
        path=f"/{folder}/cur/x:2,S",
    )
    msg = parser.parse_email_bytes(raw, source)
    assert msg is not None
    database = Database(tmp_path / "db.sqlite", account=TEST_ACCOUNT)
    try:
        database.upsert_thread(make_thread([msg]), [0.1] * EMBEDDING_DIM)
        pending = database.pending_identity_evidence(limit=10)
        assert len(pending) == 1
        assert probe_mod.evidence_from_message(msg) == pending[0].evidence
    finally:
        database.close()


def test_every_policy_branch_is_covered_on_synthetic_evidence():
    """Local branch coverage of ``assess`` (P12), including the two the
    live run cannot reach: absent historical evidence and a capture that
    stopped."""
    cases = {
        ("Recovered Messages", True, 1, ID): ("rejected", "recovered_folder"),
        ("INBOX", None, 1, ID): ("unresolved", "headers_unknown"),
        ("INBOX", False, 1, ID): ("unresolved", "headers_incomplete"),
        ("INBOX", True, 0, None): ("unresolved", "no_identity_header"),
        ("INBOX", True, 2, ID): ("unresolved", "duplicate_identity_header"),
        ("INBOX", True, 1, None): ("unresolved", "malformed_identity"),
        ("INBOX", True, 1, "has space"): ("unresolved", "malformed_identity"),
        ("INBOX", True, 1, ID): ("unresolved", "no_acceptance_rule"),
    }
    for (folder, complete, count, value), (outcome, reason) in cases.items():
        got = assess(Evidence(folder, complete, count, value))
        assert (got.outcome, got.reason) == (outcome, reason), (folder, complete, count)
    # Recovery overrides every header reason (provider_identity.py).
    assert assess(Evidence("Recovered Messages", None, 2, None)).reason == "recovered_folder"
    assert {r for (_o, r) in cases.values()} == probe_mod.POLICY_REASONS
    # The expected set comes from the policy's own source, not from the list
    # under test, so a reason added there and missed here fails.
    source = (INDEXER / "src" / "provider_identity.py").read_text()
    in_policy = set(re.findall(r'Assessment\("(?:rejected|unresolved)", "([a-z_]+)"', source))
    assert in_policy == probe_mod.POLICY_REASONS


def test_no_usable_message_id_is_a_counted_token_not_a_crash():
    p = probe_mod.Probe()
    raw = b"From: a@mail.example\r\nSubject: no id\r\n\r\nbody\r\n"
    seen = p.observe(raw, folder="INBOX")
    assert seen.parse_token == "parse_none"
    assert p.parser_counts() == {"parse_none": 1}
    assert p.reason_counts() == {}


def test_a_message_nested_too_deeply_is_a_counted_token(monkeypatch):
    def too_deep(*_a, **_k):
        raise parser.MessageNestingError()

    monkeypatch.setattr(parser, "parse_email_bytes", too_deep)
    p = probe_mod.Probe()
    seen = p.observe(_raw(), folder="INBOX")
    assert seen.parse_token == "parse_nesting_error"
    assert p.parser_counts() == {"parse_nesting_error": 1}


def test_equality_is_judged_in_memory_and_never_exposes_the_value():
    p = probe_mod.Probe()
    a = p.observe(
        _raw((("X-Pm-Internal-Id", ID),), message_id="a@mail.example"), "INBOX", token="T-a"
    )
    b = p.observe(
        _raw((("X-Pm-Internal-Id", ID),), message_id="b@mail.example"), "INBOX", token="T-b"
    )
    c = p.observe(
        _raw((("X-Pm-Internal-Id", "SYNTHPMID1759cccc=="),), message_id="c@mail.example"),
        "INBOX",
        token="T-c",
    )
    none = p.observe(_raw(message_id="n@mail.example"), "INBOX", token="T-n")
    assert p.equal("T-a", "T-b") is True
    assert p.equal("T-a", "T-c") is False
    assert p.equal("T-a", "T-n") is None  # no candidate: not comparable
    assert p.equal("T-a", "missing") is None
    # The observation carries a boolean, never the value.
    assert a.candidate_present and b.candidate_present and c.candidate_present
    assert not none.candidate_present
    assert not hasattr(a, "candidate")
    assert ID not in repr(a)


def test_the_equality_key_is_per_probe_and_not_reproducible_across_probes():
    one, two = probe_mod.Probe(), probe_mod.Probe()
    raw = _raw((("X-Pm-Internal-Id", ID),))
    one.observe(raw, "INBOX", token="T")
    two.observe(raw, "INBOX", token="T")
    assert one._digests["T"] != two._digests["T"]


def test_forged_and_control_fixtures_read_as_designed():
    p = probe_mod.Probe()
    p.observe(control_message("r1", 1), folder="INBOX")
    p.observe(forged_message("r1", 2, "duplicated"), folder="INBOX")
    p.observe(forged_message("r1", 3, "conflicting"), folder="INBOX")
    p.observe(forged_message("r1", 4, "forged"), folder="INBOX")
    counts = p.reason_counts()
    assert counts["no_identity_header"] == 1  # control mail: Bridge adds the field later
    assert counts["duplicate_identity_header"] == 2
    assert counts["no_acceptance_rule"] == 1


def test_a_really_nested_message_is_the_same_counted_token():
    """The real parser path, not a stub: a message/rfc822 chain nested far
    past the recursion limit."""
    layer = b"Content-Type: message/rfc822\r\n\r\n"
    raw = b"Message-ID: <deep@mail.example>\r\n" + layer * 3000 + b"Subject: x\r\n\r\nbody\r\n"
    p = probe_mod.Probe()
    seen = p.observe(raw, folder="INBOX")
    assert seen.parse_token == "parse_nesting_error"


# A token seen again must never keep the digest of its earlier observation:
# a stale digest would let equal() report a match the latest observation
# does not support (Codex round 1, #1768).
_LOST_CANDIDATE = {
    "header_gone": lambda: _raw(message_id="a@mail.example"),
    "header_duplicated": lambda: _raw(
        (("X-Pm-Internal-Id", ID), ("X-Pm-Internal-Id", "SYNTHPMID1759bbbb==")),
        message_id="a@mail.example",
    ),
    "header_malformed": lambda: _raw((("X-Pm-Internal-Id", ""),), message_id="a@mail.example"),
    "no_message_id": lambda: b"From: a@mail.example\r\nSubject: no id\r\n\r\nbody\r\n",
}


@pytest.mark.parametrize("shape", sorted(_LOST_CANDIDATE))
def test_a_re_observation_without_a_candidate_invalidates_the_digest(shape):
    p = probe_mod.Probe()
    p.observe(_raw((("X-Pm-Internal-Id", ID),), message_id="b@mail.example"), "INBOX", token="T-b")
    p.observe(_raw((("X-Pm-Internal-Id", ID),), message_id="a@mail.example"), "INBOX", token="T-a")
    assert p.equal("T-a", "T-b") is True
    p.observe(_LOST_CANDIDATE[shape](), "INBOX", token="T-a")
    assert p.equal("T-a", "T-b") is None  # not comparable, never a stale True


def test_a_re_observation_that_is_too_deeply_nested_invalidates_the_digest(monkeypatch):
    p = probe_mod.Probe()
    p.observe(_raw((("X-Pm-Internal-Id", ID),), message_id="b@mail.example"), "INBOX", token="T-b")
    p.observe(_raw((("X-Pm-Internal-Id", ID),), message_id="a@mail.example"), "INBOX", token="T-a")

    def too_deep(*_a, **_k):
        raise parser.MessageNestingError()

    monkeypatch.setattr(parser, "parse_email_bytes", too_deep)
    p.observe(_raw(message_id="a@mail.example"), "INBOX", token="T-a")
    assert p.equal("T-a", "T-b") is None
