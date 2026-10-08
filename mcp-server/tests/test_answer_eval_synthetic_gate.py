"""The answer eval's synthetic-index gate, keyed per claimant (#1275).

A test-local corpus holds a duplicate delivery: two files that share one
Message-ID with different bytes, which the indexer keeps as two
claimants (``parser.claimant_id``). The committed baseline corpus is
not changed. The index is written here by hand from values computed
independently of the gate (the bytes' SHA-256, hand-written dates and
text), so the tests do not check the gate against its own derivation.
"""

import hashlib
import importlib.util
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from tests.answer_eval.runner import NonSyntheticIndexError, corpus_manifest, index_identity

# Message-IDs are not thread positions, so the gate must read them from
# the built files. The reply is delivered twice with different bytes
# (another Date and one different body word): "echo" is in the first
# copy only, "foxtrot" in the second only.
_CORPUS = """
import email.policy
from email.message import EmailMessage

OCR_TEXT = {}
THREADS = {
    1: [
        (" <kickoff-root@dup.example> ", "Mon, 3 Aug 2026 07:00:00 +0000", "Kickoff plan",
         "alpha bravo"),
        ("<kickoff-reply@dup.example>", "Tue, 4 Aug 2026 08:00:00 +0000", "Re: Kickoff plan",
         "charlie echo"),
        ("<kickoff-reply@dup.example>", "Tue, 4 Aug 2026 09:30:00 +0000", "Re: Kickoff plan",
         "charlie foxtrot"),
    ],
}
THREADS.update(EXTRA)


def build_message(n, index, msg):
    message_id, date, subject, body = msg
    em = EmailMessage(policy=email.policy.SMTP)
    em["Message-ID"] = message_id
    em["Date"] = date
    em["From"] = "Sam Reed <sam@dup.example>"
    em["To"] = "Kim Lowe <kim@dup.example>"
    em["Subject"] = subject
    em.set_content(body)
    return em.as_bytes()
"""

ROOT_ID = "kickoff-root@dup.example"
REPLY_ID = "kickoff-reply@dup.example"
# (Message-ID, sent_at as the indexer stores it, subject, body) per file.
_MESSAGES = [
    (ROOT_ID, "2026-08-03T07:00:00+00:00", "Kickoff plan", "alpha bravo"),
    (REPLY_ID, "2026-08-04T08:00:00+00:00", "Re: Kickoff plan", "charlie echo"),
    (REPLY_ID, "2026-08-04T09:30:00+00:00", "Re: Kickoff plan", "charlie foxtrot"),
]


def _write_corpus(tmp_path: Path, extra: str = "{}") -> tuple[Path, list[str]]:
    """The corpus module and each built file's claimant ID, in thread order."""
    path = tmp_path / "corpus.py"
    path.write_text(f"EXTRA = {extra}\n" + _CORPUS, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("gate_test_corpus", path)
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    claimants = []
    for index, msg in enumerate(module.THREADS[1]):
        raw = module.build_message(1, index, msg)
        claimants.append(f"{msg[0].strip().strip('<>')}#{hashlib.sha256(raw).hexdigest()[:16]}")
    return path, claimants


class _Db:
    """What ``index_identity`` reads: ``_connect``."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)


def _build_index(tmp_path: Path, claimants: list[str]) -> _Db:
    """The tables and columns the gate reads, as the indexer fills them."""
    path = tmp_path / "mail.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE messages (claimant_id TEXT PRIMARY KEY, message_id TEXT,
                sent_at TEXT, occurred_at TEXT, subject TEXT);
            CREATE TABLE message_chunks (claimant_id TEXT, text TEXT);
            CREATE TABLE message_participants (claimant_id TEXT, role TEXT, address TEXT,
                name TEXT);
            CREATE TABLE message_participant_names (claimant_id TEXT, role TEXT,
                address TEXT, name TEXT);
            CREATE TABLE attachments (claimant_id TEXT, filename TEXT, content_type TEXT);
            CREATE TABLE threads (thread_id TEXT PRIMARY KEY, subject TEXT,
                display_subject TEXT, snippet TEXT, body_text TEXT, participants TEXT);
            """
        )
        for claimant, (message_id, sent_at, subject, body) in zip(
            claimants, _MESSAGES, strict=True
        ):
            conn.execute(
                "INSERT INTO messages VALUES (?, ?, ?, NULL, ?)",
                (claimant, message_id, sent_at, subject),
            )
            conn.execute("INSERT INTO message_chunks VALUES (?, ?)", (claimant, body))
            for role, address, name in (
                ("from", "sam@dup.example", "Sam Reed"),
                ("to", "kim@dup.example", "Kim Lowe"),
            ):
                conn.execute(
                    "INSERT INTO message_participants VALUES (?, ?, ?, ?)",
                    (claimant, role, address, name),
                )
                conn.execute(
                    "INSERT INTO message_participant_names VALUES (?, ?, ?, ?)",
                    (claimant, role, address, name),
                )
        conn.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
            (
                ROOT_ID,
                "Kickoff plan",
                "Kickoff plan",
                "charlie foxtrot",
                "Subject: Kickoff plan\nDate: 2026-08-03T07:00:00+00:00\n"
                "alpha bravo charlie echo charlie fox",  # cut at a token budget
                json.dumps(["Sam Reed <sam@dup.example>", "Kim Lowe <kim@dup.example>"]),
            ),
        )
        conn.commit()
    return _Db(path)


