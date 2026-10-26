"""Attached email files expose their child attachments without promoting inner authors."""

import email
import hashlib
from email.message import EmailMessage

import pytest
from src import parser


def _message():
    msg = EmailMessage()
    msg["Message-ID"] = "<synthetic-outer@test.example>"
    msg["From"] = "outer@test.example"
    msg["To"] = "recipient@test.example"
    msg.set_content("Synthetic outer body")
    return msg


def _parse(msg):
    raw = msg.as_bytes()
    source = parser.SourceMetadata(
        folder="INBOX", flags=frozenset(), size=len(raw), mtime_ns=0, path="synthetic"
    )
    return parser.parse_email_bytes(raw, source)


def test_leaf_eml_child_precedes_the_next_outer_attachment():
    inner = _message()
    inner.replace_header("From", "inner-claim@test.example")
    payload = b"Synthetic child evidence"
    inner.add_attachment(
        payload, maintype="application", subtype="octet-stream", filename="child.txt"
    )
    outer = _message()
    outer.add_attachment(
        inner.as_bytes(), maintype="application", subtype="eml", filename="mail.eml"
    )
    outer.add_attachment(b"later", maintype="text", subtype="plain", filename="later.txt")
    parsed = _parse(outer)
    assert [a.filename for a in parsed.attachments] == ["mail.eml", "child.txt", "later.txt"]
    child = parsed.attachments[1]
    assert child.payload == payload and child.payload_complete
    assert child.content_hash == hashlib.sha256(payload).hexdigest()
    assert parsed.attachments_manifest_complete
    assert parsed.from_addr == "outer@test.example"
    assert "inner-claim@test.example" not in parsed.from_addrs


@pytest.mark.parametrize(
    "shape", ["plain", "multipart", "empty", "folded", "8bit", "native", "nested"]
)
def test_existing_root_mime_walk_catalogue(shape):
    """Pin the unchanged root walk before adding child-only expansion."""
    outer = _message()
    expected = []
    if shape == "empty":
        outer = EmailMessage()
    elif shape == "8bit":
        outer.set_content("Synthetic café body")
    elif shape == "folded":
        outer["Subject"] = "Synthetic " * 25
    elif shape in {"multipart", "native", "nested"}:
        inner = _message()
        inner.add_attachment(b"child", maintype="text", subtype="plain", filename="child.txt")
        if shape == "native":
            outer.add_attachment(inner, filename="native.eml")
            expected = ["native.eml", "child.txt"]
        else:
            if shape == "nested":
                parent = _message()
                parent.add_attachment(
                    inner.as_bytes(), maintype="application", subtype="eml", filename="inner.eml"
                )
                inner = parent
            outer.add_attachment(
                inner.as_bytes(), maintype="application", subtype="eml", filename="mail.eml"
            )
            expected = ["mail.eml"]
    raw = outer.as_bytes()
    _, attachments = parser._extract_body_and_attachments(email.message_from_bytes(raw), raw=raw)
    assert [a.filename for a in attachments] == expected


def _attached(children=1, *, native=False):
    inner = _message()
    for n in range(children):
        inner.add_attachment(
            b"Synthetic descendant text", maintype="text", subtype="plain", filename=f"child{n}.txt"
        )
    outer = _message()
    if native:
        outer.add_attachment(inner, filename="mail.eml")
    else:
        outer.add_attachment(
            inner.as_bytes(), maintype="application", subtype="eml", filename="mail.eml"
        )
    return outer


def test_nested_leaf_and_native_containers_keep_depth_first_occurrences():
    nested = _attached(native=True)
    outer = _message()
    outer.add_attachment(
        nested.as_bytes(), maintype="application", subtype="octet-stream", filename="leaf.EML"
    )
    outer.add_attachment(b"later", maintype="text", subtype="plain", filename="later.txt")
    parsed = _parse(outer)
    assert [a.filename for a in parsed.attachments] == [
        "leaf.EML",
        "mail.eml",
        "child0.txt",
        "later.txt",
    ]
    assert parsed.attachments_manifest_complete
    assert parsed.attachments[2].payload == b"Synthetic descendant text"


