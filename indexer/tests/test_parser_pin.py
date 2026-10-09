"""#1077: the ``Message`` every parser fixture produces, pinned.

Written against ``main`` before ``parse_email`` was split into
``parse_email_bytes`` and the Maildir adapter, so the split can be shown
output-identical. The catalogue is every raw message the indexer tests
build for the parser: the ``.eml`` files under ``fixtures/chunker``, the
synthetic baseline corpus (``tests/baseline/corpus.py``) and the shape
catalogues ``tests/test_parser.py`` keeps at module level. Each is
written to a Maildir under ``tmp_path``, parsed with ``parse_email`` and
recorded field by field; the snapshot lives in
``fixtures/parser/parse_email_pin.json``.

Fields that vary between runs are recorded in a stable form: the
``date`` of a message without a usable ``Date:`` header (the parser's
current-time fallback) as ``None``, ``filepath`` relative to the
message's Maildir root and ``mtime_ns`` as whether it was captured.
The body text and each attachment payload are recorded as a length
and a SHA-256, which keeps the snapshot small; that and the file's
``content_hash`` are written as ``sha256:<hex>`` digests, which the
pre-commit scanner for leaked keys does not flag. One record per line,
so a change shows as that message's line.

A deliberate parser change regenerates the snapshot with
``PARSER_PIN_UPDATE=1 uv run pytest tests/test_parser_pin.py`` and the
PR explains the diff, as for ``make baseline UPDATE=1``. So does adding
a corpus message or a shape, since the catalogue grows with them: the
regenerated file then differs only by the added records, plus the
corpus messages after an insertion or removal renumbered under new
file names with no other field changed (#1124).
"""

import hashlib
import json
import os
import shutil
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest
from src.parser import Message, parse_email

from tests import test_parser as tp
from tests.baseline.corpus import write_maildir

_FIXTURES = Path(__file__).parent / "fixtures"
_PIN = _FIXTURES / "parser" / "parse_email_pin.json"
_CHUNKER_EMLS = sorted((_FIXTURES / "chunker").glob("*.eml"))

# ``Message`` fields recorded as they are. The rest are recorded in a
# stable form by ``_record``; ``test_every_message_field_is_recorded``
# fails when a new field is in neither list.
_VERBATIM_FIELDS = (
    "message_id",
    "in_reply_to",
    "references",
    "subject",
    "from_addr",
    "from_addrs",
    "to_addrs",
    "cc_addrs",
    "folder",
    "has_attachments",
    "size",
    "date_status",
    "sender_ambiguous",
    "participant_names_complete",
    "subject_complete",
    "from_addresses_complete",
    "to_addresses_complete",
    "cc_addresses_complete",
    "attachments_manifest_complete",
    "body_complete",
    "parse_caps",
)
_DERIVED_FIELDS = (
    "date",
    "occurred_at",
    "body_text",
    "filepath",
    "attachments",
    "mtime_ns",
    "content_hash",
    "participant_names",
)
# Not recorded: the parse time (#1080), a clock reading rather than a
# property of the bytes.
_UNPINNED_FIELDS = ("first_indexed_at",)


def _digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _record(msg: Message, root: Path) -> dict[str, Any]:
    record: dict[str, Any] = {name: getattr(msg, name) for name in _VERBATIM_FIELDS}
    record["date"] = msg.date.isoformat() if msg.date else None
    record["occurred_at"] = msg.occurred_at.isoformat() if msg.occurred_at else None
    record["body_text_length"] = len(msg.body_text)
    record["body_text"] = _digest(msg.body_text.encode("utf-8"))
    record["filepath"] = Path(msg.filepath).relative_to(root).as_posix()
    record["mtime_ns_captured"] = msg.mtime_ns is not None
    record["content_hash"] = f"sha256:{msg.content_hash}"
    # ``(role, address, name)`` rows, as JSON lists (#1140).
    record["participant_names"] = [list(row) for row in msg.participant_names or []]
    record["attachments"] = [
        {
            "filename": a.filename,
            "content_type": a.content_type,
            "size": a.size,
            "content_hash": f"sha256:{a.content_hash}",
            "payload_length": len(a.payload),
            "payload": _digest(a.payload),
            "payload_complete": a.payload_complete,
        }
        for a in msg.attachments
    ]
    return record


def _maildir(tmp_path: Path, *key: str) -> Path:
    """A fresh Maildir root for one catalogue entry."""
    root = tmp_path.joinpath(*key)
    root.mkdir(parents=True)
    return root


def _parse_raw(tmp_path: Path, group: str, name: str, raw: bytes) -> tuple[Path, Message]:
    root = _maildir(tmp_path, group, name)
    path = root / "INBOX" / "cur" / "m.eml"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    msg = parse_email(path, maildir_root=root)
    assert msg is not None
    return root, msg


