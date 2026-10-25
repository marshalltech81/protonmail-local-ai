"""#1517: reply header syntax must not hide the referenced messages."""

import pytest
from src.parser import SourceMetadata, parse_email_bytes


def _parse(headers):
    raw = (
        "Message-ID: <child@example.test>\r\nSubject: Different subject\r\n"
        "From: a@example.test\r\nTo: b@example.test\r\n" + headers + "\r\n\r\nSynthetic body"
    ).encode()
    return parse_email_bytes(
        raw, SourceMetadata("INBOX", frozenset(), len(raw), None, "/synthetic/cur/child")
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("<a@example.test><b@example.test>", ["a@example.test", "b@example.test"]),
        ("<a@example.test> (comment) <b@example.test>", ["a@example.test", "b@example.test"]),
        ("(ignore <decoy@example.test>) <a@example.test>", ["a@example.test"]),
        ("(nested (ignore <decoy@example.test>)) <a@example.test>", ["a@example.test"]),
        ("<a@example.test>\r\n\t<b@example.test>", ["a@example.test", "b@example.test"]),
        ("a@example.test b@example.test", ["a@example.test", "b@example.test"]),
    ],
)
def test_reference_ids_keep_header_order(value, expected):
    assert _parse("References: " + value).references == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("<a@example.test> (comment)", "a@example.test"),
        ("<a@example.test><b@example.test>", "b@example.test"),
        ("<a@example.test> <b@example.test>", "b@example.test"),
        ("<a@example.test> (ignore <decoy@example.test>)", "a@example.test"),
        ("a@example.test", "a@example.test"),
    ],
)
def test_last_in_reply_to_id_is_the_direct_parent(value, expected):
    assert _parse("In-Reply-To: " + value).in_reply_to == expected


@pytest.mark.parametrize(
    "identifier", ["parent@example.test", "parent@[a>b]", "parent@[a<b]", 'parent@[a"b]']
)
def test_reference_lookup_joins_the_parent_without_subject_fallback(tmp_path, identifier):
    from src.database import EMBEDDING_DIM, Database
    from src.threader import Threader

    from tests.conftest import TEST_ACCOUNT, make_message

    db = Database(tmp_path / "mail.db", account=TEST_ACCOUNT)
    try:
        threader = Threader(db)
        parent = make_message(message_id=identifier, subject="Original topic")
        thread = threader.assign_thread(parent)
        db.upsert_thread(thread, [1.0] + [0.0] * (EMBEDDING_DIM - 1))
        reply = _parse(f"References: <missing@example.test><{identifier}> (comment)")
        assert threader.assign_thread(reply).thread_id == thread.thread_id
    finally:
        db.close()


@pytest.mark.serial
@pytest.mark.parametrize("shape", ["unclosed", "comments", "quoted"])
def test_reply_header_work_is_linear(shape):
    import time

    from src.parser import _reply_ids

    class Counted(str):
        visits = 0

        def __iter__(self):
            for char in super().__iter__():
                self.visits += 1
                yield char

    value = {
        "unclosed": "<" * 100_000,
        "comments": "(" * 50_000 + "<ignored@example.test>" + ")" * 50_000,
        "quoted": '<"' + "\\\\" * 50_000 + '">@example.test>',
    }[shape] + " <kept@example.test>"
    text = Counted(value)
    start = time.monotonic()
    ids = list(_reply_ids(text))
    assert time.monotonic() - start < 2
    assert len(text) <= text.visits <= 2 * len(text)
    assert ids[-1] == "kept@example.test"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("parent@example.test (see <decoy@example.test>)", ["parent@example.test"]),
        ("(see <decoy@example.test>) parent@example.test", ["parent@example.test"]),
        (
            "parent@example.test (nested (see <decoy@example.test>)) later@example.test",
            ["parent@example.test", "later@example.test"],
        ),
        ("<parent@[a>b]>", ["parent@[a>b]"]),
        ("<parent@[a<b]>", ["parent@[a<b]"]),
        ('<parent@[a"b]> <later@example.test>', ['parent@[a"b]', "later@example.test"]),
        ("parent@[a<b]", ["parent@[a<b]"]),
        ("parent@[a(b]", ["parent@[a(b]"]),
    ],
)
def test_delimiters_in_comments_and_domain_literals_are_not_syntax(value, expected):
    msg = _parse("References: " + value + "\r\nIn-Reply-To: " + value)
    assert msg.references == expected
    assert msg.in_reply_to == expected[-1]
