"""RFC 2387 roots use the declared Content-ID without promoting resources."""

import pytest
from src import parser
from src.extractors import eml


def _related(
    start='; start="<root@example.test>"',
    *,
    resource_id="res@example.test",
    duplicate=False,
    attachment=False,
):
    disposition = b"Content-Disposition: attachment; filename=root.txt\r\n" if attachment else b""
    return (
        b"Message-ID: <related@example.test>\r\nSubject: synthetic\r\n"
        b"Date: Mon, 28 Sep 2026 12:00:00 +0000\r\nFrom: sender@example.test\r\n"
        b"Content-Type: multipart/related; boundary=B" + start.encode() + b"\r\n\r\n"
        b"--B\r\nContent-Type: text/plain\r\nContent-ID: <"
        + resource_id.encode()
        + b">\r\n\r\nRESOURCE\r\n"
        b"--B\r\nContent-Type: text/plain\r\nContent-ID: <root@example.test>\r\n"
        + (b"Content-ID: <root@example.test>\r\n" if duplicate else b"")
        + disposition
        + b"\r\nROOT\r\n--B--\r\n"
    )


def _parse(raw):
    source = parser.SourceMetadata(
        folder="INBOX",
        flags=frozenset(),
        size=len(raw),
        mtime_ns=0,
        path="/maildir/INBOX/cur/synthetic-related",
    )
    message = parser.parse_email_bytes(raw, source)
    assert message is not None
    return message


@pytest.mark.parametrize(
    "kwargs, body, complete",
    [
        ({}, "ROOT", True),
        ({"start": ""}, "RESOURCE", True),
        ({"start": '; start="<missing@example.test>"'}, "", False),
        ({"resource_id": "root@example.test"}, "", False),
        ({"duplicate": True}, "", False),
        ({"start": '; start=""'}, "", False),
        ({"start": '; start="<root@example.test>"; start="<res@example.test>"'}, "", False),
        ({"attachment": True}, "", True),
    ],
    ids=[
        "second-root",
        "default-first",
        "missing-root",
        "duplicate-root",
        "duplicate-cid",
        "empty-start",
        "duplicate-start",
        "attached-root",
    ],
)
def test_related_start_root_and_completeness(kwargs, body, complete, caplog):
    raw = _related(**kwargs)
    message = _parse(raw)
    assert message.body_text == body
    assert message.body_complete is complete
    assert "ROOT" not in caplog.text and "RESOURCE" not in caplog.text
    text, caps = eml.extract_text(raw)
    if body:
        assert text.split("\n\n", 1)[-1] == body
    else:
        assert "RESOURCE" not in text and "ROOT" not in text
    assert ("eml_body_structure" in caps) is (not complete)


def test_related_root_can_be_an_alternative_container():
    raw = _related().replace(
        b"Content-Type: text/plain\r\nContent-ID: <root@example.test>\r\n\r\nROOT",
        b"Content-Type: multipart/alternative; boundary=A\r\nContent-ID: <root@example.test>\r\n\r\n"
        b"--A\r\nContent-Type: text/html\r\n\r\n<p>HTML</p>\r\n"
        b"--A\r\nContent-Type: text/plain\r\n\r\nROOT\r\n--A--",
    )
    assert _parse(raw).body_text == "ROOT"


def test_ambiguous_resource_id_cannot_hide_a_duplicate_root():
    raw = _related(resource_id="root@example.test").replace(
        b"Content-ID: <root@example.test>\r\n\r\nRESOURCE",
        b"Content-ID: <root@example.test>\r\nContent-ID: <other@example.test>\r\n\r\nRESOURCE",
    )
    message = _parse(raw)
    assert message.body_text == ""
    assert message.body_complete is False


def test_inline_nested_message_can_be_the_named_related_root():
    raw = _related().replace(
        b"Content-Type: text/plain\r\nContent-ID: <root@example.test>\r\n\r\nROOT",
        b"Content-Type: message/rfc822\r\nContent-ID: <root@example.test>\r\n\r\n"
        b"Subject: nested\r\nContent-Type: text/plain\r\n\r\nROOT",
    )
    text, caps = eml.extract_text(raw)
    assert "ROOT" in text and "RESOURCE" not in text
    assert not caps