@pytest.fixture
def duplicate_pair(tmp_path: Path) -> tuple[Path, list[str], _Db]:
    corpus, claimants = _write_corpus(tmp_path)
    return corpus, claimants, _build_index(tmp_path, claimants)


def test_manifest_keys_each_built_file_by_its_claimant(duplicate_pair):
    corpus, claimants, _ = duplicate_pair
    manifest = corpus_manifest(corpus)
    assert sorted(manifest) == sorted(claimants)
    assert [manifest[c].message_id for c in claimants] == [ROOT_ID, REPLY_ID, REPLY_ID]
    # The thread root is the built root file's own Message-ID.
    assert {e.thread_id for e in manifest.values()} == {ROOT_ID}
    # Each copy's token allowlist is its own file's words.
    assert "echo" in manifest[claimants[1]].tokens
    assert "echo" not in manifest[claimants[2]].tokens
    assert "foxtrot" in manifest[claimants[2]].tokens


def test_duplicate_pair_is_accepted(duplicate_pair):
    corpus, claimants, db = duplicate_pair
    identity = index_identity(db, corpus)
    assert identity["corpus"] == "synthetic-baseline"
    assert identity["messages"] == 3


def test_byte_identical_files_are_a_duplicate_expected_key(tmp_path: Path):
    # A second, byte-identical copy of the root gives the same claimant.
    copy = '{2: [(" <kickoff-root@dup.example> ", "Mon, 3 Aug 2026 07:00:00 +0000", '
    copy += '"Kickoff plan", "alpha bravo")]}'
    corpus, _ = _write_corpus(tmp_path, copy)
    with pytest.raises(NonSyntheticIndexError, match="more than once"):
        corpus_manifest(corpus)


def test_a_built_file_without_a_message_id_is_an_error(tmp_path: Path):
    no_id = '{2: [(" <> ", "Mon, 3 Aug 2026 07:00:00 +0000", "Kickoff plan", "alpha")]}'
    corpus, _ = _write_corpus(tmp_path, no_id)
    with pytest.raises(NonSyntheticIndexError, match="no Message-ID"):
        corpus_manifest(corpus)


def _tamper_cases() -> list[tuple[str, Callable[[list[str]], str]]]:
    def swap(old: str, new: str) -> str:
        return f"UPDATE messages SET claimant_id = '{new}' WHERE claimant_id = '{old}'"

    return [
        ("missing_sibling", lambda c: f"DELETE FROM messages WHERE claimant_id = '{c[2]}'"),
        (
            "unexpected_extra_claimant",
            lambda c: (
                "INSERT INTO messages VALUES ('other@dup.example#"
                + c[1].split("#")[1]
                + "', 'other@dup.example', '2026-08-04T08:00:00+00:00', NULL, 'Kickoff plan')"
            ),
        ),
        (
            "extra_claimant_suffix_matching_no_sibling",
            lambda c: (
                f"INSERT INTO messages VALUES ('{REPLY_ID}#{'0' * 16}', '{REPLY_ID}', "
                "'2026-08-04T08:00:00+00:00', NULL, 'Re: Kickoff plan')"
            ),
        ),
        # The root's hash under the reply's Message-ID: a trusted suffix,
        # but no sibling's.
        ("suffix_matching_no_sibling", lambda c: swap(c[2], f"{REPLY_ID}#{c[0].split('#')[1]}")),
        ("short_suffix", lambda c: swap(c[2], c[2][:-1])),
        ("long_suffix", lambda c: swap(c[2], c[2] + "0")),
        # Metadata checked against the claimant's own entry, not its sibling's.
        (
            "sibling_sent_at",
            lambda c: (
                "UPDATE messages SET sent_at = '2026-08-04T08:00:00+00:00' "
                f"WHERE claimant_id = '{c[2]}'"
            ),
        ),
        (
            "other_message_id",
            lambda c: f"UPDATE messages SET message_id = '{ROOT_ID}' WHERE claimant_id = '{c[2]}'",
        ),
        (
            "occurred_at",
            lambda c: (
                "UPDATE messages SET occurred_at = '2026-08-04T09:30:00+00:00' "
                f"WHERE claimant_id = '{c[2]}'"
            ),
        ),
        # A word only the sibling's token allowlist permits.
        (
            "sibling_only_chunk_text",
            lambda c: (
                f"UPDATE message_chunks SET text = 'charlie foxtrot' WHERE claimant_id = '{c[1]}'"
            ),
        ),
        (
            "sibling_only_subject",
            lambda c: f"UPDATE messages SET subject = 'echo' WHERE claimant_id = '{c[2]}'",
        ),
        (
            "unknown_word",
            lambda c: (
                f"UPDATE message_chunks SET text = 'privatemarker' WHERE claimant_id = '{c[2]}'"
            ),
        ),
    ]


@pytest.mark.parametrize(("name", "tamper"), _tamper_cases(), ids=[n for n, _ in _tamper_cases()])
def test_tampered_duplicate_pair_is_refused(duplicate_pair, name, tamper):
    corpus, claimants, db = duplicate_pair
    with closing(db._connect()) as conn:
        conn.execute(tamper(claimants))
        conn.commit()
    with pytest.raises(NonSyntheticIndexError):
        index_identity(db, corpus)
