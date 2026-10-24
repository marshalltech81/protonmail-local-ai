"""Tests for scripts/config/operator_config.py (#1607).

Standard library only, Python 3.11 or later, like the translator:
    python3 -m unittest discover -s scripts/config/tests   (make test-config)

The Compose cases run ``docker compose config``; they skip when docker
is not installed and fail in CI, where it must be."""

from __future__ import annotations

import copy
import io
import json
import os
import re
import shutil
import stat
import subprocess  # nosec B404 - runs docker compose with a fixed argv
import sys
import tempfile
import tomllib
import unittest
import unittest.mock
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import operator_config as oc  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

import synthetic_compose  # noqa: E402

REPO = oc.REPO_ROOT
FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONFIG_DIR = REPO / "config"
MARKER = "zq9marker"
UUID_A = "00000000-0000-4000-8000-000000000001"
UUID_B = "00000000-0000-4000-8000-00000000000b"
FP = "a" * 64


def fixture() -> dict[str, Any]:
    return tomllib.loads((FIXTURES / "legacy.toml").read_text(encoding="utf-8"))


def checked(data: dict[str, Any]) -> dict[str, Any]:
    """Validated as if the file were config/config.toml."""
    return oc.validate(data, CONFIG_DIR)


def second_account(uuid: str = UUID_B, **overrides: Any) -> dict[str, Any]:
    account: dict[str, Any] = {
        "scope_uuid": uuid,
        "name": "Second",
        "provider": "proton-bridge",
        "maildir_root": f"/accounts/{uuid}",
        "password_file": "../.secrets/bridge_pass_second.txt",
        "connection": {"username": "second@example.com"},
        "proton_bridge": {"certificate_fingerprint": "b" * 64},
    }
    account.update(overrides)
    return account


def example_keys() -> set[str]:
    """Every .env.example key, set or commented out, read from the file
    itself: the independent source the mapping must cover."""
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(r"^(?:# ?)?([A-Z][A-Z0-9_]*)=", text, re.MULTILINE))


def parse_env(text: str) -> dict[str, str]:
    env = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            assert value.startswith("'") and value.endswith("'"), line
            env[key] = value[1:-1]
    return env


# --- The example and the fixture --------------------------------------------


class TheExample(unittest.TestCase):
    def test_the_example_translates(self) -> None:
        data = tomllib.loads((CONFIG_DIR / "config.toml.example").read_text(encoding="utf-8"))
        outputs = oc.translate(checked(data))
        self.assertEqual(
            set(outputs),
            {
                "compose.env",
                "compose.accounts.yml",
                "config/identity.toml",
                "config/authority.toml",
            },
        )
        self.assertTrue(all(text is not None for text in outputs.values()))

    def test_the_example_holds_only_synthetic_domains(self) -> None:
        text = (CONFIG_DIR / "config.toml.example").read_text(encoding="utf-8")
        for address in re.findall(r"[\w.+-]+@([\w.-]+)", text):
            self.assertTrue(address.endswith(".example"), address)

    def test_identity_and_authority_render_the_existing_shape(self) -> None:
        # The lists of the legacy files, rendered from TOML, read back as
        # the same TOML documents the indexer loads today.
        config = checked(fixture())
        outputs = oc.translate(config)
        for name in ("identity.toml", "authority.toml"):
            legacy = tomllib.loads((CONFIG_DIR / f"{name}.example").read_text(encoding="utf-8"))
            generated = outputs[f"config/{name}"]
            assert generated is not None
            self.assertEqual(tomllib.loads(generated), legacy, name)

    def test_unconfigured_identity_and_authority_have_no_file(self) -> None:
        data = fixture()
        del data["accounts"][0]["identity"]
        del data["global"]["classification"]
        outputs = oc.translate(checked(data))
        self.assertIsNone(outputs["config/identity.toml"])
        self.assertIsNone(outputs["config/authority.toml"])


# --- Refusals ----------------------------------------------------------------


def _set(data: dict[str, Any], path: str, value: Any) -> None:
    node: Any = data
    parts = path.split(".")
    for part in parts[:-1]:
        node = node[int(part)] if part.isdigit() else node.setdefault(part, {})
    if value is _DELETE:
        del node[parts[-1]]
    else:
        node[parts[-1]] = value


_DELETE = object()

# (description, edits as (dotted path, value), the refusal's text).
REFUSALS: list[tuple[str, list[tuple[str, Any]], str]] = [
    ("unknown top-level key", [("zq9_extra", 1)], "zq9_extra is not a known key."),
    (
        "unknown key holding private text",
        [(f"global.{MARKER}@mail.example", 1)],
        "global.<a key> is not a known key.",
    ),
    (
        "unknown nested key",
        [("accounts.0.sync.intervall_seconds", 5)],
        "accounts[0].sync.intervall_seconds is not a known key.",
    ),
    ("unknown legacy-form key", [("ACCOUNT_SCOPE", UUID_A)], "<a key> is not a known key."),
    (
        "missing schema_version",
        [("schema_version", _DELETE)],
        "schema_version is required; this translator reads version 1.",
    ),
    (
        "another schema_version",
        [("schema_version", 2)],
        "schema_version must be 1, the only version this translator reads.",
    ),
    (
        "a string schema_version",
        [("schema_version", "1")],
        "schema_version must be 1, the only version this translator reads.",
    ),
    (
        "a boolean schema_version",
        [("schema_version", True)],
        "schema_version must be 1, the only version this translator reads.",
    ),
    ("missing global", [("global", _DELETE)], "global is required."),
    ("global not a table", [("global", MARKER)], "global must be a table."),
    ("no accounts", [("accounts", [])], "accounts must be one or more [[accounts]] tables."),
    (
        "missing accounts",
        [("accounts", _DELETE)],
        "accounts is required: at least one [[accounts]] table.",
    ),
    (
        "accounts as one table",
        [("accounts", {"name": MARKER})],
        "accounts must be one or more [[accounts]] tables.",
    ),
    (
        "a string port",
        [("accounts.0.connection.port", "1143")],
        "accounts[0].connection.port must be an integer from 1 to 65535.",
    ),
    (
        "port zero",
        [("accounts.0.connection.port", 0)],
        "accounts[0].connection.port must be an integer from 1 to 65535.",
    ),
    (
        "port too high",
        [("accounts.0.connection.port", 65536)],
        "accounts[0].connection.port must be an integer from 1 to 65535.",
    ),
    (
        "a boolean integer",
        [("global.indexer.initial_batch_size", True)],
        "global.indexer.initial_batch_size must be an integer from 1 or more.",
    ),
    (
        "below a minimum",
        [("global.indexer.wal_checkpoint_interval_seconds", 59)],
        "global.indexer.wal_checkpoint_interval_seconds must be an integer from 60 or more.",
    ),
    (
        "a float integer",
        [("global.indexer.retry.max_attempts", 5.0)],
        "global.indexer.retry.max_attempts must be an integer from 1 or more.",
    ),
    (
        "interval over nine digits",
        [("accounts.0.sync.interval_seconds", 1000000000)],
        "accounts[0].sync.interval_seconds must be an integer from 1 to 999999999.",
    ),
    (
        "deadline over nine digits",
        [("accounts.0.sync.deadline_seconds", 1000000000)],
        "accounts[0].sync.deadline_seconds must be an integer from 1 to 999999999.",
    ),
    (
        "an infinite number",
        [("global.embedding.timeout_seconds", float("inf"))],
        "global.embedding.timeout_seconds must be a finite number of at least 1.",
    ),
    (
        "a NaN number",
        [("global.mcp.session_idle_timeout_seconds", float("nan"))],
        "global.mcp.session_idle_timeout_seconds must be a finite number of at least 1.",
    ),
    (
        "a number below its minimum",
        [("global.inference.timeout_seconds", 0.5)],
        "global.inference.timeout_seconds must be a finite number of at least 1.",
    ),
    (
        # Review round 2: a hexadecimal integer is not bound by the digit
        # limit, and str() of it would raise; validate-env.sh's arithmetic
        # wraps past 2**63 - 1.
        "an integer past 64 bits",
        [("global.inference.max_tokens", int("f" * 5000, 16))],
        "global.inference.max_tokens must be at most 9223372036854775807.",
    ),
    (
        "an integer one past 64 bits",
        [("global.indexer.parse_max_bytes", 2**63)],
        "global.indexer.parse_max_bytes must be at most 9223372036854775807.",
    ),
    (
        "a number past 64 bits",
        [("global.embedding.warmup_timeout_seconds", 1e19)],
        "global.embedding.warmup_timeout_seconds must be at most 9223372036854775807.",
    ),
    (
        "a fraction over 1",
        [("global.indexer.deletion.max_batch_fraction", 1.5)],
        "global.indexer.deletion.max_batch_fraction must be a number from 0 to 1.",
    ),
    (
        "a string boolean",
        [("global.mcp.experimental_tools", "true")],
        "global.mcp.experimental_tools must be true or false.",
    ),
    (
        "an unknown mode",
        [("global.inference.mode", MARKER)],
        "global.inference.mode must be one of: anthropic, openai, none.",
    ),
    (
        "a mode in upper case",
        [("global.reranking.mode", "COHERE")],
        "global.reranking.mode must be one of: cohere, none.",
    ),
    (
        "a line break in text",
        [("accounts.0.connection.username", f"a\n{MARKER}")],
        "accounts[0].connection.username must not hold control characters or line breaks.",
    ),
    (
        "a quote in projected text",
        [("global.embedding.model", f"{MARKER}'x")],
        "global.embedding.model must not hold a single quote (').",
    ),
    (
        "empty text",
        [("accounts.0.connection.username", "")],
        "accounts[0].connection.username must be text of 1 to 1024 characters.",
    ),
    (
        "a list that is not a list",
        [("accounts.0.identity.addresses", MARKER)],
        "accounts[0].identity.addresses must be a list of text.",
    ),
    (
        "an empty identity",
        [("accounts.0.identity.addresses", [])],
        "accounts[0].identity.addresses must hold 1 to 1000 entries.",
    ),
    (
        "an identity table without addresses",
        [("accounts.0.identity", {})],
        "accounts[0].identity.addresses is required.",
    ),
    (
        "too many identity addresses",
        [("accounts.0.identity.addresses", [f"a{i}@mail.example" for i in range(1001)])],
        "accounts[0].identity.addresses must hold 1 to 1000 entries.",
    ),
    (
        "too many authority patterns",
        [
            (
                "global.classification.authority.vendor.domains",
                [f"d{i}.example" for i in range(9996)],
            )
        ],
        "global.classification.authority may hold at most 10000 patterns.",
    ),
    (
        "an unknown authority class",
        [("global.classification.authority.zq9class", {"domains": []})],
        "global.classification.authority.zq9class is not a known key.",
    ),
    (
        "missing embedding base_url",
        [("global.embedding.base_url", _DELETE)],
        "global.embedding.base_url is required when global.embedding.mode is openai (base_url: the provider's URL, or default for the SDK's own endpoint).",
    ),
    (
        "inference enabled without a model",
        [("global.inference.mode", "openai"), ("global.inference.base_url", "default")],
        "global.inference.model is required when global.inference.mode is openai.",
    ),
    (
        "reranking enabled without a base_url",
        [("global.reranking.mode", "cohere"), ("global.reranking.model", "m")],
        "global.reranking.base_url is required when global.reranking.mode is cohere (base_url: the provider's URL, or default for the SDK's own endpoint).",
    ),
    (
        "a malformed UUID",
        [("accounts.0.scope_uuid", UUID_A.replace("4000", "400A"))],
        "accounts[0].scope_uuid must be a UUID in canonical lower-case form (8-4-4-4-12 hexadecimal digits).",
    ),
    ("a missing UUID", [("accounts.0.scope_uuid", _DELETE)], "accounts[0].scope_uuid is required."),
    (
        "an invalid root",
        [("accounts.0.maildir_root", f"/{MARKER}")],
        "accounts[0].maildir_root must be /maildir (one account at most) or /accounts/ followed by accounts[0].scope_uuid.",
    ),
    (
        "a root below /maildir",
        [("accounts.0.maildir_root", "/maildir/x")],
        "accounts[0].maildir_root must be /maildir (one account at most) or /accounts/ followed by accounts[0].scope_uuid.",
    ),
    (
        "a non-empty Proton include",
        [("accounts.0.sync.mailboxes.include", [f"Folders/{MARKER}"])],
        "accounts[0].sync.mailboxes.include must be empty for provider proton-bridge: Proton accounts sync every folder except their exclusions.",
    ),
    (
        "an exclusion holding |",
        [("accounts.0.sync.mailboxes.exclude", ["All Mail", "Labels/*", "Starred", f"a|{MARKER}"])],
        "accounts[0].sync.mailboxes.exclude[3] must not hold |.",
    ),
    (
        "a Proton account without its table",
        [("accounts.0.proton_bridge", _DELETE)],
        "accounts[0].proton_bridge is required when accounts[0].provider is proton-bridge.",
    ),
    (
        "a Proton account with an imap table",
        [("accounts.0.imap", {})],
        "accounts[0].imap must not be set when accounts[0].provider is proton-bridge.",
    ),
    (
        "a malformed fingerprint",
        [("accounts.0.proton_bridge.certificate_fingerprint", MARKER)],
        "accounts[0].proton_bridge.certificate_fingerprint must be a SHA-256 fingerprint: 64 hex digits, with or without colons.",
    ),
    (
        "an unknown address scope",
        [("accounts.0.proton_bridge.declared_address_scope", MARKER)],
        "accounts[0].proton_bridge.declared_address_scope must be one of: combined, split, unverified.",
    ),
]

