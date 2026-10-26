"""Generated Compose from synthetic configurations (#1608).

Writes, for each case below, ``<out>/<case>/compose.accounts.yml`` as the
translator renders it and ``<out>/<case>/collectors.json``, the enabled
accounts the configuration declares (scope UUID, provider, Maildir root,
the password file as Compose resolves it, and the rendered environment,
read independently of ``collector_env()`` by ``declared_environment()``
below). The hardening checks of
scripts/tests/compose_test.sh take the declared accounts from that file,
the input, never from the generated Compose under test, and
scripts/tests/semgrep_generated_test.sh scans each generated file.

The cases: one Proton account (fixtures/legacy.toml), and that account
plus one or two more. A Proton plus ``imap`` case waits on #1606, which
enables ``imap`` collectors.

Usage: python3 scripts/config/tests/synthetic_compose.py <out-dir>
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import operator_config as oc  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "legacy.toml"
UUID_B = "00000000-0000-4000-8000-00000000000b"
UUID_C = "00000000-0000-4000-8000-00000000000c"


def added_account(uuid: str, letter: str) -> dict[str, Any]:
    return {
        "scope_uuid": uuid,
        "name": f"Synthetic {letter}",
        "provider": oc.PROTON,
        "maildir_root": f"/accounts/{uuid}",
        "password_file": f"../.secrets/bridge_pass_{letter}.txt",
        "connection": {"username": f"synthetic-{letter}@example.com"},
        "proton_bridge": {"certificate_fingerprint": letter * 64},
    }


def cases() -> dict[str, dict[str, Any]]:
    one = tomllib.loads(FIXTURE.read_text(encoding="utf-8"))
    two = tomllib.loads(FIXTURE.read_text(encoding="utf-8"))
    two["accounts"].append(added_account(UUID_B, "b"))
    three = tomllib.loads(FIXTURE.read_text(encoding="utf-8"))
    three["accounts"] += [added_account(UUID_B, "b"), added_account(UUID_C, "c")]
    return {"one-proton": one, "two-proton": two, "three-proton": three}


def declared_environment(account: dict[str, Any]) -> dict[str, str]:
    """This account's collector environment, read straight from its own
    validated fields -- never through collector_env(), the function
    render_compose() calls to build the real render. Round 8, finding 6
    (security) fixed this for BRIDGE_CERT_FINGERPRINT alone: every other
    key still came from collector_env(), so a bug in it (for example,
    reclassifying a setting so collector_env() stops emitting it) would
    corrupt both this expectation and the real render alike, and the
    merged hardening check would see no mismatch (review round 9,
    finding 6). oc.validate() already fills every field used below with
    its schema default (operator_config.py's ACCOUNT table and
    _account_defaults) before this function ever sees the account, so
    each value here is read directly off that validated dict and
    formatted the same way _format() does (str, or "|".join for a
    list) -- never through collector_env()'s MAPPING-driven lookup.
    Scoped to a Proton account with no sync/connection overrides, the
    only shape the cases below build; an imap case (#1606) or one that
    overrides a sync/connection field needs its own expected values."""
    connection = account["connection"]
    sync = account["sync"]
    bridge = account["proton_bridge"]
    return {
        "SOURCE_MODE": account["provider"],
        "PROTON_DECLARED_ADDRESS_SCOPE": bridge["declared_address_scope"],
        "ACCOUNT_SCOPE": account["scope_uuid"],
        "BRIDGE_USER": connection["username"],
        "BRIDGE_IMAP_PORT": str(connection["port"]),
        "BRIDGE_CERT_FINGERPRINT": bridge["certificate_fingerprint"],
        "BRIDGE_HOST": connection["host"],
        "BRIDGE_CERT_HOST": bridge["certificate_host"],
        "PROTON_IMAP_ACCOUNT_NAME": sync["imap_account_name"],
        "PROTON_REMOTE_STORE_NAME": sync["remote_store_name"],
        "PROTON_LOCAL_STORE_NAME": sync["local_store_name"],
        "PROTON_CHANNEL_NAME": sync["channel_name"],
        "PROTON_FOLDER_EXCLUSIONS": "|".join(sync["mailboxes"]["exclude"]),
        "SYNC_INTERVAL": str(sync["interval_seconds"]),
        "SYNC_DEADLINE_SECONDS": str(sync["deadline_seconds"]),
    }


def declared(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "scope_uuid": account["scope_uuid"],
            "provider": account["provider"],
            "maildir_root": account["maildir_root"],
            "password_file": os.path.normpath(account["password_file"]),
            # The rendered environment this account's collector must
            # carry (compose_test.sh's merged hardening check, review
            # round 6, finding 3): the key-name check alone let a
            # renderer regression drop a required value, such as
            # BRIDGE_USER, or empty one, such as BRIDGE_CERT_FINGERPRINT,
            # without tripping anything. Comparing every declared value
            # also subsumes the trust-value check added for round 5,
            # finding 3 (BRIDGE_CERT_FINGERPRINT). Derived independently
            # of collector_env() by declared_environment() above (review
            # round 9, finding 6).
            "environment": declared_environment(account),
        }
        for account in config["accounts"]
        if account["enabled"]
    ]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: synthetic_compose.py <out-dir>", file=sys.stderr)
        return 2
    out = Path(argv[0])
    for name, data in cases().items():
        # Validated as if the file were config/config.toml, so relative
        # paths resolve in this checkout as they do for an operator.
        config = oc.validate(data, oc.REPO_ROOT / "config")
        text = oc.translate(config)["compose.accounts.yml"]
        assert text is not None
        case = out / name
        case.mkdir(parents=True, exist_ok=True)
        (case / "compose.accounts.yml").write_text(text, encoding="utf-8")
        (case / "collectors.json").write_text(json.dumps(declared(config)), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
