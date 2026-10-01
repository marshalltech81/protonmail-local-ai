"""Source authority from the operator rules file (PLAN Phase 4 item 2).

An operator-written TOML file maps addresses and domains to an
``authority_class``. Every entity records the class and the rule that
matched it (provenance); an entity no rule matches is ``unclassified``.
A malformed file fails closed; an absent file classifies nothing.
"""

import os
from pathlib import Path

import pytest
from src.entities import (
    AUTHORITY_CLASSES,
    AUTHORITY_RULES_MAX_BYTES,
    AUTHORITY_RULES_MAX_PATTERNS,
    UNCLASSIFIED,
    AuthorityRules,
    AuthorityRulesError,
    load_authority_rules,
)

from tests.conftest import make_message, make_thread

RULES = """
[counsel]
domains = ["lawfirm.example"]
addresses = ["outside.counsel@gmail.com"]

[management]
domains = ["hq.northwind.example"]

[vendor]
domains = ["northwind.example"]
addresses = ["Billing@Lawfirm.example"]
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "authority.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _vec() -> list[float]:
    return [1.0] + [0.0] * 4095


class TestLoad:
    def test_absent_file_classifies_nothing(self, tmp_path):
        rules = load_authority_rules(tmp_path / "missing.toml")
        assert rules.pattern_count == 0
        assert rules.classify("a@lawfirm.example") == (UNCLASSIFIED, None)

    def test_valid_file(self, tmp_path):
        rules = load_authority_rules(_write(tmp_path, RULES))
        assert rules.pattern_count == 5

    def test_example_file_in_the_repo_loads(self):
        example = Path(__file__).resolve().parents[2] / "config" / "authority.toml.example"
        rules = load_authority_rules(example)
        assert rules.pattern_count > 0

    @pytest.mark.parametrize(
        ("text", "fragment"),
        [
            ("[counsel\n", "not valid TOML"),
            ('[spies]\ndomains = ["x.example"]\n', "unknown authority class"),
            ('[unclassified]\ndomains = ["x.example"]\n', "unknown authority class"),
            ('counsel = "x.example"\n', "must be a table"),
            ('[counsel]\nhosts = ["x.example"]\n', "unknown key"),
            ('[counsel]\ndomains = "x.example"\n', "must be a list of strings"),
            ("[counsel]\ndomains = [1]\n", "must be a list of strings"),
            ('[counsel]\ndomains = ["@x.example"]\n', "bare domain"),
            ('[counsel]\ndomains = [""]\n', "bare domain"),
            ('[counsel]\ndomains = ["x .example"]\n', "bare domain"),
            ('[counsel]\naddresses = ["x.example"]\n', "bare address"),
            ('[counsel]\naddresses = ["Jo <jo@x.example>"]\n', "bare address"),
            (
                '[counsel]\ndomains = ["x.example"]\n[vendor]\ndomains = ["X.example"]\n',
                "more than once",
            ),
        ],
    )
    def test_malformed_file_fails_closed(self, tmp_path, text, fragment):
        with pytest.raises(AuthorityRulesError, match=fragment):
            load_authority_rules(_write(tmp_path, text))

    def test_errors_do_not_quote_the_patterns(self, tmp_path):
        # The file holds addresses: an error names the class and key
        # and the entry's position, never the value.
        text = '[counsel]\naddresses = ["ok@x.example", "secret-marker-not-an-address"]\n'
        with pytest.raises(AuthorityRulesError) as exc:
            load_authority_rules(_write(tmp_path, text))
        assert "secret-marker" not in str(exc.value)
        assert "counsel.addresses[1]" in str(exc.value)

    def test_directory_path_fails_closed(self, tmp_path):
        with pytest.raises(AuthorityRulesError, match="not a regular file"):
            load_authority_rules(tmp_path)

    def test_oversized_file_fails_closed_without_parsing(self, tmp_path):
        path = _write(tmp_path, "#" * (AUTHORITY_RULES_MAX_BYTES + 1))
        with pytest.raises(AuthorityRulesError, match="larger than"):
            load_authority_rules(path)

    def test_too_many_patterns_fails_closed(self, tmp_path):
        domains = ", ".join(f'"d{n}.example"' for n in range(AUTHORITY_RULES_MAX_PATTERNS + 1))
        with pytest.raises(AuthorityRulesError, match="at most"):
            load_authority_rules(_write(tmp_path, f"[vendor]\ndomains = [{domains}]\n"))

    def test_classes_are_the_documented_set(self):
        assert AUTHORITY_CLASSES == (
            "counsel",
            "management",
            "vendor",
            "government",
            "personal",
            "other",
        )


class TestClassify:
    @pytest.fixture
    def rules(self, tmp_path) -> AuthorityRules:
        return load_authority_rules(_write(tmp_path, RULES))

    @pytest.mark.parametrize(
        ("address", "expected"),
        [
            ("partner@lawfirm.example", ("counsel", "domain:lawfirm.example")),
            # Subdomains inherit the closest listed parent.
            ("a@mail.lawfirm.example", ("counsel", "domain:lawfirm.example")),
            ("ceo@hq.northwind.example", ("management", "domain:hq.northwind.example")),
            ("rep@northwind.example", ("vendor", "domain:northwind.example")),
            # An address rule beats its domain's rule; patterns are
            # matched case-insensitively.
            ("billing@lawfirm.example", ("vendor", "address:billing@lawfirm.example")),
            (
                "outside.counsel@gmail.com",
                ("counsel", "address:outside.counsel@gmail.com"),
            ),
            ("someone@gmail.com", (UNCLASSIFIED, None)),
            # A suffix that is not a label boundary does not match.
            ("x@notlawfirm.example", (UNCLASSIFIED, None)),
        ],
    )
    def test_classify(self, rules, address, expected):
        assert rules.classify(address) == expected

    def test_classify_domain(self, rules):
        assert rules.classify_domain("mail.lawfirm.example") == (
            "counsel",
            "domain:lawfirm.example",
        )
        assert rules.classify_domain("other.example") == (UNCLASSIFIED, None)


class TestEntityAuthority:
    def _authority(self, db) -> dict[str, tuple[str, str | None]]:
        return {
            r["entity_id"]: (r["authority_class"], r["authority_rule"])
            for r in db._conn.execute("SELECT * FROM entities")
        }

    def test_written_with_the_entity(self, db, tmp_path):
        db.set_authority_rules(load_authority_rules(_write(tmp_path, RULES)))
        msg = make_message(
            message_id="m1@example.com",
            from_addr="Pat Partner <partner@lawfirm.example>",
            to_addrs=["sam@contoso.example"],
        )
        db.upsert_thread(make_thread(messages=[msg], thread_id="t1"), _vec())

        assert self._authority(db) == {
            "person:partner@lawfirm.example": ("counsel", "domain:lawfirm.example"),
            "org:lawfirm.example": ("counsel", "domain:lawfirm.example"),
            "person:sam@contoso.example": (UNCLASSIFIED, None),
            "org:contoso.example": (UNCLASSIFIED, None),
        }

    def test_no_rules_leaves_everything_unclassified(self, db):
        msg = make_message(message_id="m1@example.com", from_addr="partner@lawfirm.example")
        db.upsert_thread(make_thread(messages=[msg], thread_id="t1"), _vec())
        assert {v for v in self._authority(db).values()} == {(UNCLASSIFIED, None)}

    def test_new_rules_reclassify_existing_entities(self, db, tmp_path):
        msg = make_message(
            message_id="m1@example.com",
            from_addr="partner@lawfirm.example",
            to_addrs=["rep@northwind.example"],
        )
        db.upsert_thread(make_thread(messages=[msg], thread_id="t1"), _vec())

        db.set_authority_rules(load_authority_rules(_write(tmp_path, RULES)))
        authority = self._authority(db)
        assert authority["person:partner@lawfirm.example"] == (
            "counsel",
            "domain:lawfirm.example",
        )
        assert authority["person:rep@northwind.example"] == (
            "vendor",
            "domain:northwind.example",
        )

        # Removing a rule returns its entities to unclassified.
        db.set_authority_rules(AuthorityRules())
        assert {v for v in self._authority(db).values()} == {(UNCLASSIFIED, None)}


class TestStartup:
    def test_malformed_file_stops_the_indexer(self, tmp_path):
        from src.main import _load_authority_rules

        path = _write(tmp_path, '[counsel]\naddresses = ["secret-marker"]\n')
        with pytest.raises(SystemExit) as exc:
            _load_authority_rules(path)
        assert "Invalid source-authority rules" in str(exc.value)
        assert "secret-marker" not in str(exc.value)

    def test_valid_and_absent_files_load(self, tmp_path, caplog):
        from src.main import _load_authority_rules

        caplog.set_level("INFO")
        assert _load_authority_rules(_write(tmp_path, RULES)).pattern_count == 5
        assert _load_authority_rules(tmp_path / "missing.toml").pattern_count == 0
        assert "lawfirm" not in caplog.text


class TestReviewRound2:
    """Codex round 2 on #459."""

    def test_repeated_addresses_do_not_spend_the_entity_budget(self, db):
        from src.database import MAX_ENTITY_PARTICIPANTS_PER_MESSAGE

        msg = make_message(message_id="m1@example.com", from_addr="dup@sender.example")
        msg.from_addrs = ["dup@sender.example"] * (MAX_ENTITY_PARTICIPANTS_PER_MESSAGE + 50) + [
            "Second Author <second@author.example>"
        ]
        db.upsert_thread(make_thread(messages=[msg], thread_id="t1"), _vec())

        people = {
            r[0] for r in db._conn.execute("SELECT entity_id FROM entities WHERE kind = 'person'")
        }
        assert "person:dup@sender.example" in people
        assert "person:second@author.example" in people

    def test_the_cap_counts_distinct_addresses(self, db):
        from src.database import MAX_ENTITY_PARTICIPANTS_PER_MESSAGE

        distinct = [f"r{n}@d.example" for n in range(MAX_ENTITY_PARTICIPANTS_PER_MESSAGE + 10)]
        msg = make_message(
            message_id="m1@example.com",
            from_addr="s@sender.example",
            to_addrs=distinct,
            cc_addrs=distinct,
        )
        db.upsert_thread(make_thread(messages=[msg], thread_id="t1"), _vec())
        people = db._conn.execute("SELECT COUNT(*) FROM entities WHERE kind = 'person'").fetchone()[
            0
        ]
        assert people == MAX_ENTITY_PARTICIPANTS_PER_MESSAGE