# Review round 1: what scripts/validate-env.sh or a service would refuse in
# the projection is refused here too, with fixed text.
REFUSALS += [
    (
        "blank text",
        [("accounts.0.connection.username", "   ")],
        "accounts[0].connection.username must not be blank.",
    ),
    (
        "a blank model",
        [("global.embedding.model", " ")],
        "global.embedding.model must not be blank.",
    ),
    (
        "a blank base_url",
        [("global.embedding.base_url", "  ")],
        "global.embedding.base_url must not be blank.",
    ),
    (
        "a blank list entry",
        [("accounts.0.identity.addresses", [" "])],
        "accounts[0].identity.addresses[0] must not be blank.",
    ),
    (
        "a base_url that is not a URL",
        [("global.embedding.base_url", f"{MARKER}.example")],
        "global.embedding.base_url must be default or start with http:// or https://.",
    ),
    (
        # Review round 6: mcp-server reads every *_BASE_URL with urlsplit
        # and refuses userinfo whatever the mode.
        "a disabled layer's base_url with credentials",
        [
            ("global.inference.mode", "none"),
            ("global.inference.base_url", f"https://u:{MARKER}@x.example"),
        ],
        "global.inference.base_url must not embed credentials (user:pass@host): put the key in the layer's api_key_file.",
    ),
    (
        "a disabled layer's base_url with an @ and no scheme",
        [("global.reranking.base_url", f"//u:{MARKER}@x.example")],
        "global.reranking.base_url must not embed credentials (user:pass@host): put the key in the layer's api_key_file.",
    ),
    (
        "a disabled layer's base_url urlsplit refuses",
        [("global.reranking.base_url", f"http://[{MARKER}")],
        "global.reranking.base_url is not a valid URL.",
    ),
    (
        "an enabled layer's base_url urlsplit refuses",
        [("global.embedding.base_url", f"https://[{MARKER}/v1")],
        "global.embedding.base_url is not a valid URL.",
    ),
    (
        # Review round 7: indexer/src/entities.py's loaders refuse these in
        # the rendered files, so the translator does too.
        "an authority domain with a wildcard",
        [("global.classification.authority.vendor.domains", ["ok.example", f"*.{MARKER}.example"])],
        "global.classification.authority.vendor.domains[1] must be a bare domain: dot-separated labels of letters, digits and hyphens, at most 16 labels and 253 characters.",
    ),
    (
        "an authority domain with an empty label",
        [("global.classification.authority.vendor.domains", [f"{MARKER}..example"])],
        "global.classification.authority.vendor.domains[0] must be a bare domain: dot-separated labels of letters, digits and hyphens, at most 16 labels and 253 characters.",
    ),
    (
        "authority address with no local part",
        [("global.classification.authority.personal.addresses", [f"@{MARKER}.example"])],
        "global.classification.authority.personal.addresses[0] must be a bare address (name@domain).",
    ),
    (
        "an authority address with a display name",
        [("global.classification.authority.personal.addresses", [f"A <{MARKER}@mail.example>"])],
        "global.classification.authority.personal.addresses[0] must be a bare address (name@domain).",
    ),
    (
        "an authority address without a bare domain",
        [("global.classification.authority.personal.addresses", [f"{MARKER}@mail_example"])],
        "global.classification.authority.personal.addresses[0] must be a bare address (name@domain).",
    ),
    (
        "an authority domain in two classes",
        [("global.classification.authority.vendor.domains", [" LawFirm.example"])],
        "global.classification.authority.vendor.domains[0] repeats an earlier pattern (compared trimmed and in lower case).",
    ),
    (
        "an authority address in two classes",
        [("global.classification.authority.other.addresses", ["A.Friend@mail.example"])],
        "global.classification.authority.other.addresses[0] repeats an earlier pattern (compared trimmed and in lower case).",
    ),
    (
        "an identity address that is not bare",
        [("accounts.0.identity.addresses", ["me@mail.example", f"<{MARKER}@mail.example>"])],
        "accounts[0].identity.addresses[1] must be a bare address (name@domain).",
    ),
    (
        "an identity address with no local part",
        [("accounts.0.identity.addresses", [f"@{MARKER}.example"])],
        "accounts[0].identity.addresses[0] must be a bare address (name@domain).",
    ),
    (
        "an identity address listed twice",
        [("accounts.0.identity.addresses", ["me@mail.example", "ME@mail.example "])],
        "accounts[0].identity.addresses[1] repeats an earlier address (compared trimmed and in lower case).",
    ),
    (
        "a base_url with credentials",
        [("global.embedding.base_url", f"https://u:{MARKER}@x.example/v1")],
        "global.embedding.base_url must not embed credentials (user:pass@host): put the key in the layer's api_key_file.",
    ),
    (
        "an anthropic base_url ending in /v1",
        [
            ("global.inference.mode", "anthropic"),
            ("global.inference.model", "m"),
            ("global.inference.base_url", "https://gw.example/v1/"),
        ],
        "global.inference.base_url must not end with /v1 when global.inference.mode is anthropic: the SDK appends /v1/messages itself.",
    ),
    (
        "chunk target over max",
        [("global.indexer.chunking.target_tokens", 1600)],
        "global.indexer.chunking.target_tokens must be at most global.indexer.chunking.max_tokens.",
    ),
    (
        "overlap not under target",
        [("global.indexer.chunking.overlap_tokens", 1000)],
        "global.indexer.chunking.overlap_tokens must be less than global.indexer.chunking.target_tokens.",
    ),
    (
        "a context too small for its reply",
        [
            ("global.inference.mode", "openai"),
            ("global.inference.base_url", "default"),
            ("global.inference.model", "m"),
            ("global.inference.max_tokens", 4096),
            ("global.inference.context_tokens", 5000),
        ],
        "global.inference.context_tokens (32768 when unset) must exceed global.inference.max_tokens (1024 when unset) by at least 1088.",
    ),
    (
        "an anthropic default context under a large reply",
        [
            ("global.inference.mode", "anthropic"),
            ("global.inference.base_url", "default"),
            ("global.inference.model", "m"),
            ("global.inference.max_tokens", 47000),
        ],
        "global.inference.context_tokens (48000 when unset) must exceed global.inference.max_tokens (16000 when unset) by at least 1088.",
    ),
    (
        "the placeholder username",
        [("accounts.0.connection.username", "your@proton.me")],
        "accounts[0].connection.username must be the username from the Bridge app's IMAP details, not the example's placeholder.",
    ),
    (
        "a model key that is the MCP token",
        [("global.inference.api_key_file", "../.secrets/mcp_auth_token.txt")],
        "global.inference.api_key_file is a reserved credential file (.secrets/mcp_auth_token.txt, .secrets/mcp_client_headers.txt): each secret goes only where it belongs.",
    ),
    (
        "a password file that is the MCP token",
        [("accounts.0.password_file", "../.secrets/mcp_auth_token.txt")],
        "accounts[0].password_file is a reserved credential file (.secrets/mcp_auth_token.txt, .secrets/mcp_client_headers.txt): each secret goes only where it belongs.",
    ),
]

