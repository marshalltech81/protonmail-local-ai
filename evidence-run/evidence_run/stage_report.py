"""The account-free stage report (procedure v5 1A): the file the live guard
(``frozen.authorize_live``) requires. It records two booleans, whether the
isolation matrix passed and whether the TLS pin was checked, as an
allowlisted export. It is built from the matrix's verdict and nothing else;
a failed matrix or an unchecked pin gives a report the guard refuses.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence

from . import export, runner


def build(matrix: object, *, tls_pin_checked: bool) -> dict:
    """The report from ``isolation.py``'s output (its ``verdict`` field)."""
    if not isinstance(matrix, Mapping) or matrix.get("verdict") not in ("pass", "fail"):
        raise runner.Refused("isolation_report_invalid")
    return export.build_export(
        booleans={
            "isolation_passed": matrix["verdict"] == "pass",
            "tls_pin_checked": bool(tls_pin_checked),
        }
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args.add_argument("--isolation-report", required=True, help="isolation.py's JSON output")
    args.add_argument("--tls-pin-checked", action="store_true")
    args.add_argument("--out", required=True, help="the report, written mode 600")
    opts = args.parse_args(argv)
    try:
        try:
            with open(opts.isolation_report, encoding="utf-8") as handle:
                matrix = json.load(handle)
        except OSError, ValueError:
            raise runner.Refused("isolation_report_invalid") from None
        report = build(matrix, tls_pin_checked=opts.tls_pin_checked)
    except runner.Refused as refused:
        print(f"refused: {refused.token}", file=sys.stderr)
        return 2
    fd = os.open(opts.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(export.to_json(report) + "\n")
    passed = report["booleans"]["isolation_passed"] and report["booleans"]["tls_pin_checked"]
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
