"""#1416 round 5 (owner decision on #1416): container identification
receives an attachment's preserved, transfer-decoded bytes before any
nested-email interpretation. An OLE2 or ZIP payload labelled
``message/*`` is kept as sent, under every transfer encoding, and is a
leaf the walk does not open; a genuine attached email or delivery report
parses exactly as before.

The catalogue (``tests/message_label_catalogue.py``) crosses every
``message/*`` label the parser reads as a container with every transfer
encoding, container and genuine contents, at the top level and inside an
attached email. ``tests/fixtures/message_label_pin.json`` is every
shape's parse before round 5 (``outcome`` over the whole catalogue with
the parser of ``bd31e37``): the genuine shapes must still match it, and
the container shapes show what changed."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

from tests import message_label_catalogue as catalogue

_PIN = json.loads(
    (Path(__file__).parent / "fixtures" / "message_label_pin.json").read_text(encoding="utf-8")
)


def _key(label: str, encoding: str, name: str, nested: bool) -> str:
    return f"{label}|{encoding}|{name}|{'nested' if nested else 'top'}"


def _sha(data: bytes) -> str:
    return catalogue.digest(data)


@pytest.mark.parametrize("nested", [False, True], ids=["top", "nested"])
@pytest.mark.parametrize("name", sorted(catalogue.GENUINE))
@pytest.mark.parametrize("encoding", catalogue.ENCODINGS)
@pytest.mark.parametrize("label", catalogue.LABELS)
def test_genuine_contents_parse_as_pinned(tmp_path, label, encoding, name, nested):
    data = catalogue.message_bytes(label, encoding, catalogue.GENUINE[name], nested=nested)
    got = catalogue.outcome(tmp_path / "m.eml", data)
    assert [list(a) for a in got] == _PIN[_key(label, encoding, name, nested)]


@pytest.mark.parametrize("nested", [False, True], ids=["top", "nested"])
@pytest.mark.parametrize("name", sorted(catalogue.CONTAINERS))
@pytest.mark.parametrize("encoding", catalogue.ENCODINGS)
@pytest.mark.parametrize("label", catalogue.LABELS)
def test_a_container_is_kept_as_sent_and_not_walked(tmp_path, label, encoding, name, nested):
    """The attachment's payload is the container's bytes exactly (so
    identification sees its signature), complete, and nothing inside it
    is recorded as an attachment. Inside an attached email, the outer
    email's own record is unchanged."""
    payload = catalogue.CONTAINERS[name]
    data = catalogue.message_bytes(label, encoding, payload, nested=nested)
    got = catalogue.outcome(tmp_path / "m.eml", data)
    expected = [[label, _sha(payload), True]]
    if nested:
        expected = [_PIN[_key(label, encoding, name, nested)][0], *expected]
    assert [list(a) for a in got] == expected


_DECODED_OUTER = [e for e in catalogue.OUTER_ENCODINGS if e != "none"]


@pytest.mark.parametrize("outer", _DECODED_OUTER)
@pytest.mark.parametrize("name", sorted(catalogue.CONTAINERS))
@pytest.mark.parametrize("encoding", catalogue.ENCODINGS)
@pytest.mark.parametrize("label", catalogue.LABELS)
def test_a_container_in_a_decoded_attached_email_is_kept_as_sent(
    tmp_path, label, encoding, name, outer
):
    """Review round 6 on #1444: the attached email around the part is
    itself base64 or quoted-printable, so the walk reads the part from
    the decoded tree. It is kept as sent all the same, delivery report
    included, and the outer email's record is that of the same email
    sent identity-encoded."""
    payload = catalogue.CONTAINERS[name]
    data = catalogue.message_bytes(label, encoding, payload, nested=True, outer=outer)
    got = catalogue.outcome(tmp_path / "m.eml", data)
    plain = catalogue.message_bytes(label, encoding, payload, nested=True)
    expected = catalogue.outcome(tmp_path / "plain.eml", plain)
    assert [list(a) for a in got] == [list(a) for a in expected]
    assert list(got[-1]) == [label, _sha(payload), True]


@pytest.mark.parametrize("outer", _DECODED_OUTER)
@pytest.mark.parametrize("name", sorted(catalogue.GENUINE))
@pytest.mark.parametrize("encoding", catalogue.ENCODINGS)
@pytest.mark.parametrize("label", catalogue.LABELS)
def test_genuine_contents_in_a_decoded_attached_email_parse_as_identity(
    tmp_path, label, encoding, name, outer
):
    """The same genuine contents parse the same whichever transfer
    encoding the attached email around them is in. A quoted-printable
    email's own serialized payload differs from the identity-encoded
    one's (pre-existing, #1458), so only the parts inside it are compared
    for that encoding."""
    contents = catalogue.GENUINE[name]
    data = catalogue.message_bytes(label, encoding, contents, nested=True, outer=outer)
    plain = catalogue.message_bytes(label, encoding, contents, nested=True)
    got = catalogue.outcome(tmp_path / "m.eml", data)
    expected = catalogue.outcome(tmp_path / "plain.eml", plain)
    if outer == "quoted-printable":
        got, expected = got[1:], expected[1:]
    assert got == expected


