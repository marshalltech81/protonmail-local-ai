"""Generated Compose from synthetic configurations (#1608).

Writes, for each case below, ``<out>/<case>/compose.accounts.yml`` as the
translator renders it and ``<out>/<case>/collectors.json``, the enabled
accounts the configuration declares (scope UUID, provider, Maildir root,
the password file as Compose resolves it, and the rendered environment
``collector_env()`` builds for it). The hardening checks of
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
            # finding 3 (BRIDGE_CERT_FINGERPRINT).
            #
            # Every other key here comes from collector_env(), the same
            # function render_compose() calls to build the real render,
            # so a bug in that function would corrupt both sides alike
            # and never produce a mismatch here. That is fine for a
            # routing value such as BRIDGE_USER, but BRIDGE_CERT_FINGERPRINT
            # is the one mbsync trusts to accept Bridge's certificate
            # (AGENTS.md "Bridge-Specific Guardrails"): a collector_env()
            # regression that cross-wires it with another account's
            # fingerprint would let that account's mounted Bridge
            # password reach an attacker's certificate, and this
            # construction would not catch it, agreeing with the
            # corrupted render by sharing its source (review round 8,
            # finding 6, security). password_file above is already
            # independent of collector_env() the same way; fingerprint
            # is derived the same way here, straight from the account,
            # not through collector_env(). (Pin rotation was the other
            # trust value this check covered until #1616: it is now a
            # one-run command, not configuration, and collector_env()
            # sets no environment key for it at all, so there is nothing
            # left to derive independently for it.)
            "environment": {
                **oc.collector_env(account),
                "BRIDGE_CERT_FINGERPRINT": account["proton_bridge"]["certificate_fingerprint"],
            },
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