class _CountingDict(dict):
    """A rules table that counts membership lookups."""

    lookups = 0

    def __contains__(self, key):
        type(self).lookups += 1
        return super().__contains__(key)


class TestReviewRound1:
    """Codex round 1 on #459."""

    def _counting_rules(self) -> AuthorityRules:
        _CountingDict.lookups = 0
        return AuthorityRules(domains=_CountingDict({"lawfirm.example": "counsel"}))

    def test_many_label_domain_does_bounded_work(self):
        # 500 one-character labels: the old walk joined every suffix
        # (quadratic). Past the DNS length cap it is unclassified with
        # no lookups at all.
        rules = self._counting_rules()
        domain = "a." * 500 + "lawfirm.example"
        assert rules.classify(f"x@{domain}") == (UNCLASSIFIED, None)
        assert rules.classify_domain(domain) == (UNCLASSIFIED, None)
        assert _CountingDict.lookups == 0

    def test_lookups_are_capped_by_label_count(self):
        rules = self._counting_rules()
        # 17 labels, well under 253 characters: over the label cap.
        assert rules.classify_domain("a." * 15 + "lawfirm.example") == (UNCLASSIFIED, None)
        assert _CountingDict.lookups == 0
        # 16 labels: classified, with at most one lookup per label.
        assert rules.classify_domain("a." * 14 + "lawfirm.example") == (
            "counsel",
            "domain:lawfirm.example",
        )
        assert _CountingDict.lookups <= 16

    def test_unknown_table_name_is_not_quoted(self, tmp_path, caplog):
        from src.main import _load_authority_rules

        caplog.set_level("INFO")
        path = _write(tmp_path, '["secret-marker@x.example"]\ndomains = ["x.example"]\n')
        with pytest.raises(AuthorityRulesError) as exc:
            load_authority_rules(path)
        assert "secret-marker" not in str(exc.value)
        assert "table 1" in str(exc.value)
        with pytest.raises(SystemExit) as stop:
            _load_authority_rules(path)
        assert "secret-marker" not in str(stop.value)
        assert "secret-marker" not in caplog.text

    def test_unknown_key_name_is_not_quoted(self, tmp_path):
        path = _write(tmp_path, '[counsel]\n"secret-marker@x.example" = ["x.example"]\n')
        with pytest.raises(AuthorityRulesError, match="unknown key") as exc:
            load_authority_rules(path)
        assert "secret-marker" not in str(exc.value)

    def test_dangling_symlink_fails_closed(self, tmp_path):
        link = tmp_path / "authority.toml"
        link.symlink_to(tmp_path / "gone.toml")
        with pytest.raises(AuthorityRulesError, match="could not be inspected"):
            load_authority_rules(link)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
    def test_unsearchable_parent_fails_closed(self, tmp_path):
        parent = tmp_path / "locked"
        parent.mkdir()
        (parent / "authority.toml").write_text(RULES, encoding="utf-8")
        parent.chmod(0)
        try:
            with pytest.raises(AuthorityRulesError, match="could not be inspected"):
                load_authority_rules(parent / "authority.toml")
        finally:
            parent.chmod(0o700)

    @pytest.mark.parametrize(
        "domain",
        [
            "*.x.example",
            "https://x.example",
            "x.example/path",
            "a..b.example",
            "-x.example",
            "x-.example",
            "x_y.example",
            "a" * 64 + ".example",
            "a." * 16 + "example",
            ("a" * 60 + ".") * 5 + "example",
        ],
    )
    def test_unmatchable_domain_rules_fail_closed(self, tmp_path, domain):
        with pytest.raises(AuthorityRulesError, match=r"counsel\.domains\[0\]") as exc:
            load_authority_rules(_write(tmp_path, f'[counsel]\ndomains = ["{domain}"]\n'))
        assert domain not in str(exc.value)

    def test_address_rule_domain_is_validated_too(self, tmp_path):
        with pytest.raises(AuthorityRulesError, match=r"counsel\.addresses\[0\]"):
            load_authority_rules(_write(tmp_path, '[counsel]\naddresses = ["jo@a..example"]\n'))

    def test_valid_ldh_domains_load(self, tmp_path):
        text = '[vendor]\ndomains = ["x-1.example", "mail.x2.example", "A.Example"]\n'
        assert load_authority_rules(_write(tmp_path, text)).pattern_count == 3

    def test_entity_writes_are_capped_per_message(self, db):
        from src.database import MAX_ENTITY_PARTICIPANTS_PER_MESSAGE

        recipients = [f"r{n}@d{n}.example" for n in range(MAX_ENTITY_PARTICIPANTS_PER_MESSAGE + 50)]
        msg = make_message(
            message_id="m1@example.com", from_addr="s@sender.example", to_addrs=recipients
        )
        db.upsert_thread(make_thread(messages=[msg], thread_id="t1"), _vec())

        count = db._conn.execute
        participants = count("SELECT COUNT(*) FROM message_participants").fetchone()[0]
        people = count("SELECT COUNT(*) FROM entities WHERE kind = 'person'").fetchone()[0]
        assert participants == len(recipients) + 1
        assert people == MAX_ENTITY_PARTICIPANTS_PER_MESSAGE
        # The sender is written first, so it always gets its entity.
        assert count(
            "SELECT 1 FROM entities WHERE entity_id = 'person:s@sender.example'"
        ).fetchone()