def test_nested_leaf_file_is_recursively_expanded_once():
    parsed = _parse(_attached())
    inner = _attached()
    outer = _message()
    outer.add_attachment(
        inner.as_bytes(), maintype="application", subtype="eml", filename="parent.eml"
    )
    actual = _parse(outer)
    assert [a.filename for a in actual.attachments] == ["parent.eml", "mail.eml", "child0.txt"]
    assert actual.attachments[-1].content_hash == parsed.attachments[-1].content_hash
    assert actual.attachments_manifest_complete


def test_budget_is_shared_between_sibling_children(monkeypatch):
    from src.extractors import eml_descendants as expansion

    real = expansion._runner.run_child
    grants = []

    def run(*args, **kwargs):
        grants.append(list(map(int, kwargs["options"][1:])))
        return real(*args, **kwargs)

    monkeypatch.setattr(expansion._runner, "run_child", run)
    outer = _message()
    for n in range(2):
        outer.add_attachment(
            _attached().as_bytes(), maintype="application", subtype="eml", filename=f"{n}.eml"
        )
    parsed = _parse(outer)
    assert parsed.attachments_manifest_complete
    assert len(grants) == 2
    for key in (
        "parse_bytes",
        "decode_bytes",
        "parts",
        "headers",
        "header_bytes",
        "occurrences",
        "output_bytes",
    ):
        i = list(expansion.LIMITS).index(key)
        assert grants[1][i] < grants[0][i], key


@pytest.mark.parametrize(
    "dimension",
    [
        "parse_bytes",
        "decode_bytes",
        "parts",
        "headers",
        "header_bytes",
        "occurrences",
        "output_bytes",
    ],
)
def test_every_work_dimension_can_cut_a_child(dimension):
    from src.extractors._runner import _Frames
    from src.extractors.eml_descendants import LIMITS, expand_payload

    limits = dict(LIMITS)
    limits[dimension] = 0
    data, caps, counts = expand_payload(
        _attached().as_bytes(), "1", *(str(n) for n in limits.values())
    )
    assert caps == ["eml_expansion"]
    assert counts[dimension] == 0
    assert all(0 <= counts[k] <= limits[k] for k in limits)
    frames = _Frames(frozenset(), frozenset(), None, attachment_limit=10)
    frames.feed(data + b"T 0\n")
    assert not frames.result().attachments


def test_launch_cap_preserves_outer_siblings_and_marks_manifest(monkeypatch, caplog):
    from src.extractors import eml_descendants as expansion

    monkeypatch.setattr(expansion, "MAX_CHILDREN", 1)
    outer = _message()
    for n in range(2):
        outer.add_attachment(
            _attached().as_bytes(), maintype="application", subtype="eml", filename=f"{n}.eml"
        )
    outer.add_attachment(b"last", maintype="text", subtype="plain", filename="last.txt")
    parsed = _parse(outer)
    assert [a.filename for a in parsed.attachments] == [
        "0.eml",
        "mail.eml",
        "child0.txt",
        "1.eml",
        "last.txt",
    ]
    assert not parsed.attachments_manifest_complete
    assert parsed.body_complete and parsed.full_body.complete
    assert parsed.parse_caps["eml_expansion"] == 1
    assert "manifest incomplete" in caplog.text
    assert "Synthetic descendant text" not in caplog.text


@pytest.mark.parametrize("failure", ["timeout", "output", "error", "exit", "crash", "missing"])
def test_failed_child_stops_further_launches_and_withholds_content(failure, monkeypatch, caplog):
    from src.extractors import _runner
    from src.extractors import eml_descendants as expansion

    failures = {
        "timeout": _runner.ToolTimeoutError,
        "output": _runner.ChildOutputError,
        "error": lambda: _runner.ChildError("ValueError"),
        "exit": _runner.ToolExitError,
        "crash": _runner.ToolCrashError,
        "missing": _runner.ToolNotFoundError,
    }
    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise failures[failure]()

    monkeypatch.setattr(expansion._runner, "run_child", fail)
    outer = _message()
    for n in range(2):
        outer.add_attachment(
            _attached().as_bytes(), maintype="application", subtype="eml", filename=f"{n}.eml"
        )
    parsed = _parse(outer)
    assert len(calls) == 1
    assert [a.filename for a in parsed.attachments] == ["0.eml", "1.eml"]
    assert not parsed.attachments_manifest_complete
    assert "manifest incomplete" in caplog.text
    assert "Synthetic descendant text" not in caplog.text


