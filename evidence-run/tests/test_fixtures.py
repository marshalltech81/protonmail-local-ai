"""The fixture generators (#1759, procedure v5 section 2 and 4)."""

import re

import pytest
from evidence_run import fixtures
from src import parser


def _parse(raw: bytes):
    source = parser.SourceMetadata(
        folder="INBOX", flags=frozenset(), size=len(raw), mtime_ns=None, path="/INBOX/cur/x"
    )
    return parser.parse_email_bytes(raw, source)


def _identity_fields(raw: bytes) -> list[str]:
    head = raw.split(b"\r\n\r\n", 1)[0].decode()
    return re.findall(r"^X-Pm-Internal-Id: (.*)$", head, flags=re.MULTILINE | re.IGNORECASE)


def test_fixtures_are_crlf_only_ascii_and_within_the_plain_cap():
    for raw in (
        fixtures.control_message("r", 1),
        fixtures.control_message("r", 2, multipart=True),
        fixtures.forged_message("r", 3, "forged"),
        fixtures.forged_message("r", 4, "duplicated"),
        fixtures.forged_message("r", 5, "conflicting"),
    ):
        assert len(raw) <= fixtures.PLAIN_MAX_BYTES
        assert b"\n" not in raw.replace(b"\r\n", b"")
        assert b"\r" not in raw.replace(b"\r\n", b"")
        raw.decode("ascii")


def test_every_fixture_carries_its_correlation_token_and_parses():
    for n, raw in enumerate(
        (fixtures.control_message("run7", 1), fixtures.control_message("run7", 2, multipart=True)),
        start=1,
    ):
        assert fixtures.token("run7", n).encode() in raw
        assert _parse(raw) is not None


def test_multipart_fixtures_are_multipart_with_a_boundary():
    raw = fixtures.control_message("r", 9, multipart=True)
    msg = _parse(raw)
    assert msg is not None
    assert b"multipart/alternative" in raw and b"boundary=" in raw


@pytest.mark.parametrize(
    "kind, count, distinct",
    [("forged", 1, 1), ("duplicated", 2, 1), ("conflicting", 2, 2)],
)
def test_forged_fixtures_have_the_designed_identity_fields(kind, count, distinct):
    raw = fixtures.forged_message("r", 1, kind)
    values = _identity_fields(raw)
    assert (len(values), len(set(values))) == (count, distinct)
    assert b"X-Pm-Gluon-Id:" in raw


def test_an_unknown_forged_kind_is_refused():
    with pytest.raises(ValueError):
        fixtures.forged_message("r", 1, "other")


def test_the_requested_size_is_exact_and_capped():
    raw = fixtures.control_message("r", 1, size=20_000)
    assert len(raw) == 20_000
    with pytest.raises(ValueError):
        fixtures.control_message("r", 1, size=fixtures.PLAIN_MAX_BYTES + 1)


def test_the_g6_set_is_generated_lazily_at_the_h10_workload():
    assert fixtures.G6_COUNT == 200
    assert fixtures.G6_COUNT * fixtures.G6_MESSAGE_BYTES == 52_428_800  # about 52 MB, unique
    generated = fixtures.g6_messages("r")
    first = next(generated)  # lazily: nothing but this one message exists yet
    assert len(first) == fixtures.G6_MESSAGE_BYTES
    assert b"\n" not in first.replace(b"\r\n", b"")
    assert fixtures.token("r", 1).encode() in first
    second = next(generated)
    assert second != first  # unique payload per message


def test_the_g6_set_honours_a_smaller_count_for_tests():
    messages = list(fixtures.g6_messages("r", count=3, size=4096))
    assert len(messages) == 3 and all(len(m) == 4096 for m in messages)
    assert len(set(messages)) == 3
