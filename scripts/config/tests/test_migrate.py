"""Tests for scripts/config/migrate.py (make migrate-config, #1603).

Standard library only, Python 3.11 or later:
    python3 -m unittest discover -s scripts/config/tests   (make test-config)
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import tomllib
import unittest
import unittest.mock
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import migrate  # noqa: E402
import operator_config as oc  # noqa: E402

REPO = oc.REPO_ROOT
MARKER = "zq9marker"
UUID = "00000000-0000-4000-8000-000000000002"
FP = "c" * 64

BASE_ENV = f"""ACCOUNT_SCOPE={UUID}
BRIDGE_USER=migrated@example.com
BRIDGE_CERT_FINGERPRINT={FP}
EMBED_BASE_URL=http://host.docker.internal:8001/v1
EMBED_MODEL=migrated-embed-model
"""


class MigrateConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "scripts").mkdir()
        shutil.copy(REPO / "scripts" / "validate-env.sh", self.root / "scripts" / "validate-env.sh")
        (self.root / "mbsync").mkdir()
        shutil.copy(
            REPO / "mbsync" / "proton-settings.sh", self.root / "mbsync" / "proton-settings.sh"
        )
        (self.root / "config").mkdir()
        self.paths = migrate.Paths(self.root)

    def write_env(self, text: str = BASE_ENV) -> None:
        (self.root / ".env").write_text(text, encoding="utf-8")

    def test_build_config_produces_a_validatable_configuration(self) -> None:
        self.write_env()
        sparse = migrate.build_config(self.paths)
        config = oc.validate(sparse, self.paths.config.parent)
        account = config["accounts"][0]
        self.assertEqual(account["scope_uuid"], UUID)
        self.assertEqual(account["provider"], "proton-bridge")
        self.assertEqual(account["connection"]["username"], "migrated@example.com")
        self.assertEqual(account["maildir_root"], "/maildir")
        self.assertTrue(account["enabled"])
        self.assertEqual(config["global"]["embedding"]["model"], "migrated-embed-model")
        oc.check_single_enabled_account(config)  # does not raise

    def test_an_unset_source_mode_imports_as_proton_bridge(self) -> None:
        self.write_env()  # SOURCE_MODE left unset, like most real .env files
        config = oc.validate(migrate.build_config(self.paths), self.paths.config.parent)
        self.assertEqual(config["accounts"][0]["provider"], "proton-bridge")

    def test_an_exported_shell_variable_wins_over_env(self) -> None:
        # Review finding: Compose and env_value() both let a variable
        # exported in the invoking shell override .env; --get alone
        # reads only the file, so migrate-config must check os.environ
        # itself first (#1603 round 1).
        self.write_env()
        exported = "11111111-2222-4333-8444-555555555555"
        with unittest.mock.patch.dict(os.environ, {"ACCOUNT_SCOPE": exported}):
            config = oc.validate(migrate.build_config(self.paths), self.paths.config.parent)
        self.assertEqual(config["accounts"][0]["scope_uuid"], exported)

    def test_a_mixed_case_mode_is_normalized_like_the_legacy_validator(self) -> None:
        # Review finding: normalize_mode() in validate-env.sh accepts
        # "OpenAI"; the TOML Choice leaf requires the exact lower-case
        # literal, so migrate-config must normalize it too.
        self.write_env(
            BASE_ENV + "INFERENCE_MODE=OpenAI\nINFERENCE_MODEL=m\nINFERENCE_BASE_URL=default\n"
        )
        config = oc.validate(migrate.build_config(self.paths), self.paths.config.parent)
        self.assertEqual(config["global"]["inference"]["mode"], "openai")

    def test_anthropic_mode_with_no_model_takes_the_compose_default(self) -> None:
        # Review finding: scripts/validate-env.sh lets INFERENCE_MODEL
        # stay empty only in anthropic mode (Compose's own default,
        # claude-sonnet-5-5); the TOML schema has no model default at
        # all, so migrate-config must apply it explicitly.
        extra = "INFERENCE_MODE=anthropic\nINFERENCE_BASE_URL=default\n"  # pragma: allowlist secret
        self.write_env(BASE_ENV + extra)
        config = oc.validate(migrate.build_config(self.paths), self.paths.config.parent)
        self.assertEqual(config["global"]["inference"]["model"], "claude-sonnet-5-5")

    def test_non_bmp_characters_round_trip_through_the_renderer(self) -> None:
        # Review finding: json.dumps's default ensure_ascii escapes a
        # character outside the Basic Multilingual Plane as a UTF-16
        # surrogate pair, which tomllib refuses to read back.
        self.write_env(BASE_ENV + "PROTON_FOLDER_EXCLUSIONS=All Mail|Labels/*|Starred|\U0001f600\n")
        text = migrate.migrate(self.paths)
        reloaded = oc.validate(tomllib.loads(text), self.paths.config.parent)
        self.assertIn("\U0001f600", reloaded["accounts"][0]["sync"]["mailboxes"]["exclude"])

    def test_an_unset_optional_setting_takes_the_schema_default_not_a_skip_bug(self) -> None:
        self.write_env()  # SYNC_INTERVAL left unset
        config = oc.validate(migrate.build_config(self.paths), self.paths.config.parent)
        self.assertEqual(config["accounts"][0]["sync"]["interval_seconds"], 60)

    def test_folder_exclusions_round_trip_through_the_pipe_join(self) -> None:
        self.write_env(BASE_ENV + "PROTON_FOLDER_EXCLUSIONS=All Mail|Labels/*|Starred|Mine\n")
        config = oc.validate(migrate.build_config(self.paths), self.paths.config.parent)
        self.assertEqual(
            config["accounts"][0]["sync"]["mailboxes"]["exclude"],
            ["All Mail", "Labels/*", "Starred", "Mine"],
        )

    def test_identity_toml_is_folded_into_the_account(self) -> None:
        self.write_env()
        (self.root / "config" / "identity.toml").write_text(
            'addresses = ["me@mail.example"]\n', encoding="utf-8"
        )
        config = oc.validate(migrate.build_config(self.paths), self.paths.config.parent)
        self.assertEqual(config["accounts"][0]["identity"]["addresses"], ["me@mail.example"])

    def test_authority_toml_is_folded_into_global_classification(self) -> None:
        self.write_env()
        (self.root / "config" / "authority.toml").write_text(
            '[vendor]\ndomains = ["contractor.example"]\n', encoding="utf-8"
        )
        config = oc.validate(migrate.build_config(self.paths), self.paths.config.parent)
        self.assertEqual(
            config["global"]["classification"]["authority"]["vendor"]["domains"],
            ["contractor.example"],
        )

    def test_render_toml_round_trips(self) -> None:
        self.write_env()
        text = migrate.migrate(self.paths)
        reloaded = oc.validate(tomllib.loads(text), self.paths.config.parent)
        config = oc.validate(migrate.build_config(self.paths), self.paths.config.parent)
        self.assertEqual(reloaded, config)

    def test_run_writes_config_toml_and_sets_mode_toml(self) -> None:
        self.write_env()
        code = migrate.run(self.paths)
        self.assertEqual(code, 0)
        self.assertEqual(self.paths.mode.read_text(encoding="utf-8").strip(), "toml")
        self.assertEqual(stat.S_IMODE(self.paths.mode.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.paths.config.stat().st_mode), 0o600)
        data = tomllib.loads(self.paths.config.read_text(encoding="utf-8"))
        self.assertEqual(data["accounts"][0]["scope_uuid"], UUID)

    def test_run_refuses_an_invalid_existing_config_toml(self) -> None:
        self.write_env()
        self.paths.config.write_text(MARKER, encoding="utf-8")
        code = migrate.run(self.paths)
        self.assertEqual(code, 1)
        self.assertEqual(self.paths.config.read_text(encoding="utf-8"), MARKER)
        self.assertFalse(self.paths.mode.exists())

    def test_run_adopts_a_valid_existing_config_toml_without_rewriting_it(self) -> None:
        # Review finding: scripts/init-config.sh tells an operator whose
        # config/config.toml predates config/mode to run make
        # migrate-config; that advice must actually work, not just
        # refuse again (#1603 round 1).
        self.write_env()
        text = migrate.render_toml(
            oc.validate(migrate.build_config(self.paths), self.paths.config.parent)
        )
        os.makedirs(self.paths.config.parent, exist_ok=True)
        self.paths.config.write_text(text, encoding="utf-8")
        os.chmod(self.paths.config, 0o600)
        code = migrate.run(self.paths)
        self.assertEqual(code, 0)
        self.assertEqual(self.paths.config.read_text(encoding="utf-8"), text)
        self.assertEqual(self.paths.mode.read_text(encoding="utf-8").strip(), "toml")

    def test_run_refuses_adopting_an_invalid_existing_config_toml_without_writing_mode(
        self,
    ) -> None:
        self.write_env()
        (self.paths.config).write_text("schema_version = 1\n", encoding="utf-8")
        os.chmod(self.paths.config, 0o600)
        code = migrate.run(self.paths)
        self.assertEqual(code, 1)
        self.assertFalse(self.paths.mode.exists())

    def test_run_refuses_when_mode_is_already_toml(self) -> None:
        self.write_env()
        self.paths.mode.write_text("toml\n", encoding="utf-8")
        code = migrate.run(self.paths)
        self.assertEqual(code, 1)
        self.assertFalse(self.paths.config.exists())

    def test_run_refuses_more_than_one_account_would_be_enabled(self) -> None:
        # Not reachable through .env (which only ever describes one
        # account), but check_single_enabled_account is still consulted
        # so a future bug cannot silently import two enabled accounts.
        self.write_env()
        sparse = migrate.build_config(self.paths)
        second: dict[str, Any] = {
            "scope_uuid": "00000000-0000-4000-8000-000000000003",
            "name": "Second",
            "provider": "proton-bridge",
            "maildir_root": "/accounts/00000000-0000-4000-8000-000000000003",
            "password_file": "../.secrets/bridge_pass_second.txt",
            "connection": {"username": "second@example.com"},
            "proton_bridge": {"certificate_fingerprint": "d" * 64},
        }
        config = oc.validate(
            {**sparse, "accounts": [*sparse["accounts"], second]}, self.paths.config.parent
        )
        with self.assertRaises(oc.ConfigError):
            oc.check_single_enabled_account(config)

    def test_a_marker_never_leaks_into_a_refusal(self) -> None:
        # A malformed fingerprint containing the marker must trigger a
        # refusal whose text never repeats it.
        self.write_env(BASE_ENV.replace(FP, MARKER + "0" * (64 - len(MARKER))))
        with self.assertRaises(oc.ConfigError) as caught:
            oc.validate(migrate.build_config(self.paths), self.paths.config.parent)
        self.assertNotIn(MARKER, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