def test_malformed_structure_is_unknown_not_a_complete_empty_manifest():
    outer = _message()
    outer.add_attachment(
        b"Content-Type: multipart/mixed; boundary=missing\n\nsynthetic",
        maintype="application",
        subtype="eml",
        filename="bad.eml",
    )
    parsed = _parse(outer)
    assert not parsed.attachments_manifest_complete


@pytest.mark.parametrize(
    "content_type", ["message/rfc822", "message/global", "message/news", "message/x-synthetic"]
)
def test_depth_cap_is_shared_with_native_ancestors(monkeypatch, content_type):
    from src.extractors import eml_descendants as expansion

    outer = _attached()
    for _ in range(20):
        parent = _message()
        parent.add_attachment(outer, filename="native.eml")
        parent.get_payload()[-1].set_type(content_type)
        outer = parent
    called = []

    def must_not_launch(*args, **kwargs):
        called.append(args)
        raise AssertionError("depth-capped expansion must not launch a child")

    monkeypatch.setattr(expansion._runner, "run_child", must_not_launch)
    parsed = _parse(outer)
    assert not called
    assert not parsed.attachments_manifest_complete


def test_expansion_migration_queues_existing_sources():
    from pathlib import Path

    from src.database import SCHEMA_VERSION
    from src.queue import REPARSE_ENQUEUE_SQL

    migration = Path(parser.__file__).parent / "migrations" / "0029_eml_descendants.sql"
    assert SCHEMA_VERSION == 29
    assert migration.read_text().endswith(REPARSE_ENQUEUE_SQL + "\n")


@pytest.mark.parametrize(
    "shape", ["plain", "multipart", "empty", "folded", "8bit", "native", "nested"]
)
def test_file_descendants_match_native_message_walk_catalogue(shape):
    inner = _message()
    if shape == "empty":
        inner = EmailMessage()
    elif shape == "8bit":
        inner.set_content("Synthetic café body")
    elif shape == "folded":
        inner["Subject"] = "Synthetic long folded subject " * 50
    if shape in {"multipart", "folded", "8bit"}:
        inner.add_attachment(
            b"\x00\xff\nSynthetic file",
            maintype="application",
            subtype="octet-stream",
            filename="child.txt",
        )
    elif shape in {"native", "nested"}:
        child = _attached()
        if shape == "native":
            inner.add_attachment(child, filename="native.eml")
        else:
            inner.add_attachment(
                child.as_bytes(), maintype="application", subtype="eml", filename="nested.eml"
            )
    as_file, as_native = _message(), _message()
    as_file.add_attachment(
        inner.as_bytes(), maintype="application", subtype="eml", filename="mail.eml"
    )
    as_native.add_attachment(inner, filename="mail.eml")
    file_result, native_result = _parse(as_file), _parse(as_native)

    def describe(a):
        return a.filename, a.content_type, a.payload, a.payload_complete

    assert list(map(describe, file_result.attachments[1:])) == list(
        map(describe, native_result.attachments[1:])
    )
    assert file_result.attachments_manifest_complete == native_result.attachments_manifest_complete


def test_unknown_child_work_is_rejected_without_crediting_allowance(monkeypatch):
    from src.extractors import eml_descendants as expansion
    from src.extractors._runner import ChildResult

    calls = []

    def forged(*args, **kwargs):
        calls.append(args)
        return ChildResult("", [], dict.fromkeys(expansion.LIMITS, 0))

    monkeypatch.setattr(expansion._runner, "run_child", forged)
    parsed = _parse(_attached())
    assert len(calls) == 1
    assert not parsed.attachments_manifest_complete
    assert len(parsed.attachments) == 1