def _catalogue(tmp_path: Path) -> dict[str, dict[str, Any]]:
    """Parse every fixture; the record of each under a stable key."""
    records: dict[str, dict[str, Any]] = {}

    # The committed ``.eml`` files, under a flagged Maildir name.
    for eml in _CHUNKER_EMLS:
        root = _maildir(tmp_path, "chunker", eml.stem)
        path = root / "INBOX" / "cur" / f"{eml.stem}:2,S"
        path.parent.mkdir(parents=True)
        shutil.copyfile(eml, path)
        msg = parse_email(path, maildir_root=root)
        assert msg is not None
        records[f"chunker/{eml.name}"] = _record(msg, root)

    # The synthetic baseline corpus, in its own folders.
    root = _maildir(tmp_path, "corpus")
    write_maildir(root)
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        msg = parse_email(path, maildir_root=root)
        assert msg is not None
        records[f"corpus/{path.relative_to(root).as_posix()}"] = _record(msg, root)

    # The shape catalogues of tests/test_parser.py, built as their own
    # tests build them.
    for shape in tp._CAP_SHAPES:
        with pytest.MonkeyPatch.context() as mp:
            root = _maildir(tmp_path, "caps", shape)
            msg, _ = tp._parse_cap_shape(root, mp, shape)
        records[f"caps/{shape}"] = _record(msg, root)
    for shape, (raw, _) in tp._NO_LOSS_SHAPES.items():
        root, msg = _parse_raw(tmp_path, "no_loss", shape, raw)
        records[f"no_loss/{shape}"] = _record(msg, root)
    for shape in tp._BODY_CAP_SHAPES:
        root = _maildir(tmp_path, "body_caps", shape)
        msg, _ = tp._parse_body_cap_shape(root, shape)
        records[f"body_caps/{shape}"] = _record(msg, root)
    for shape, (raw, _, _) in tp._ORDINARY_SHAPES.items():
        root, msg = _parse_raw(tmp_path, "ordinary", shape, tp._CAP_HEAD + raw)
        records[f"ordinary/{shape}"] = _record(msg, root)
    for group, catalogue, message_id in (
        ("body", tp._BODY_SHAPES, "shape"),
        ("related", tp._RELATED_SHAPES, "related"),
    ):
        for shape, (tree, _) in catalogue.items():
            raw = (
                f"Message-ID: <{message_id}@example.test>\r\nFrom: sender@example.test\r\n"
                "Date: Mon, 28 Sep 2026 12:00:00 +0000\r\nMIME-Version: 1.0\r\n"
            ) + tp._render(tree, [0])
            root, msg = _parse_raw(tmp_path, group, shape, raw.encode("utf-8"))
            records[f"{group}/{shape}"] = _record(msg, root)
    for shape, (headers, _) in tp._FILENAME_SHAPES.items():
        root = _maildir(tmp_path, "filename", shape)
        msg = parse_email(tp._write_filename_message(root, headers), maildir_root=root)
        assert msg is not None
        records[f"filename/{shape}"] = _record(msg, root)
    for shape, (template, _) in tp._FOLDED_ADDRESS_SHAPES.items():
        for eol, eol_name in (("\r\n", "crlf"), ("\n", "lf")):
            root = _maildir(tmp_path, "folded", shape, eol_name)
            path = tp._folded_address_eml(root, template.format(eol=eol), eol)
            msg = parse_email(path, maildir_root=root)
            assert msg is not None
            records[f"folded/{shape}/{eol_name}"] = _record(msg, root)
    return records


def test_every_message_field_is_recorded():
    """A ``Message`` field this pin does not cover is a gap in it."""
    covered = set(_VERBATIM_FIELDS) | set(_DERIVED_FIELDS) | set(_UNPINNED_FIELDS)
    assert covered == {f.name for f in fields(Message)}


def test_parse_email_output_is_pinned(tmp_path):
    records = _catalogue(tmp_path)
    if os.environ.get("PARSER_PIN_UPDATE") == "1":
        lines = (
            f" {json.dumps(key)}: {json.dumps(records[key], sort_keys=True, ensure_ascii=False)}"
            for key in sorted(records)
        )
        _PIN.parent.mkdir(parents=True, exist_ok=True)
        _PIN.write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding="utf-8")
    pinned = json.loads(_PIN.read_text(encoding="utf-8"))
    assert sorted(records) == sorted(pinned), (
        "the catalogue changed (a fixture, shape or corpus message was added or removed); "
        "regenerate the pin with PARSER_PIN_UPDATE=1"
    )
    for key in sorted(records):
        assert records[key] == pinned[key], key
