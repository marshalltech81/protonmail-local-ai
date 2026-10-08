"""The operator's own addresses from ``config/identity.toml`` (#824, PR 1).

An operator-written TOML file lists the operator's exact addresses. The
indexer loads it at startup and replaces the stored set, its state and
a digest of the normalised set in one transaction. An absent file
clears the set (unconfigured); an empty or malformed one stops the
indexer. Errors and logs name positions and counts, never an address.
"""

import hashlib
import logging
import sqlite3
from pathlib import Path

import pytest
from src import main
from src.database import Database
from src.entities import (
    IDENTITY_MAX_ADDRESSES,
    IDENTITY_MAX_BYTES,
    OperatorIdentity,
    OperatorIdentityError,
    identity_digest,
    load_operator_identity,
)

from tests.conftest import make_message, make_thread

MARKER = "synthetic-identity-marker"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "identity.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _vec() -> list[float]:
    return [1.0] + [0.0] * 4095


def _digest(addresses: list[str]) -> str:
    return hashlib.sha256("".join(f"{a}\n" for a in sorted(addresses)).encode()).hexdigest()


def _stored(db: Database) -> tuple[tuple, list[str]]:
    record = db._conn.execute(
        "SELECT state, address_count, address_digest FROM operator_identity"
    ).fetchall()
    addresses = [r[0] for r in db._conn.execute("SELECT address FROM operator_addresses")]
    assert len(record) == 1
    return tuple(record[0]), sorted(addresses)


class TestLoad:
    def test_absent_file_is_unconfigured(self, tmp_path):
        assert load_operator_identity(tmp_path / "missing.toml") is None

    def test_addresses_are_normalised_like_participant_addresses(self, tmp_path):
        identity = load_operator_identity(
            _write(tmp_path, 'addresses = [" Me@Mail.Example ", "alias@other.example"]\n')
        )
        assert identity == OperatorIdentity(
            addresses=frozenset({"me@mail.example", "alias@other.example"})
        )
        assert identity_digest(identity.addresses) == _digest(
            ["me@mail.example", "alias@other.example"]
        )

    def test_a_loaded_address_equals_the_stored_participant_address(self, tmp_path, db):
        msg = make_message(message_id="m1@example.com", from_addr="Op <Op@Mail.Example>")
        db.upsert_thread(make_thread(messages=[msg], thread_id="t1"), _vec())
        stored = db._conn.execute(
            "SELECT address FROM message_participants WHERE role = 'from'"
        ).fetchone()[0]
        identity = load_operator_identity(_write(tmp_path, 'addresses = ["OP@mail.example"]\n'))
        assert identity is not None
        assert stored in identity.addresses

    def test_example_file_in_the_repo_loads(self):
        example = Path(__file__).resolve().parents[2] / "config" / "identity.toml.example"
        identity = load_operator_identity(example)
        assert identity is not None and identity.addresses
        assert all(a.endswith(".example") for a in identity.addresses)

    @pytest.mark.parametrize(
        ("text", "fragment"),
        [
            ("addresses = [\n", "not valid TOML"),
            ("", "lists no addresses"),
            ("# only a comment\n", "lists no addresses"),
            ("addresses = []\n", "lists no addresses"),
            ('addresses = "me@mail.example"\n', "must be a list of strings"),
            ("addresses = [1]\n", "must be a list of strings"),
            ('aliases = ["me@mail.example"]\n', "unknown key"),
            ('[addresses]\nme = "me@mail.example"\n', "must be a list of strings"),
            ('addresses = ["mail.example"]\n', "bare address"),
            ('addresses = ["Me <me@mail.example>"]\n', "bare address"),
            ('addresses = ["@mail.example"]\n', "bare address"),
            ('addresses = ["me@*.example"]\n', "bare address"),
            ('addresses = [""]\n', "bare address"),
            ('addresses = ["me@mail.example", "ME@mail.example"]\n', "more than once"),
        ],
    )
    def test_malformed_or_empty_file_fails_closed(self, tmp_path, text, fragment):
        with pytest.raises(OperatorIdentityError, match=fragment):
            load_operator_identity(_write(tmp_path, text))

    def test_errors_do_not_quote_the_addresses(self, tmp_path):
        text = f'addresses = ["ok@mail.example", "{MARKER}"]\n'
        with pytest.raises(OperatorIdentityError) as exc:
            load_operator_identity(_write(tmp_path, text))
        assert MARKER not in str(exc.value)
        assert "addresses[1]" in str(exc.value)

    def test_unknown_key_error_does_not_quote_the_key(self, tmp_path):
        # A mistyped key may itself be an address.
        text = f'"{MARKER}@mail.example" = 1\naddresses = ["ok@mail.example"]\n'
        with pytest.raises(OperatorIdentityError) as exc:
            load_operator_identity(_write(tmp_path, text))
        assert MARKER not in str(exc.value)
        assert "key 1" in str(exc.value)

    def test_directory_path_fails_closed(self, tmp_path):
        with pytest.raises(OperatorIdentityError, match="not a regular file"):
            load_operator_identity(tmp_path)

    def test_dangling_symlink_fails_closed(self, tmp_path):
        link = tmp_path / "identity.toml"
        link.symlink_to(tmp_path / "missing.toml")
        with pytest.raises(OperatorIdentityError, match="could not be inspected"):
            load_operator_identity(link)

    def test_oversized_file_fails_closed_without_parsing(self, tmp_path):
        path = _write(tmp_path, "#" * (IDENTITY_MAX_BYTES + 1))
        with pytest.raises(OperatorIdentityError, match="larger than"):
            load_operator_identity(path)

    def test_too_many_addresses_fails_closed(self, tmp_path):
        many = ", ".join(f'"a{n}@mail.example"' for n in range(IDENTITY_MAX_ADDRESSES + 1))
        with pytest.raises(OperatorIdentityError, match="at most"):
            load_operator_identity(_write(tmp_path, f"addresses = [{many}]\n"))

    def test_the_count_bound_is_inclusive(self, tmp_path):
        many = ", ".join(f'"a{n}@mail.example"' for n in range(IDENTITY_MAX_ADDRESSES))
        identity = load_operator_identity(_write(tmp_path, f"addresses = [{many}]\n"))
        assert identity is not None and len(identity.addresses) == IDENTITY_MAX_ADDRESSES