# Reject endpoint credentials in every configuration mode, including disabled layers.
REFUSALS += [
    (
        f"{layer} endpoint {suffix}",
        [(f"global.{layer}.base_url", f"https://provider.invalid/v1{suffix}{MARKER}")],
        f"global.{layer}.base_url must not contain a query string or fragment.",
    )
    for layer in ("embedding", "inference", "reranking")
    for suffix in ("?key=", "#")
]

# Refused by mbsync/proton-settings.sh, whose fixed text the refusal carries.
PROTON_REFUSALS: list[tuple[str, str, Any, str]] = [
    (
        "a host off the allowlist",
        "accounts.0.connection.host",
        f"{MARKER}.local",
        "BRIDGE_HOST must be one of",
    ),
    (
        "a certificate name off the list",
        "accounts.0.proton_bridge.certificate_host",
        f"{MARKER}.local",
        "BRIDGE_CERT_HOST must be 127.0.0.1",
    ),
    (
        "an exclusion list without Starred",
        "accounts.0.sync.mailboxes.exclude",
        ["All Mail", "Labels/*", f"Folders/{MARKER}"],
        "PROTON_FOLDER_EXCLUSIONS must keep All Mail, Labels/* and Starred",
    ),
    (
        "an exclusion with a refused character",
        "accounts.0.sync.mailboxes.exclude",
        ["All Mail", "Labels/*", "Starred", f"{MARKER}#x"],
        "PROTON_FOLDER_EXCLUSIONS must be folder patterns",
    ),
    (
        "a channel name with a space",
        "accounts.0.sync.channel_name",
        f"{MARKER} x",
        "PROTON_CHANNEL_NAME must be 1 to 64 characters",
    ),
    (
        "equal store names",
        "accounts.0.sync.remote_store_name",
        "protonmail-local",
        "PROTON_REMOTE_STORE_NAME and PROTON_LOCAL_STORE_NAME must differ.",
    ),
]