def test_cut_related_child_walk_does_not_assume_a_unique_explicit_root(monkeypatch):
    monkeypatch.setattr(parser, "MAX_WALKED_PARTS", 2)
    raw = _related('; start="<res@example.test>"')
    message = _parse(raw)
    assert message.body_text == ""
    assert message.body_complete is False


def test_related_header_budget_stops_before_parameter_parsing(monkeypatch):
    import email

    part = email.message_from_bytes(
        _related().replace(
            b"\r\n\r\n--B", b"\r\n" + b"X-Synthetic: value\r\n" * 10000 + b"\r\n--B", 1
        )
    )
    visited = []
    original = part.raw_items

    def items():
        for pair in original():
            visited.append(1)
            yield pair

    monkeypatch.setattr(part, "raw_items", items)
    calls = []
    original_params = email.message.Message.get_params

    def params(self, *args, **kwargs):
        calls.append(1)
        return original_params(self, *args, **kwargs)

    monkeypatch.setattr(email.message.Message, "get_params", params)
    remaining = [32]
    fields = parser._related_fields(part, remaining)
    assert fields is None
    assert parser._related_start(fields) == ""
    assert len(visited) <= 33
    assert calls == []


@pytest.mark.parametrize("valid", [True, False])
def test_body_choice_and_unknown_capture_survive_database_write(tmp_path, db, threader, valid):
    from tests.conftest import make_mock_embedder
    from tests.test_database import FAKE_EMBEDDING
    from tests.test_main import _index_one

    raw = _related() if valid else _related('; start="<missing@example.test>"')
    path = tmp_path / "INBOX" / "cur" / "synthetic-related"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    succeeded, stage, error = _index_one(path, db, make_mock_embedder(FAKE_EMBEDDING), threader)
    assert succeeded, (stage, error)
    assert db._conn.execute("SELECT body_complete FROM messages").fetchone()[0] == int(valid)
    assert db._conn.execute(
        "SELECT COUNT(*) FROM message_chunks_fts WHERE message_chunks_fts MATCH 'ROOT'"
    ).fetchone()[0] == int(valid)
    assert (
        db._conn.execute(
            "SELECT COUNT(*) FROM message_chunks_fts WHERE message_chunks_fts MATCH 'RESOURCE'"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.serial
def test_related_parameter_work_spends_one_shared_budget(monkeypatch):
    import time

    from tests.test_eml_extractor import _multipart

    params = "; ".join(f"x{index}=value" for index in range(900))
    part = _related().replace(b"boundary=B", b"boundary=B; " + params.encode())
    raw = _multipart(*[part] * 20, boundary=b"BOUND")
    parsed = []
    original = parser._related_start

    def count(fields):
        parsed.append(fields is not None)
        return original(fields)

    monkeypatch.setattr(parser, "_related_start", count)
    started = time.perf_counter()
    text, caps = eml.extract_text(raw)
    assert time.perf_counter() - started < 2
    assert 0 < text.count("ROOT") < 20
    assert "RESOURCE" not in text
    assert caps == ["eml_body_structure"]
    assert len(parsed) == 20
    assert sum(parsed) < 20


@pytest.mark.parametrize(
    "start",
    [
        '; START="<root@example.test>"',
        "; start*=utf-8''%3Croot%40example.test%3E",
    ],
)
def test_start_parameter_forms_use_the_standard_library_decoder(start):
    message = _parse(_related(start))
    assert message.body_text == "ROOT" and message.body_complete


def test_nonfirst_text_root_is_not_lost_behind_an_image_resource():
    raw = _related().replace(b"Content-Type: text/plain", b"Content-Type: image/png", 1)
    message = _parse(raw)
    assert message.body_text == "ROOT" and message.body_complete