class TestStore:
    def test_a_fresh_database_is_unconfigured(self, db):
        assert _stored(db) == (("unconfigured", 0, _digest([])), [])

    def test_a_loaded_set_replaces_the_stored_one(self, db):
        db.set_operator_identity(
            OperatorIdentity(addresses=frozenset({"a@mail.example", "b@mail.example"}))
        )
        db.set_operator_identity(OperatorIdentity(addresses=frozenset({"c@mail.example"})))
        assert _stored(db) == (
            ("configured", 1, _digest(["c@mail.example"])),
            ["c@mail.example"],
        )

    def test_absent_file_clears_a_stored_set(self, db):
        db.set_operator_identity(OperatorIdentity(addresses=frozenset({"a@mail.example"})))
        db.set_operator_identity(None)
        assert _stored(db) == (("unconfigured", 0, _digest([])), [])

    def test_a_failed_write_keeps_the_previous_set(self, db):
        db.set_operator_identity(OperatorIdentity(addresses=frozenset({"a@mail.example"})))
        # Fail the last statement of the write: the address rows it
        # already replaced must roll back with it.
        db._conn.execute(
            "CREATE TEMP TRIGGER fail_identity BEFORE UPDATE ON operator_identity "
            "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
            db.set_operator_identity(OperatorIdentity(addresses=frozenset({"b@mail.example"})))
        assert _stored(db) == (
            ("configured", 1, _digest(["a@mail.example"])),
            ["a@mail.example"],
        )

    def test_the_record_rejects_other_states_and_rows(self, db):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            db._conn.execute("UPDATE operator_identity SET state = 'partial'")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            db._conn.execute(
                "INSERT INTO operator_identity (id, state, address_count, address_digest) "
                "VALUES (2, 'unconfigured', 0, '')"
            )


class TestStartup:
    @pytest.mark.parametrize(
        "text", ["addresses = []\n", f'addresses = ["{MARKER}"]\n', "addresses = [\n"]
    )
    def test_malformed_or_empty_file_stops_the_indexer(self, tmp_path, caplog, text):
        caplog.set_level(logging.INFO)
        with pytest.raises(SystemExit) as exc:
            main._load_operator_identity(_write(tmp_path, text))
        assert "Invalid operator identity" in str(exc.value)
        assert MARKER not in str(exc.value)
        assert MARKER not in caplog.text

    def test_info_lines_carry_counts_only(self, tmp_path, caplog):
        caplog.set_level(logging.INFO)
        path = _write(tmp_path, f'addresses = ["{MARKER}@mail.example", "b@mail.example"]\n')
        assert main._load_operator_identity(path) is not None
        assert main._load_operator_identity(tmp_path / "missing.toml") is None
        lines = [r for r in caplog.records if r.name == main.log.name]
        assert [r.levelno for r in lines] == [logging.INFO, logging.INFO]
        assert "Operator identity: 2 address(es)" in lines[0].getMessage()
        assert "Operator identity: none at" in lines[1].getMessage()
        assert "direction is unknown" in lines[1].getMessage()
        assert MARKER not in caplog.text

    def test_main_stores_the_set_at_startup(self, tmp_path, monkeypatch, caplog):
        class _Stop(Exception):
            pass

        caplog.set_level(logging.INFO)
        path = _write(tmp_path, f'addresses = ["{MARKER}@mail.example"]\n')
        db = Database(tmp_path / "mail.db")
        monkeypatch.setattr(main, "OPERATOR_IDENTITY_PATH", path)
        monkeypatch.setattr(main, "EMBED_BASE_URL", "http://embed.example/v1")
        monkeypatch.setattr(main, "EMBED_MODEL", "synthetic-embed")
        monkeypatch.setattr(main, "EMBED_API_KEY", "unauthenticated")
        monkeypatch.setattr(main, "Database", lambda _path: db)

        def stop(**_kw):
            raise _Stop

        monkeypatch.setattr(main, "OpenAIEmbedder", stop)
        try:
            with pytest.raises(_Stop):
                main.main()
            assert _stored(db) == (
                ("configured", 1, _digest([f"{MARKER}@mail.example"])),
                [f"{MARKER}@mail.example"],
            )
        finally:
            db.close()
        assert MARKER not in caplog.text