class Refusals(unittest.TestCase):
    def assert_refused(self, data: dict[str, Any], expected: str, *, prefix: bool = False) -> None:
        with self.assertRaises(oc.ConfigError) as caught:
            oc.translate(checked(data))
        text = str(caught.exception)
        if prefix:
            self.assertIn(expected, text)
        else:
            self.assertEqual(text, expected)
        self.assertNotIn(MARKER, text)

    def test_each_refusal_is_fixed_text(self) -> None:
        self.assertGreater(len(REFUSALS), 40)
        for description, edits, expected in REFUSALS:
            with self.subTest(description):
                data = fixture()
                for path, value in edits:
                    _set(data, path, value)
                self.assert_refused(data, expected)

    def test_proton_settings_refusals_carry_the_shared_text(self) -> None:
        for description, path, value, expected in PROTON_REFUSALS:
            with self.subTest(description):
                data = fixture()
                _set(data, path, value)
                self.assert_refused(data, f"accounts[0]: {expected}", prefix=True)

    def test_the_round_one_rules_accept_their_boundaries(self) -> None:
        # The accepted side of each rule above, as validate-env.sh draws it.
        data = fixture()
        inference = {"mode": "openai", "base_url": "DEFAULT", "model": "m"}
        data["global"]["inference"] = {
            **inference,
            "max_tokens": 4096,
            "context_tokens": 4096 + 1088,
        }
        data["global"]["indexer"] = {
            "chunking": {"target_tokens": 1500, "max_tokens": 1500, "overlap_tokens": 1499}
        }
        data["global"]["reranking"] = {
            "mode": "cohere",
            "base_url": "https://gw.example/v1",
            "model": "m",
        }
        checked(data)
        # A disabled layer's budget and URL are not checked, as in validate-env.sh.
        data["global"]["inference"] = {
            "mode": "none",
            "base_url": "x",
            "max_tokens": 9000,
            "context_tokens": 1,
        }
        checked(data)

    def test_a_link_to_a_reserved_file_is_refused(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / "key.txt").symlink_to(REPO / ".secrets" / "mcp_auth_token.txt")
        data = fixture()
        data["global"]["embedding"]["api_key_file"] = str(tmp / "key.txt")
        self.assert_refused(
            data,
            "global.embedding.api_key_file is a reserved credential file (.secrets/mcp_auth_token.txt, .secrets/mcp_client_headers.txt): each secret goes only where it belongs.",
        )

    def test_duplicate_uuids_are_refused(self) -> None:
        data = fixture()
        data["accounts"].append(second_account(UUID_A, maildir_root=f"/accounts/{UUID_A}"))
        self.assert_refused(
            data,
            "accounts[1].scope_uuid repeats an earlier account's: each account has its own UUID.",
        )

    def test_two_accounts_at_maildir_are_refused(self) -> None:
        data = fixture()
        data["accounts"].append(second_account(maildir_root="/maildir"))
        self.assert_refused(data, "accounts[1].maildir_root: only one account may use /maildir.")

    def test_a_root_naming_another_account_is_refused(self) -> None:
        data = fixture()
        data["accounts"].append(second_account(maildir_root=f"/accounts/{UUID_A}"))
        self.assert_refused(
            data,
            "accounts[1].maildir_root must be /maildir (one account at most) or /accounts/ followed by accounts[1].scope_uuid.",
        )

    def test_a_shared_password_file_is_refused(self) -> None:
        for shared in ("../.secrets/bridge_pass.txt", "../.secrets/embed_api_key.txt"):
            with self.subTest(shared):
                data = fixture()
                data["accounts"].append(second_account(password_file=shared))
                self.assert_refused(
                    data,
                    "accounts[1].password_file is another account's password file or a model API key file: each account has its own secret.",
                )

    def test_secret_paths_are_compared_by_file_identity(self) -> None:
        # Review round 2: a case-insensitive spelling (APFS's default) or a
        # hard link opens the reserved file without matching its path.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / ".secrets").mkdir()
        (tmp / ".secrets" / "mcp_auth_token.txt").write_text("t")
        os.link(tmp / ".secrets" / "mcp_auth_token.txt", tmp / "linked.txt")
        reserved = (
            "global.embedding.api_key_file is a reserved credential file "
            "(.secrets/mcp_auth_token.txt, .secrets/mcp_client_headers.txt): "
            "each secret goes only where it belongs."
        )
        with unittest.mock.patch.object(oc, "REPO_ROOT", tmp):
            for alias in (".SECRETS/MCP_AUTH_TOKEN.TXT", "linked.txt"):
                with self.subTest(alias):
                    data = fixture()
                    data["global"]["embedding"]["api_key_file"] = str(tmp / alias)
                    self.assert_refused(data, reserved)

    def test_a_password_file_aliasing_another_secret_is_refused(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / "pass.txt").write_text("p")
        os.link(tmp / "pass.txt", tmp / "linked.txt")
        shared = (
            "accounts[1].password_file is another account's password file or a model "
            "API key file: each account has its own secret."
        )
        # Existing files by identity; a missing one by its spelling, case folded,
        # since it may be created on a case-insensitive filesystem.
        for first, second in (
            ("pass.txt", "linked.txt"),
            ("pass.txt", "PASS.TXT"),
            ("missing.txt", "MISSING.txt"),
        ):
            with self.subTest(second):
                data = fixture()
                data["accounts"][0]["password_file"] = str(tmp / first)
                data["accounts"].append(second_account(password_file=str(tmp / second)))
                self.assert_refused(data, shared)
        data = fixture()
        data["global"].setdefault("reranking", {})["api_key_file"] = str(tmp / "pass.txt")
        data["accounts"][0]["password_file"] = str(tmp / "linked.txt")
        self.assert_refused(
            data,
            "accounts[0].password_file is another account's password file or a model "
            "API key file: each account has its own secret.",
        )

    def test_a_conventional_file_of_another_role_is_refused(self) -> None:
        # Review round 4: the shared-secret check sees only the paths in
        # use, so with the owner configured elsewhere a key file could be
        # .secrets/bridge_pass.txt, or a password file a model key's file.
        for layer in ("embedding", "inference", "reranking"):
            with self.subTest(layer):
                data = fixture()
                data["accounts"][0]["password_file"] = "../.secrets/bridge_pass_elsewhere.txt"
                data["global"].setdefault(layer, {})["api_key_file"] = "../.secrets/bridge_pass.txt"
                self.assert_refused(
                    data,
                    f"global.{layer}.api_key_file is the Bridge password's file "
                    "(.secrets/bridge_pass.txt): each secret goes only where it belongs.",
                )
        for name in ("embed_api_key", "inference_api_key", "rerank_api_key"):
            with self.subTest(name):
                data = fixture()
                for layer in ("embedding", "inference", "reranking"):
                    data["global"].setdefault(layer, {})["api_key_file"] = (
                        f"../.secrets/{layer}_elsewhere.txt"
                    )
                data["accounts"][0]["password_file"] = f"../.secrets/{name}.txt"
                self.assert_refused(
                    data,
                    "accounts[0].password_file is a model API key's file "
                    "(.secrets/embed_api_key.txt, .secrets/inference_api_key.txt, "
                    ".secrets/rerank_api_key.txt): each secret goes only where it belongs.",
                )
        # Within a role the conventional file stays usable: a second
        # account may use .secrets/bridge_pass.txt, and two layers one key.
        data = fixture()
        data["accounts"][0]["password_file"] = "../.secrets/bridge_pass_elsewhere.txt"
        data["accounts"].append(second_account(password_file="../.secrets/bridge_pass.txt"))
        data["global"].setdefault("reranking", {})["api_key_file"] = "../.secrets/embed_api_key.txt"
        checked(data)

    def test_a_data_or_private_key_file_is_not_a_secret(self) -> None:
        # Review round 5: AGENTS.md's other sensitive files (.env, the
        # operator's TOML files and their projections) and private-key
        # files would be mounted and sent as a key or password.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / "link.txt").symlink_to(tmp / "bridge.PEM")
        paths = [f"../{name}" for name in oc.RESERVED_DATA_FILES] + [
            "identity.toml",
            "../mbsync/bridge-cert.pem",
            "tls.KEY",
            "store.p12",
            "store.pfx",
            str(tmp / "link.txt"),
        ]
        self.assertIn(".env", oc.RESERVED_DATA_FILES)
        self.assertIn("config/authority.toml", oc.RESERVED_DATA_FILES)
        self.assertIn(".generated/config/identity.toml", oc.RESERVED_DATA_FILES)
        for value in paths:
            for key, edit in (
                ("global.inference.api_key_file", ("global.inference.api_key_file", value)),
                ("accounts[0].password_file", ("accounts.0.password_file", value)),
            ):
                with self.subTest(value=value, key=key):
                    data = fixture()
                    data["global"].setdefault("inference", {})
                    _set(data, *edit)
                    self.assert_refused(
                        data,
                        f"{key} is a configuration, data or private-key file "
                        f"({', '.join(oc.RESERVED_DATA_FILES)}, or *.pem, *.key, *.p12, *.pfx): "
                        "each secret goes only where it belongs.",
                    )

    def test_config_mode_is_not_an_api_key_file(self) -> None:
        # Review round 2 (#1603): config/mode (the legacy/toml selector,
        # #1603) is mode 600 with non-whitespace content ("toml" or
        # "legacy"), so without this check check-secrets/validate-env
        # would pass a layer whose api_key_file points at it, and the
        # service would receive the selector string as its credential.
        self.assertIn("config/mode", oc.RESERVED_DATA_FILES)
        data = fixture()
        data["global"].setdefault("inference", {})
        data["global"]["inference"]["api_key_file"] = "mode"  # pragma: allowlist secret
        self.assert_refused(
            data,
            "global.inference.api_key_file is a configuration, data or private-key file "
            f"({', '.join(oc.RESERVED_DATA_FILES)}, or *.pem, *.key, *.p12, *.pfx): "
            "each secret goes only where it belongs.",
        )

    def test_role_checks_also_read_the_configuration_root(self) -> None:
        # Review round 7: with the configuration outside this checkout,
        # its own ../.secrets and config/ files are the conventional ones.
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        base = root / "config"
        cases = [
            (
                "global.embedding.api_key_file",
                "../.secrets/bridge_pass.txt",
                "global.embedding.api_key_file is the Bridge password's file "
                "(.secrets/bridge_pass.txt): each secret goes only where it belongs.",
            ),
            (
                "global.embedding.api_key_file",
                "../.secrets/mcp_auth_token.txt",
                "global.embedding.api_key_file is a reserved credential file "
                "(.secrets/mcp_auth_token.txt, .secrets/mcp_client_headers.txt): "
                "each secret goes only where it belongs.",
            ),
            (
                "global.embedding.api_key_file",
                "identity.toml",
                "global.embedding.api_key_file is a configuration, data or private-key file "
                f"({', '.join(oc.RESERVED_DATA_FILES)}, or *.pem, *.key, *.p12, *.pfx): "
                "each secret goes only where it belongs.",
            ),
            (
                "accounts.0.password_file",
                "../.secrets/rerank_api_key.txt",
                "accounts[0].password_file is a model API key's file "
                "(.secrets/embed_api_key.txt, .secrets/inference_api_key.txt, "
                ".secrets/rerank_api_key.txt): each secret goes only where it belongs.",
            ),
        ]
        for key, value, expected in cases:
            with self.subTest(value):
                data = fixture()
                data["accounts"][0]["password_file"] = "../.secrets/pass_elsewhere.txt"
                data["global"].setdefault("reranking", {})["api_key_file"] = "../keys/r.txt"
                _set(data, key, value)
                with self.assertRaises(oc.ConfigError) as caught:
                    oc.validate(data, base)
                self.assertEqual(str(caught.exception), expected)

    def test_proton_values_reach_bash_on_stdin_not_its_arguments(self) -> None:
        # Review round 7: other local accounts can read process arguments,
        # and folder names are mailbox metadata.
        calls: list[tuple[list[str], Any]] = []
        real = subprocess.run

        def recording(argv: list[str], **kwargs: Any) -> Any:
            calls.append((argv, kwargs.get("input")))
            return real(argv, **kwargs)

        values = [
            "proton-bridge",
            "host.docker.internal",
            "1143",
            "127.0.0.1",
            f"All Mail|Labels/*|Starred|{MARKER} folder",
            "protonmail",
            "protonmail-remote",
            "protonmail-local",
            "protonmail",
        ]
        with unittest.mock.patch("subprocess.run", recording):
            self.assertIsNone(oc.proton_settings_problem(values))
            problem = oc.proton_settings_problem([*values[:4], f"{MARKER} folder", *values[5:]])
        self.assertIsNotNone(problem)
        self.assertEqual(len(calls), 2)
        for argv, given in calls:
            self.assertFalse(any(MARKER in arg for arg in argv))
            self.assertIn(MARKER, given)

    def test_an_enabled_imap_account_is_refused_and_a_disabled_one_accepted(self) -> None:
        imap = second_account(
            provider="imap",
            connection={
                "host": "imap.mail.example",
                "port": 993,
                "username": "second@mail.example",
            },
            imap={"folder_roles": {"trash": "Trash"}},
        )
        del imap["proton_bridge"]
        data = fixture()
        data["accounts"].append(copy.deepcopy(imap))
        self.assert_refused(
            data,
            "accounts[1]: provider imap is defined but cannot be enabled yet (#1606); set enabled = false.",
        )
        imap["enabled"] = False
        data = fixture()
        data["accounts"].append(imap)
        config = checked(data)
        self.assertNotIn("mbsync-" + UUID_B, oc.translate(config)["compose.accounts.yml"] or "")

    def test_an_imap_account_needs_its_host_and_port(self) -> None:
        imap = second_account(provider="imap", enabled=False, imap={})
        del imap["proton_bridge"]
        data = fixture()
        data["accounts"].append(imap)
        self.assert_refused(
            data, "accounts[1].connection.host is required when accounts[1].provider is imap."
        )

    def test_no_enabled_account_at_maildir_is_refused(self) -> None:
        data = fixture()
        data["accounts"][0]["enabled"] = False
        self.assert_refused(
            data,
            'No enabled account has maildir_root = "/maildir": today\'s indexer and mcp-server read the account at /maildir only (#1605).',
        )

    def test_too_many_accounts_are_refused_before_any_is_checked(self) -> None:
        data = fixture()
        data["accounts"] = [{"zq9": MARKER}] * (oc.MAX_ACCOUNTS + 1)
        self.assert_refused(data, f"accounts may hold at most {oc.MAX_ACCOUNTS} tables.")

    def test_more_than_one_enabled_account_is_refused_until_1605(self) -> None:
        # validate()/translate() still accept several enabled accounts
        # (the #1608 Compose hardening tests, the future #1605
        # collectors); check_single_enabled_account is the launcher's
        # own, narrower cap (#1603).
        data = fixture()
        data["accounts"].append(second_account())
        config = checked(data)
        oc.translate(config)  # unrestricted: still succeeds
        with self.assertRaises(oc.ConfigError) as caught:
            oc.check_single_enabled_account(config)
        self.assertEqual(
            str(caught.exception),
            "accounts: more than one account is enabled; exactly one is accepted "
            "until per-account collectors exist (#1605).",
        )
        self.assertNotIn(MARKER, str(caught.exception))

    def test_a_single_enabled_account_passes_the_launcher_cap(self) -> None:
        data = fixture()
        data["accounts"].append(second_account(enabled=False))
        oc.check_single_enabled_account(checked(data))  # does not raise