def test_the_catalogue_round_trips_its_transfer_encodings():
    """Guards the catalogue: each encoding decodes back to the bytes."""
    import base64
    import quopri

    for payload in (*catalogue.CONTAINERS.values(), *catalogue.GENUINE.values()):
        assert base64.b64decode(catalogue.encode(payload, "base64")) == payload
        assert quopri.decodestring(catalogue.encode(payload, "quoted-printable")) == payload


def test_the_pin_shows_containers_were_not_kept_before():
    """The intended diff: before round 5, no container under a
    ``message/*`` label reached identification as sent."""
    for name, payload in catalogue.CONTAINERS.items():
        for label in catalogue.LABELS:
            for encoding in catalogue.ENCODINGS:
                before = _PIN[_key(label, encoding, name, False)]
                assert [label, _sha(payload), True] not in before


# --------------------------------------------------------------------------
# Loss accounting, the leaf flag and the second parse's work


def _parse(tmp_path, data: bytes):
    from src.parser import parse_email

    path = tmp_path / "m.eml"
    path.write_bytes(data)
    msg = parse_email(path)
    assert msg is not None
    return msg


def test_a_lossy_transport_keeps_the_bytes_and_marks_them_incomplete(tmp_path):
    """The loss accounting of a base64 attached email is kept for a
    container: the lenient decode's bytes reach identification, marked
    incomplete and counted (#1242, #1288)."""
    payload = catalogue.CONTAINERS["zip"]
    encoded = catalogue.encode(payload, "base64")
    lossy = encoded[:8] + b"!!!!" + encoded[8:]
    data = catalogue.message_bytes("message/rfc822", "base64", b"")
    data = data.replace(
        b'filename="SYNTHETIC_FILENAME.bin"\r\n\r\n',
        b'filename="SYNTHETIC_FILENAME.bin"\r\n\r\n' + lossy,
    )
    msg = _parse(tmp_path, data)
    [attachment] = msg.attachments
    assert attachment.payload.startswith(b"PK\x03\x04")
    assert attachment.payload_complete is False
    assert msg.parse_caps == {"transport_lossy": 1}


def test_a_genuine_attached_email_is_still_walked(tmp_path):
    """The leaf flag is set only for a container: an attached email's
    own attachment is still recorded."""
    data = catalogue.message_bytes(
        "message/rfc822", "none", catalogue.GENUINE["email-with-attachment"]
    )
    msg = _parse(tmp_path, data)
    assert [a.content_type for a in msg.attachments] == ["message/rfc822", "application/pdf"]


def test_the_second_parse_runs_once_and_only_for_a_container_report(tmp_path, monkeypatch):
    """The delivery-status re-read parses the message once, whatever the
    number of reports in it, and never for genuine reports."""
    from src import parser

    reads = []
    real = parser._DeliveryStatusBytes._read

    def counting(self):
        reads.append(1)
        return real(self)

    monkeypatch.setattr(parser._DeliveryStatusBytes, "_read", counting)
    genuine = catalogue.message_bytes(
        "message/delivery-status", "none", catalogue.GENUINE["dsn-trailing-blank-lines"]
    )
    msg = _parse(tmp_path, genuine)
    assert msg.attachments[0].payload_complete is True
    assert reads == []

    report = (
        b"--out\r\nContent-Type: message/delivery-status\r\n"
        b'Content-Disposition: attachment; filename="r.bin"\r\n\r\n'
        + catalogue.CONTAINERS["zip-multi-blank"]
        + b"\r\n"
    )
    single = catalogue.message_bytes("message/rfc822", "none", b"")
    head = single.partition(b"--out\r\nContent-Type: message/rfc822")[0]
    msg = _parse(tmp_path, head + report + report + b"--out--\r\n")
    containers = [
        a for a in msg.attachments if a.payload == catalogue.CONTAINERS["zip-multi-blank"]
    ]
    assert len(containers) == 2
    assert reads == [1]


def test_parses_that_do_not_correspond_give_no_bytes():
    """A second parse whose parts do not match the first's, type for
    type, recovers nothing rather than another part's bytes."""
    import email

    from src import parser

    first = email.message_from_bytes(
        catalogue.message_bytes("message/delivery-status", "7bit", catalogue.CONTAINERS["zip"])
    )
    other = catalogue.message_bytes("message/rfc822", "7bit", catalogue.CONTAINERS["zip"])
    report = first.get_payload()[1]
    assert parser._DeliveryStatusBytes(other, first, Counter()).get(report) is None