def test_native_transport_preflight_and_secondary_parse_consume_the_shared_ledger():
    import base64

    from src.extractors.eml_descendants import LIMITS, expand_payload

    inner = _attached().as_bytes()
    wrapper = (
        b'Content-Type: message/rfc822\nContent-Disposition: attachment; filename="native.eml"\n'
        b"Content-Transfer-Encoding: base64\n\n" + base64.encodebytes(inner)
    )
    wire, caps, counts = expand_payload(wrapper, "1", *(str(n) for n in LIMITS.values()))
    assert not caps
    assert counts["parse_bytes"] > len(wrapper) + len(inner)  # secondary transport-preserving parse
    assert counts["decode_bytes"] >= len(base64.encodebytes(inner))
    assert counts["parts"] > 5  # preflight and secondary walks count too
    assert b"child0.txt" in wire


def test_metadata_expansion_is_capped_before_framing_oversized_unicode():
    from src.extractors.eml_descendants import ExpansionBudget

    attachment = parser.Attachment(
        "😀" * 6000, "text/plain", 1, b"x", hashlib.sha256(b"x").hexdigest()
    )
    work = ExpansionBudget()
    assert not work.keep(attachment)


@pytest.mark.parametrize("content_type", ["multipart/mixed", "message/global", "message/rfc822"])
def test_native_container_named_eml_is_not_expanded_twice(content_type):
    inner = _message()
    inner.add_attachment(b"synthetic child", maintype="text", subtype="plain", filename="child.txt")
    if content_type.startswith("message/"):
        container = EmailMessage()
        container.set_type(content_type)
        container.set_payload([inner])
    else:
        container = inner
    container["Content-Disposition"] = 'attachment; filename="container.eml"'
    outer = _message()
    outer.make_mixed()
    outer.attach(container)
    parsed = _parse(outer)
    assert [a.filename for a in parsed.attachments] == ["container.eml", "child.txt"]
    assert parsed.attachments_manifest_complete


@pytest.mark.parametrize("content_type", ["message/rfc822", "message/delivery-status"])
def test_binary_native_recovery_is_charged_to_decode_budget(content_type, monkeypatch):
    from src.extractors.eml_descendants import LIMITS, expand_payload

    from tests.conftest import make_zip

    payload = make_zip("synthetic.txt", contents=b"Synthetic recovered bytes")
    raw = (
        f'Content-Type: {content_type}\nContent-Disposition: attachment; filename="recovered.zip"\n\n'.encode()
        + payload
    )
    wire, caps, counts = expand_payload(raw, "1", *(str(n) for n in LIMITS.values()))
    assert counts["decode_bytes"] >= len(payload)
    assert counts["output_bytes"] == len(wire)

    # Refuse before constructing a decoded bytes copy, not after the fact.
    calls = []
    real_decode = parser._decoded_payload

    def decode(part):
        calls.append(part)
        return real_decode(part)

    monkeypatch.setattr(parser, "_decoded_payload", decode)
    limits = dict(LIMITS, decode_bytes=0)
    wire, caps, counts = expand_payload(raw, "1", *(str(n) for n in limits.values()))
    assert caps == ["eml_expansion"]
    assert counts["decode_bytes"] == 0
    assert not calls
    assert payload not in wire


def test_native_reparse_charges_the_synthetic_content_type_header(monkeypatch):
    import base64

    from src import parser
    from src.extractors.eml_descendants import LIMITS, expand_payload

    inner = b"From: synthetic@test.example\n\nbody"
    transport = base64.encodebytes(inner)
    raw = (
        b"Content-Type: message/rfc822\nContent-Transfer-Encoding: base64\nContent-Disposition: attachment\n\n"
        + transport
    )
    _, _, counts = expand_payload(raw, "1", *(str(n) for n in LIMITS.values()))
    assert counts["parse_bytes"] >= len(raw) * 2 + len(
        parser._transport_form_bytes(inner, "message/rfc822")
    )

    calls = []
    real_parse = parser._parse_transport_form

    def parse(*args):
        calls.append(args)
        return real_parse(*args)

    monkeypatch.setattr(parser, "_parse_transport_form", parse)
    limits = dict(LIMITS, parse_bytes=len(raw) * 2 + len(transport))
    _, caps, _ = expand_payload(raw, "1", *(str(n) for n in limits.values()))
    assert caps == ["eml_expansion"]
    assert not calls