class Reading(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def write(self, data: bytes, mode: int = 0o600) -> Path:
        path = self.dir / "config.toml"
        path.write_bytes(data)
        os.chmod(path, mode)
        return path

    def refused(self, path: Path) -> str:
        with self.assertRaises(oc.ConfigError) as caught:
            oc.load_file(path)
        text = str(caught.exception)
        self.assertNotIn(MARKER, text)
        return text

    def test_a_readable_file_is_parsed(self) -> None:
        data, base = oc.load_file(self.write((FIXTURES / "legacy.toml").read_bytes()))
        self.assertEqual(data, fixture())
        self.assertEqual(base, self.dir)

    def test_a_relative_path_gives_an_absolute_base(self) -> None:
        # Paths in the file are read relative to its directory, then
        # written for Compose; a relative base would make them relative
        # to wherever the translator ran.
        path = self.write((FIXTURES / "legacy.toml").read_bytes())
        _, base = oc.load_file(Path(os.path.relpath(path)))
        self.assertTrue(base.is_absolute())
        self.assertEqual(base, Path(os.path.abspath(self.dir)))

    def test_a_proton_check_that_cannot_run_is_refused(self) -> None:
        with unittest.mock.patch("subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaises(oc.ConfigError) as caught:
                checked(fixture())
        self.assertEqual(
            str(caught.exception),
            "The Proton settings check (bash, mbsync/proton-settings.sh) could not run (FileNotFoundError).",
        )

    def test_a_file_others_can_read_is_refused(self) -> None:
        for mode in (0o640, 0o604, 0o644, 0o660):
            with self.subTest(oct(mode)):
                path = self.write(b"schema_version = 1\n", mode)
                self.assertEqual(
                    self.refused(path),
                    "config.toml must be readable by its owner only (chmod 600): it holds addresses.",
                )

    def test_a_missing_file_and_a_directory_are_refused(self) -> None:
        self.assertEqual(self.refused(self.dir / "config.toml"), "config.toml does not exist.")
        (self.dir / "config.toml").mkdir()
        self.assertEqual(
            self.refused(self.dir / "config.toml"), "config.toml is not a regular file."
        )

    def test_a_fifo_is_refused_without_opening_it(self) -> None:
        # Opening a FIFO waits for a writer: the check comes first, and a
        # writer unblocks a regression after 2 s so it fails, not hangs.
        fifo = self.dir / "config.toml"
        os.mkfifo(fifo)
        writer = subprocess.Popen(  # nosec B603 B607 - fixed argv
            ["sh", "-c", 'sleep 2; printf x > "$1"', "sh", str(fifo)]
        )
        self.addCleanup(writer.kill)
        self.assertEqual(self.refused(fifo), "config.toml is not a regular file.")
        self.assertIsNone(writer.poll())

    def test_an_oversized_file_is_refused(self) -> None:
        path = self.write(b"#" * (oc.MAX_CONFIG_BYTES + 1))
        self.assertEqual(
            self.refused(path), f"config.toml is larger than {oc.MAX_CONFIG_BYTES} bytes."
        )

    def test_the_read_is_bounded(self) -> None:
        # The work: at most one byte more than the limit is read, however
        # long the file is.
        source = io.BytesIO(b"x" * 10_000)
        data = oc.read_bounded(source, 100)
        self.assertEqual(len(data), 101)
        self.assertEqual(source.tell(), 101)

    def test_invalid_toml_names_only_the_position(self) -> None:
        path = self.write(f'schema_version = 1\nname = "{MARKER}\\q"\n'.encode())
        self.assertRegex(
            self.refused(path), r"^config\.toml is not valid TOML \(line 2, column \d+\)\.$"
        )

    def test_text_that_is_not_utf8_is_refused(self) -> None:
        path = self.write(b'name = "\xff' + MARKER.encode() + b'"\n')
        self.assertEqual(self.refused(path), "config.toml is not UTF-8 text.")

    def test_an_integer_over_the_conversion_limit_is_refused(self) -> None:
        # Review round 2: tomllib raises a plain ValueError past Python's
        # 4300-digit int conversion limit, quoting nothing but not a TOMLDecodeError.
        path = self.write(b"schema_version = " + b"1" * 5000 + b"\n")
        self.assertEqual(self.refused(path), "config.toml holds a number too long to read.")

    def test_deep_nesting_is_refused_without_a_crash(self) -> None:
        depth = 100_000
        path = self.write(b"a = " + b"[" * depth + b"]" * depth + b"\n")
        self.assertRegex(
            self.refused(path),
            r"^config\.toml (nests arrays or tables too deeply|is not valid TOML.*)\.$",
        )


# --- The mapping -------------------------------------------------------------

# Two TOML values per projected setting and the environment values they
# give, so a translation that drops or misroutes a key fails. Keys with one
# valid value (a single-choice mode) give one pair.
SAMPLES: dict[str, list[tuple[Any, str]]] = {
    "SOURCE_MODE": [("proton-bridge", "proton-bridge")],
    "ACCOUNT_SCOPE": [
        (UUID_A, UUID_A),
        ("00000000-0000-4000-8000-0000000000a2", "00000000-0000-4000-8000-0000000000a2"),
    ],
    "BRIDGE_USER": [("a@mail.example", "a@mail.example"), ("b@mail.example", "b@mail.example")],
    "BRIDGE_IMAP_PORT": [(1144, "1144"), (2993, "2993")],
    "BRIDGE_CERT_FINGERPRINT": [("c" * 64, "c" * 64), ("D" * 64, "D" * 64)],
    "BRIDGE_HOST": [("host.orb.internal", "host.orb.internal"), ("host.internal", "host.internal")],
    "BRIDGE_CERT_HOST": [("localhost", "localhost"), ("127.0.0.2", "127.0.0.2")],
    "PROTON_IMAP_ACCOUNT_NAME": [("acct-a", "acct-a"), ("acct-b", "acct-b")],
    "PROTON_REMOTE_STORE_NAME": [("far-a", "far-a"), ("far-b", "far-b")],
    "PROTON_LOCAL_STORE_NAME": [("near-a", "near-a"), ("near-b", "near-b")],
    "PROTON_CHANNEL_NAME": [("chan-a", "chan-a"), ("chan-b", "chan-b")],
    "PROTON_FOLDER_EXCLUSIONS": [
        (["Starred", "Labels/*", "All Mail"], "Starred|Labels/*|All Mail"),
        (
            ["All Mail", "Labels/*", "Starred", "Folders/Old"],
            "All Mail|Labels/*|Starred|Folders/Old",
        ),
    ],
    "SYNC_INTERVAL": [(61, "61"), (62, "62")],
    "SYNC_DEADLINE_SECONDS": [(1001, "1001"), (999999999, "999999999")],
    "EMBED_BASE_URL": [
        ("default", "default"),
        ("http://host.docker.internal:9/v1", "http://host.docker.internal:9/v1"),
    ],
    "EMBED_MODEL": [("embed-a", "embed-a"), ("embed-b", "embed-b")],
    "EMBED_MODE": [("openai", "openai")],
    "EMBED_TIMEOUT_SECS": [(61, "61"), (61.5, "61.5")],
    "EMBED_BATCH_SIZE": [(65, "65"), (66, "66")],
    "EMBED_CONCURRENCY": [(2, "2"), (3, "3")],
    "INITIAL_INDEX_BATCH_SIZE": [(51, "51"), (52, "52")],
    "INDEXER_STEADY_STATE_BATCH_SIZE": [(9, "9"), (10, "10")],
    "INDEXER_WAL_CHECKPOINT_INTERVAL_SECS": [(60, "60"), (601, "601")],
    "INDEXER_RECOVERY_SWEEP_INTERVAL_SECS": [(61, "61"), (1801, "1801")],
    "RERANK_MODE": [("none", "none"), ("cohere", "cohere")],
    "RERANK_BASE_URL": [
        ("default", "default"),
        ("https://rerank.example", "https://rerank.example"),
    ],
    "RERANK_MODEL": [("rerank-a", "rerank-a"), ("rerank-b", "rerank-b")],
    "RERANK_CANDIDATES": [(21, "21"), (22, "22")],
    "RERANK_TIMEOUT_SECS": [(1, "1"), (2.25, "2.25")],
    "EMBED_WARMUP_TIMEOUT_SECS": [(601, "601"), (1e16, "10000000000000000")],
    "INFERENCE_MODE": [("anthropic", "anthropic"), ("openai", "openai")],
    "INFERENCE_BASE_URL": [
        ("default", "default"),
        ("http://host.docker.internal:8002/v1", "http://host.docker.internal:8002/v1"),
    ],
    "INFERENCE_MODEL": [("model-a", "model-a"), ("model-b", "model-b")],
    "INFERENCE_TIMEOUT_SECS": [(301, "301"), (302.5, "302.5")],
    "INFERENCE_MAX_TOKENS": [(1, "1"), (4096, "4096")],
    "INFERENCE_CONTEXT_TOKENS": [(8192, "8192"), (48001, "48001")],
    "INFERENCE_STRUCTURED_OUTPUT": [(False, "false"), (True, "true")],
    "INDEXER_DELETION_ENABLED": [(False, "false"), (True, "true")],
    "INDEXER_DELETION_GRACE_DAYS": [(0, "0"), (8, "8")],
    "INDEXER_DELETION_SWEEP_INTERVAL_SECS": [(60, "60"), (3601, "3601")],
    "INDEXER_DELETION_MAX_BATCH_PCT": [(0.00001, "0.00001"), (1, "1")],
    "INDEXER_MAX_ATTEMPTS": [(1, "1"), (6, "6")],
    "INDEXER_RETRY_BASE_SECONDS": [(1, "1"), (31, "31")],
    "INDEXER_MESSAGE_TIMEOUT_SECONDS": [(0, "0"), (3601, "3601")],
    "INDEXER_CHUNK_TARGET_TOKENS": [(151, "151"), (1001, "1001")],
    "INDEXER_CHUNK_MAX_TOKENS": [(1501, "1501"), (1502, "1502")],
    "INDEXER_CHUNK_OVERLAP_TOKENS": [(0, "0"), (151, "151")],
    "INDEXER_PARSE_MAX_BYTES": [(0, "0"), (50000001, "50000001")],
    "INDEXER_ATTACHMENT_EXTRACTION_ENABLED": [(False, "false"), (True, "true")],
    "INDEXER_OCR_ENABLED": [(False, "false"), (True, "true")],
    "INDEXER_ATTACHMENT_MAX_BYTES": [(1, "1"), (33554433, "33554433")],
    "INDEXER_OCR_MAX_PAGES": [(1, "1"), (21, "21")],
    "INDEXER_OCR_TIMEOUT_SECONDS": [(0, "0"), (61, "61")],
    "INDEXER_PDF_MAX_DIGITAL_PAGES": [(0, "0"), (501, "501")],
    "INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS": [(0, "0"), (2000001, "2000001")],
    "MCP_PORT": [(1, "1"), (65535, "65535")],
    "MCP_SESSION_IDLE_TIMEOUT_SECS": [(1, "1"), (1800.5, "1800.5")],
    "MCP_EXPERIMENTAL_TOOLS": [(True, "true"), (False, "false")],
}


def _toml_path(setting: oc.Setting) -> str:
    """A MAPPING key as a path into the fixture's data."""
    assert setting.toml is not None
    return setting.toml.replace("accounts[].", "accounts.0.")


class Mapping(unittest.TestCase):
    def test_every_env_example_key_is_mapped_once(self) -> None:
        mapped = [s.env for s in oc.MAPPING]
        self.assertEqual(len(mapped), len(set(mapped)))
        self.assertEqual(set(mapped), example_keys())
        self.assertGreater(len(mapped), 50)

    def test_the_example_key_scan_sees_set_and_commented_keys(self) -> None:
        keys = example_keys()
        self.assertIn("EMBED_MODEL", keys)  # set
        self.assertIn("BRIDGE_HOST", keys)  # commented out

    def test_every_schema_key_is_mapped_or_listed_once(self) -> None:
        mapped = [s.toml for s in oc.MAPPING if s.toml is not None]
        listed = [path for path, _, _ in oc.TOML_ONLY]
        self.assertEqual(len(mapped + listed), len(set(mapped + listed)))
        self.assertEqual(set(mapped + listed), set(oc.schema_leaves()))

    def test_kinds(self) -> None:
        by_kind: dict[str, set[str]] = {}
        for s in oc.MAPPING:
            by_kind.setdefault(s.kind, set()).add(s.env)
            self.assertEqual(s.toml is None, s.kind == "runtime", s.env)
            self.assertTrue(s.consumer and s.restart, s.env)
        self.assertEqual(set(by_kind), {"global", "account", "runtime"})
        self.assertEqual(by_kind["runtime"], {"MCP_TRANSPORT"})
        self.assertIn("MCP_TRANSPORT", {name for name, _ in oc.RUNTIME_OWNED})

    def test_negative_zero_projects_as_zero(self) -> None:
        # Review round 2: -0.0 passes a lower bound of 0, but "-0.0" is
        # not a form scripts/validate-env.sh accepts. Every numeric
        # setting, derived from the schema: refused or projected unsigned.
        numeric = [
            s
            for s in oc.MAPPING
            if s.toml is not None and isinstance(oc._leaf(s.toml), oc.Number | oc.Fraction)
        ]
        self.assertIn("INDEXER_DELETION_MAX_BATCH_PCT", {s.env for s in numeric})
        accepted = []
        for setting in numeric:
            with self.subTest(setting.env):
                data = fixture()
                _set(data, _toml_path(setting), -0.0)
                try:
                    config = checked(data)
                except oc.ConfigError:
                    continue
                self.assertEqual(oc.project_env(config)[setting.env], "0.0")
                accepted.append(setting.env)
        self.assertEqual(accepted, ["INDEXER_DELETION_MAX_BATCH_PCT"])
        for leaf in (oc.Number(minimum=0), oc.Fraction()):
            with self.subTest(type(leaf).__name__):
                value = leaf.check(-0.0, "x", CONFIG_DIR)
                self.assertEqual(oc._format(value), "0.0")
                self.assertEqual(oc._format(leaf.check(0, "x", CONFIG_DIR)), "0")

    def test_samples_cover_every_projected_setting(self) -> None:
        projected = {s.env for s in oc.MAPPING if s.toml is not None}
        self.assertEqual(set(SAMPLES), projected)

    def test_each_toml_key_reaches_its_setting_and_only_it(self) -> None:
        base = fixture()
        # Modes can flip: give every layer its endpoint and model.
        base["global"]["inference"] = {"base_url": "default", "model": "m"}
        base["global"]["reranking"] = {"base_url": "default", "model": "m"}
        reference = oc.project_env(checked(base))
        for setting in oc.MAPPING:
            if setting.toml is None:
                continue
            seen = set()
            for value, expected in SAMPLES[setting.env]:
                with self.subTest(setting.env, value=value):
                    data = copy.deepcopy(base)
                    _set(data, _toml_path(setting), value)
                    if setting.env == "ACCOUNT_SCOPE":
                        data["accounts"][0]["maildir_root"] = "/maildir"
                    env = oc.project_env(checked(data))
                    self.assertEqual(env[setting.env], expected)
                    changed = {k for k in env if env[k] != reference[k]}
                    self.assertLessEqual(changed, {setting.env})
                    seen.add(env[setting.env])
            self.assertEqual(len(seen), len(SAMPLES[setting.env]), setting.env)

    def test_omitted_keys_take_the_documented_defaults(self) -> None:
        # legacy.toml holds only the required keys; legacy.env every
        # setting at the default .env.example documents.
        env = oc.project_env(checked(fixture()))
        legacy = {}
        for line in (FIXTURES / "legacy.env").read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                key, _, value = line.partition("=")
                legacy[key] = value
        projected = {s.env for s in oc.MAPPING if s.toml is not None}
        self.assertEqual(
            set(legacy) - projected,
            {"MCP_TRANSPORT"},
        )
        # An unset INFERENCE_MODEL takes Compose's default, so legacy.env's
        # explicit value and the projection's empty one are the same.
        legacy["INFERENCE_MODEL"] = ""
        self.assertEqual(env, {k: v for k, v in legacy.items() if k in projected})

    def test_the_docs_hold_the_generated_tables(self) -> None:
        text = (REPO / "docs" / "setup.md").read_text(encoding="utf-8")
        begin, end = (
            "<!-- operator-config-mapping:begin -->\n",
            "<!-- operator-config-mapping:end -->",
        )
        self.assertEqual(text.count(begin), 1)
        self.assertEqual(text.count(end), 1)
        section = text.split(begin, 1)[1].split(end, 1)[0]
        self.assertEqual(section.strip(), oc.mapping_markdown().strip())


# --- Outputs -----------------------------------------------------------------


class Outputs(unittest.TestCase):
    def test_compose_env_quotes_every_value(self) -> None:
        text = oc.translate(checked(fixture()))["compose.env"]
        assert text is not None
        self.assertEqual(parse_env(text), oc.project_env(checked(fixture())))

    def test_two_accounts_get_two_isolated_collectors(self) -> None:
        data = fixture()
        data["accounts"].append(second_account())
        text = oc.translate(checked(data))["compose.accounts.yml"]
        assert text is not None
        services = text.split("\nvolumes:\n", 1)[0]
        legacy, second = services.split(f"\n  mbsync-{UUID_B}:\n")
        legacy, second = legacy + "\n", second + "\n"
        self.assertIn("\n  mbsync:\n", legacy)
        self.assertIn("      - bridge_pass\n", legacy)
        self.assertIn("      - maildir-volume:/maildir\n      - mbsync-state:/state\n", legacy)
        self.assertIn("      - bridge-net\n", legacy)
        self.assertIn(
            f"      - source: bridge_pass_{UUID_B}\n        target: bridge_pass\n", second
        )
        self.assertIn(
            f"      - maildir-{UUID_B}:/maildir\n      - mbsync-state-{UUID_B}:/state\n", second
        )
        self.assertIn(f"      - bridge-net-{UUID_B}\n", second)
        self.assertIn('BRIDGE_USER: "second@example.com"', second)
        self.assertNotIn("synthetic@example.com", second)
        self.assertNotIn("bridge_pass\n", second.replace("target: bridge_pass\n", ""))
        self.assertIn('    file: "./.secrets/bridge_pass_second.txt"', text)

    def test_a_dollar_is_not_interpolated(self) -> None:
        data = fixture()
        data["accounts"][0]["connection"]["username"] = "a$b@mail.example"
        text = oc.translate(checked(data))["compose.accounts.yml"] or ""
        self.assertIn('BRIDGE_USER: "a$$b@mail.example"', text)

    def test_the_model_key_files_are_the_compose_secret_sources(self) -> None:
        # Compose resolves only the secrets a service in the file uses, so
        # these are checked as written: docker-compose.yml's paths by
        # default, and the configured file otherwise.
        text = oc.translate(checked(fixture()))["compose.accounts.yml"] or ""
        for secret, name in (
            ("embed_api_key", "embed"),
            ("inference_api_key", "inference"),
            ("rerank_api_key", "rerank"),
        ):
            self.assertIn(f'  {secret}:\n    file: "./.secrets/{name}_api_key.txt"\n', text)
        data = fixture()
        data["global"]["embedding"]["api_key_file"] = "keys/embed.txt"  # pragma: allowlist secret
        text = oc.translate(checked(data))["compose.accounts.yml"] or ""
        self.assertIn('  embed_api_key:\n    file: "./config/keys/embed.txt"\n', text)

    def test_a_password_file_outside_the_repository_is_absolute(self) -> None:
        data = fixture()
        data["accounts"][0]["password_file"] = "/srv/zq9/pass.txt"
        text = oc.translate(checked(data))["compose.accounts.yml"] or ""
        self.assertIn('    file: "/srv/zq9/pass.txt"', text)


class Writing(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def test_directories_are_700_and_files_600(self) -> None:
        out = self.dir / ".generated"
        oc.write_outputs(out, oc.translate(checked(fixture())))
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((out / "config").stat().st_mode), 0o700)
        files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
        self.assertEqual(
            files,
            [
                "compose.accounts.yml",
                "compose.env",
                "config/authority.toml",
                "config/identity.toml",
            ],
        )
        for name in files:
            self.assertEqual(stat.S_IMODE((out / name).stat().st_mode), 0o600, name)

    def test_a_loose_directory_is_tightened(self) -> None:
        out = self.dir / ".generated"
        out.mkdir(mode=0o755)
        os.chmod(out, 0o755)  # nosec B103 - a loose directory on purpose: the writer must tighten it
        oc.write_outputs(out, oc.translate(checked(fixture())))
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)

    def test_a_linked_directory_is_refused(self) -> None:
        (self.dir / "elsewhere").mkdir()
        (self.dir / ".generated").symlink_to(self.dir / "elsewhere")
        with self.assertRaises(oc.ConfigError):
            oc.write_outputs(self.dir / ".generated", oc.translate(checked(fixture())))
        self.assertEqual(list((self.dir / "elsewhere").iterdir()), [])

    def test_an_unconfigured_identity_removes_the_old_file(self) -> None:
        out = self.dir / ".generated"
        oc.write_outputs(out, oc.translate(checked(fixture())))
        data = fixture()
        del data["accounts"][0]["identity"]
        oc.write_outputs(out, oc.translate(checked(data)))
        self.assertFalse((out / "config" / "identity.toml").exists())
        self.assertTrue((out / "config" / "authority.toml").exists())

    def test_no_temporary_file_is_left(self) -> None:
        out = self.dir / ".generated"
        oc.write_outputs(out, oc.translate(checked(fixture())))
        oc.write_outputs(out, oc.translate(checked(fixture())))
        self.assertEqual([p.name for p in out.rglob(".*tmp*")], [])


class CommandLine(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def run_cli(self, config_text: str) -> subprocess.CompletedProcess[str]:
        (self.dir / "config").mkdir(exist_ok=True)
        path = self.dir / "config" / "config.toml"
        path.write_text(config_text, encoding="utf-8")
        os.chmod(path, 0o600)
        return subprocess.run(  # nosec B603 - fixed argv
            [
                sys.executable,
                str(REPO / "scripts" / "config" / "operator_config.py"),
                "translate",
                "--config",
                str(path),
                "--out",
                str(self.dir / ".generated"),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_translate_writes_the_outputs(self) -> None:
        result = self.run_cli((FIXTURES / "legacy.toml").read_text(encoding="utf-8"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.dir / ".generated" / "compose.env").is_file())

    def run_check_secrets(self, config_text: str) -> subprocess.CompletedProcess[str]:
        (self.dir / "config").mkdir(exist_ok=True)
        path = self.dir / "config" / "config.toml"
        path.write_text(config_text, encoding="utf-8")
        os.chmod(path, 0o600)
        return subprocess.run(  # nosec B603 - fixed argv
            [
                sys.executable,
                str(REPO / "scripts" / "config" / "operator_config.py"),
                "check-secrets",
                "--config",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_check_secrets_lists_every_accounts_and_layers_secret_path(self) -> None:
        result = self.run_check_secrets((FIXTURES / "legacy.toml").read_text(encoding="utf-8"))
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [line.split("\t", 1) for line in result.stdout.splitlines()]
        self.assertEqual(len(rows), 4)  # one account + three model layers
        requirement, password = rows[0]
        self.assertEqual(requirement, "REQUIRED")  # every account's secret is required
        self.assertTrue(password.endswith("/.secrets/bridge_pass.txt"), password)
        self.assertTrue(all(Path(p).is_absolute() for _, p in rows))
        # embedding has no "none" mode (always enabled); inference and
        # reranking default to "none" in the legacy fixture, so their
        # key files may be empty.
        self.assertEqual(
            [requirement for requirement, _ in rows[1:]], ["REQUIRED", "OPTIONAL", "OPTIONAL"]
        )

    def test_check_secrets_refuses_more_than_one_enabled_account(self) -> None:
        legacy = (FIXTURES / "legacy.toml").read_text(encoding="utf-8")
        text = (
            legacy
            + "\n"
            + "\n".join(
                [
                    "[[accounts]]",
                    f'scope_uuid = "{UUID_B}"',
                    'name = "Second"',
                    'provider = "proton-bridge"',
                    f'maildir_root = "/accounts/{UUID_B}"',
                    'password_file = "../.secrets/bridge_pass_second.txt"',
                    "[accounts.connection]",
                    'username = "second@example.com"',
                    "[accounts.proton_bridge]",
                    f'certificate_fingerprint = "{"b" * 64}"',
                ]
            )
        )
        result = self.run_check_secrets(text)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stderr,
            "ERROR: accounts: more than one account is enabled; exactly one is accepted "
            "until per-account collectors exist (#1605).\n",
        )
        self.assertEqual(result.stdout, "")

    def test_translate_refuses_more_than_one_enabled_account(self) -> None:
        legacy = (FIXTURES / "legacy.toml").read_text(encoding="utf-8")
        text = (
            legacy
            + "\n"
            + "\n".join(
                [
                    "[[accounts]]",
                    f'scope_uuid = "{UUID_B}"',
                    'name = "Second"',
                    'provider = "proton-bridge"',
                    f'maildir_root = "/accounts/{UUID_B}"',
                    'password_file = "../.secrets/bridge_pass_second.txt"',
                    "[accounts.connection]",
                    'username = "second@example.com"',
                    "[accounts.proton_bridge]",
                    f'certificate_fingerprint = "{"b" * 64}"',
                ]
            )
        )
        result = self.run_cli(text)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stderr,
            "ERROR: accounts: more than one account is enabled; exactly one is accepted "
            "until per-account collectors exist (#1605).\n",
        )
        self.assertFalse((self.dir / ".generated").exists())

    def test_a_refusal_exits_1_with_fixed_text_and_writes_nothing(self) -> None:
        text = (
            (FIXTURES / "legacy.toml")
            .read_text(encoding="utf-8")
            .replace(
                'username = "synthetic@example.com"',
                f'username = "synthetic@example.com"\npasword = "{MARKER}"',
            )
        )
        result = self.run_cli(text)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stderr, "ERROR: accounts[0].connection.pasword is not a known key.\n"
        )
        self.assertNotIn(MARKER, result.stdout + result.stderr)
        self.assertFalse((self.dir / ".generated").exists())

    def test_an_output_never_replaces_an_input(self) -> None:
        # Review round 6: --out chooses the targets, so the reserved
        # default outputs do not cover them; a target that is the
        # configuration file, a configured secret or a protected file is
        # refused before anything is written or removed.
        legacy = (FIXTURES / "legacy.toml").read_text(encoding="utf-8")
        (self.dir / "config").mkdir()
        (self.dir / "out").mkdir()
        key = self.dir / "out" / "compose.env"
        key.write_text(MARKER)
        config = self.dir / "config" / "config.toml"
        config.write_text(
            legacy.replace(
                "[global.embedding]\n", '[global.embedding]\napi_key_file = "../out/compose.env"\n'
            ),
            encoding="utf-8",
        )
        os.chmod(config, 0o600)
        self.assertIn("../out/compose.env", config.read_text(encoding="utf-8"))
        alone = self.dir / "alone" / "compose.env"
        alone.parent.mkdir()
        alone.write_text(legacy, encoding="utf-8")
        os.chmod(alone, 0o600)
        protected = self.dir / "config" / "identity.toml"
        protected.write_text(MARKER)
        cases = [
            ("a configured secret", config, self.dir / "out", key),
            ("the configuration file", alone, self.dir / "alone", alone),
            ("a protected file", config, self.dir, protected),
        ]
        for description, config_path, out, target in cases:
            with self.subTest(description):
                before = target.read_bytes()
                stderr = io.StringIO()
                with (
                    unittest.mock.patch.object(oc, "REPO_ROOT", self.dir),
                    unittest.mock.patch("sys.stderr", stderr),
                ):
                    code = oc.main(["translate", "--config", str(config_path), "--out", str(out)])
                self.assertEqual(code, 1)
                self.assertRegex(
                    stderr.getvalue(),
                    r"^ERROR: --out: [a-z./]+ would replace the configuration file, a secret file "
                    r"it names or a protected file; choose another output directory\.\n$",
                )
                self.assertNotIn(MARKER, stderr.getvalue())
                self.assertEqual(target.read_bytes(), before)

    def test_outputs_are_written_with_lf_line_ends(self) -> None:
        # Review round 6: text mode would write CRLF on Windows, past the
        # byte limits render_identity and render_authority check.
        calls: list[dict[str, Any]] = []
        real = os.fdopen

        def recording(fd: int, *args: Any, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return real(fd, *args, **kwargs)

        with unittest.mock.patch("os.fdopen", recording):
            oc.write_outputs(self.dir / "w", {"compose.env": "A='1'\nB='2'\n"})
        self.assertEqual([c.get("newline") for c in calls], ["\n"])
        self.assertEqual((self.dir / "w" / "compose.env").read_bytes(), b"A='1'\nB='2'\n")


# --- Compose -----------------------------------------------------------------


def compose_json(*args: str, env_file: Path | None = None) -> dict[str, Any]:
    docker = shutil.which("docker")
    if docker is None:
        if os.environ.get("CI"):
            raise AssertionError("docker is required in CI to read the Compose configuration")
        raise unittest.SkipTest("docker is not installed")
    argv = [docker, "compose", "--project-directory", str(REPO)]
    if env_file is not None:
        argv += ["--env-file", str(env_file)]
    result = subprocess.run(  # nosec B603 - fixed argv, no shell
        [*argv, *args, "config", "--format", "json"],
        cwd=REPO,
        # Only PATH and HOME: a variable in the caller's environment would
        # override both env files.
        env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    data: dict[str, Any] = json.loads(result.stdout)
    data.pop("name", None)
    return data


class Compose(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def generate(self, data: dict[str, Any]) -> Path:
        out = self.dir / ".generated"
        oc.write_outputs(out, oc.translate(checked(data)))
        return out

    def test_the_legacy_equivalent_toml_gives_todays_compose_configuration(self) -> None:
        # Every service, volume, network and secret as Compose resolves
        # docker-compose.yml: with today's .env, and with the projection.
        out = self.generate(fixture())
        today = compose_json("-f", "docker-compose.yml", env_file=FIXTURES / "legacy.env")
        projected = compose_json("-f", "docker-compose.yml", env_file=out / "compose.env")
        self.assertEqual(projected, today)

    def test_the_legacy_collector_is_todays_mbsync_service(self) -> None:
        out = self.generate(fixture())
        today = compose_json("-f", "docker-compose.yml", env_file=FIXTURES / "legacy.env")
        generated = compose_json("-f", str(out / "compose.accounts.yml"))
        self.assertEqual(generated["services"]["mbsync"], today["services"]["mbsync"])
        for kind, names in (
            ("volumes", ("maildir-volume", "mbsync-state")),
            ("networks", ("bridge-net",)),
        ):
            for name in names:
                self.assertEqual(generated[kind][name], today[kind][name], name)
        self.assertEqual(generated["secrets"]["bridge_pass"], today["secrets"]["bridge_pass"])

    def test_two_collectors_resolve_with_only_their_own_secret_volumes_and_network(self) -> None:
        data = fixture()
        data["accounts"].append(second_account())
        generated = compose_json("-f", str(self.generate(data) / "compose.accounts.yml"))
        services = generated["services"]
        self.assertEqual(set(services), {"mbsync", f"mbsync-{UUID_B}"})
        owned = {
            "mbsync": ("bridge_pass", {"maildir-volume", "mbsync-state"}, {"bridge-net"}),
            f"mbsync-{UUID_B}": (
                f"bridge_pass_{UUID_B}",
                {f"maildir-{UUID_B}", f"mbsync-state-{UUID_B}"},
                {f"bridge-net-{UUID_B}"},
            ),
        }
        for name, (secret, volumes, networks) in owned.items():
            service = services[name]
            self.assertEqual([s["source"] for s in service["secrets"]], [secret], name)
            self.assertIn(
                service["secrets"][0].get("target", "bridge_pass"),
                ("bridge_pass", "/run/secrets/bridge_pass"),
            )
            self.assertEqual({v["source"] for v in service["volumes"]}, volumes, name)
            self.assertEqual(
                {v["target"] for v in service["volumes"]}, {"/maildir", "/state"}, name
            )
            self.assertEqual(set(service["networks"]), networks, name)
            self.assertTrue(service["read_only"], name)
            self.assertEqual(service["cap_drop"], ["ALL"], name)
            self.assertEqual(service["security_opt"], ["no-new-privileges:true"], name)
            self.assertEqual(service["pids_limit"], 128, name)
            self.assertNotIn("ports", service, name)
        # A collector Compose does not define in docker-compose.yml names
        # its non-root user, mbsync's UID and GID (mbsync/Dockerfile), as
        # compose_test.sh requires of every new service (#1608); the
        # legacy collector stays today's mbsync service.
        self.assertEqual(services[f"mbsync-{UUID_B}"]["user"], "1001:1001")
        self.assertNotIn("user", services["mbsync"])
        dockerfile = (REPO / "mbsync" / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r"groupadd --gid 1001 mbsync")
        self.assertRegex(dockerfile, r"useradd --uid 1001 \\\s+--gid 1001")
        self.assertNotEqual(
            generated["secrets"]["bridge_pass"]["file"],
            generated["secrets"][f"bridge_pass_{UUID_B}"]["file"],
        )


class GeneratedCollectorFixture(unittest.TestCase):
    """scripts/config/tests/synthetic_compose.py builds the account data
    scripts/tests/compose_test.sh's merged hardening check takes as
    ground truth for a generated collector. declared() took
    BRIDGE_CERT_FINGERPRINT from collector_env() -- the same function
    render_compose() calls to build the real render -- so a
    collector_env() regression that cross-wired the fingerprint with
    another account's would corrupt both sides alike and the merged
    hardening check would see no mismatch (review round 8, finding 6,
    security). Every other key in declared()'s environment had the same
    problem until round 9, finding 6: for example, reclassifying
    SYNC_INTERVAL so collector_env() stopped emitting it would make both
    the generated Compose file and collectors.json omit the key, and the
    full-environment comparison would still pass. declared_environment()
    now derives every key straight from the account's own validated
    fields instead, never through collector_env()."""

    def test_the_declared_environment_does_not_depend_on_collector_env(self) -> None:
        account = checked(fixture())["accounts"][0]
        expected = {
            "SOURCE_MODE": "proton-bridge",
            "ACCOUNT_SCOPE": "00000000-0000-4000-8000-000000000001",
            "BRIDGE_USER": "synthetic@example.com",
            "BRIDGE_IMAP_PORT": "1143",
            "BRIDGE_CERT_FINGERPRINT": "a" * 64,
            "BRIDGE_HOST": "host.docker.internal",
            "BRIDGE_CERT_HOST": "127.0.0.1",
            "PROTON_IMAP_ACCOUNT_NAME": "protonmail",
            "PROTON_REMOTE_STORE_NAME": "protonmail-remote",
            "PROTON_LOCAL_STORE_NAME": "protonmail-local",
            "PROTON_CHANNEL_NAME": "protonmail",
            "PROTON_FOLDER_EXCLUSIONS": "All Mail|Labels/*|Starred",
            "SYNC_INTERVAL": "60",
            "SYNC_DEADLINE_SECONDS": "86400",
        }
        self.assertEqual(oc.collector_env(account).keys(), expected.keys())
        # A reclassified setting (SYNC_INTERVAL here) makes collector_env()
        # drop the key entirely, and a cross-wired one (BRIDGE_CERT_FINGERPRINT)
        # substitutes another account's value; declared() must see neither.
        corrupted_env = {k: "c" * 64 for k in expected if k != "SYNC_INTERVAL"}
        with unittest.mock.patch.object(oc, "collector_env", return_value=corrupted_env):
            [declared] = synthetic_compose.declared({"accounts": [account]})
        self.assertEqual(declared["environment"], expected)


if __name__ == "__main__":
    unittest.main()