def test_the_re_read_takes_at_most_the_walk_cap_of_a_tree():
    """Review round 6 on #1444: enumerating a tree for the re-read stops
    after ``limit`` parts, without taking a container's children all at
    once."""
    import email.message

    from src import parser

    taken = []

    class Counting(list):
        def __iter__(self):
            for item in list.__iter__(self):
                taken.append(1)
                yield item

    root = email.message.Message()
    root.set_payload(Counting(email.message.Message() for _ in range(100_000)))
    assert parser._parts_in_order(root, 50) is None
    assert len(taken) <= 50
    assert len(parser._parts_in_order(root, 100_001) or []) == 100_001


def test_a_tree_over_the_walk_cap_is_not_re_read(tmp_path, monkeypatch, caplog):
    """Review round 6 on #1444: a message of more than ``MAX_WALKED_PARTS``
    parts whose delivery report holds a container is not parsed a second
    time; the lost recovery is counted and logged by name, and the walk
    still ends in bounded time."""
    import email as email_module
    import time

    from src import parser

    second_parses = []
    real = email_module.message_from_bytes

    def counting(data, *args, **kwargs):
        if kwargs.get("policy") is parser._DELIVERY_STATUS_AS_LEAF:
            second_parses.append(1)
        return real(data, *args, **kwargs)

    monkeypatch.setattr(parser.email, "message_from_bytes", counting)
    report = (
        b"--out\r\nContent-Type: message/delivery-status\r\n"
        b'Content-Disposition: attachment; filename="SYNTHETIC_FILENAME.bin"\r\n\r\n'
        + catalogue.CONTAINERS["zip"]
        + b"\r\n"
    )
    head = catalogue.message_bytes("message/rfc822", "none", b"").partition(
        b"--out\r\nContent-Type: message/rfc822"
    )[0]
    filler = b"--out\r\nContent-Type: application/x-empty\r\n\r\n\r\n" * parser.MAX_WALKED_PARTS
    caplog.set_level("INFO")
    started = time.monotonic()
    msg = _parse(tmp_path, head + report + filler + b"--out--\r\n")
    assert time.monotonic() - started < 10
    assert second_parses == []
    assert msg.parse_caps == {"mime_parts": 1, "delivery_status_parts": 1}
    assert all(a.payload != catalogue.CONTAINERS["zip"] for a in msg.attachments)
    assert "delivery_status_parts=1" in caplog.text
    assert "SYNTHETIC_FILENAME" not in caplog.text
    # Within the cap, the same report is recovered by one second parse.
    msg = _parse(tmp_path, head + report + b"--out--\r\n")
    assert second_parses == [1]
    assert [a.payload for a in msg.attachments] == [catalogue.CONTAINERS["zip"]]


# --------------------------------------------------------------------------
# The reparse cascade for a changed attachment ID


def test_the_reparse_replaces_the_old_occurrence_and_embeds_the_new_text(tmp_path, monkeypatch):
    """An occurrence indexed before round 5 (its payload the serialized
    email) has another attachment ID after the reparse: its row, FTS
    row, chunks and vectors go, and the container's text is extracted and
    embedded once (#1375, #1416)."""
    from src import attachment_indexing, parser
    from src.extractors import CONTAINER_IDENTIFIER, ExtractionResult, has_container_prefix

    from tests.test_stale_occurrences import StalePipeline

    p = StalePipeline(tmp_path, monkeypatch)

    def fake_extract(*, payload, **_kwargs):
        words = " ".join(["synthetic", "container", "words"] * 10)
        return ExtractionResult(
            status="success",
            extractor="docx@7" if has_container_prefix(payload) else "eml@3",
            text=f"{hashlib.sha256(payload).hexdigest()} {words}",
            error=None,
            text_complete=True,
            identifier=CONTAINER_IDENTIFIER if has_container_prefix(payload) else "",
        )

    monkeypatch.setattr(attachment_indexing, "extract_attachment", fake_extract)
    payload = catalogue.CONTAINERS["zip"]
    path = p.maildir / "INBOX" / "cur" / "m.eml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(catalogue.message_bytes("message/rfc822", "7bit", payload))

    # Before round 5: the parser serialized the container as an email.
    with monkeypatch.context() as before:
        before.setattr(parser, "_identity_container_bytes", lambda *_a: None)
        p.queue.enqueue(str(path), "initial_scan")
        p.drain()
    [(_claimant, old_id, _name)] = p.rows()
    assert old_id != hashlib.sha256(payload).hexdigest()
    old_chunks = p.slices()[(_claimant, old_id)]
    assert old_chunks <= p.vec_ids()
    p.fts_rowids()
    p.embedder.embed_batch.reset_mock()

    p.reparse(str(path))

    [(claimant, new_id, _name)] = p.rows()
    assert new_id == hashlib.sha256(payload).hexdigest()
    slices = p.slices()
    assert set(slices) == {(claimant, new_id)}
    assert not old_chunks & p.vec_ids()
    assert slices[(claimant, new_id)] <= p.vec_ids()
    assert len(p.fts_rowids()) == 1
    embedded = [t for call in p.embedder.embed_batch.call_args_list for t in call.args[0]]
    assert any(hashlib.sha256(payload).hexdigest() in text for text in embedded)
