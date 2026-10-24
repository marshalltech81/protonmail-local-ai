"""The isolation test matrix (procedure v5 1A.4), run in the guest.

Targets are listed in a JSON file: for every family (IPv4, IPv6) and
protocol (TCP, UDP, ICMP) at least one ``allow`` target (a positive
control: reachable, or the test proves nothing) and at least one ``block``
target (must be unreachable): the host's addresses, the LAN gateway,
another LAN address, the vmnet addresses and a disallowed public
responder. The report holds counts and a verdict only; no address leaves
this module. A missing cell or control is itself a failure.

The pass/fail logic is ``evaluate``; it is proved against local responders
in the tests. ``probe_target`` is the real prober; the ICMP one shells out
to ``ping``, and its exact flags are confirmed in the guest.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

FAMILIES = ("ipv4", "ipv6")
PROTOCOLS = ("tcp", "udp", "icmp")
_PROBE = b"evidence-run"


@dataclass(frozen=True)
class Target:
    name: str
    family: str  # ipv4 | ipv6
    kind: str  # allow | block
    address: str
    tcp_port: int
    udp_port: int
    protocols: tuple[str, ...] = PROTOCOLS


Prober = Callable[[Target, str, float], bool]


@dataclass(frozen=True)
class Report:
    verdict: str
    violations: int
    control_failures: int
    incomplete: int
    cells: int

    def as_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "violations": self.violations,
            "control_failures": self.control_failures,
            "incomplete": self.incomplete,
            "cells": self.cells,
        }


def evaluate(targets: Sequence[Target], prober: Prober, *, timeout: float) -> Report:
    violations = control_failures = probed = 0
    covered: set[tuple[str, str, str]] = set()
    for target in targets:
        for protocol in target.protocols:
            covered.add((target.family, protocol, target.kind))
            reachable = prober(target, protocol, timeout)
            probed += 1
            if target.kind == "block" and reachable:
                violations += 1
            elif target.kind == "allow" and not reachable:
                control_failures += 1
    incomplete = sum(
        1
        for family in FAMILIES
        for protocol in PROTOCOLS
        for kind in ("allow", "block")
        if (family, protocol, kind) not in covered
    )
    ok = violations == control_failures == incomplete == 0
    return Report("pass" if ok else "fail", violations, control_failures, incomplete, probed)


def _af(target: Target) -> int:
    return socket.AF_INET6 if target.family == "ipv6" else socket.AF_INET


def _tcp(target: Target, timeout: float) -> bool:
    try:
        with socket.socket(_af(target), socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((target.address, target.tcp_port))
        return True
    except OSError:
        return False


def _udp(target: Target, timeout: float) -> bool:
    """Reachable only if the responder echoes: a datagram with no answer is
    indistinguishable from a drop, which is the safe reading for a block
    target and a failed control for an allow target."""
    try:
        with socket.socket(_af(target), socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(_PROBE, (target.address, target.udp_port))
            sock.recvfrom(64)
        return True
    except OSError:
        return False


def ping_command(target: Target, timeout: float, platform: str | None = None) -> list[str]:
    platform = platform or sys.platform
    wait = str(max(1, int(timeout)))
    if platform == "darwin":
        return (
            ["ping6", "-c", "1", target.address]
            if target.family == "ipv6"
            else ["ping", "-c", "1", "-t", wait, target.address]
        )
    flags = ["-6"] if target.family == "ipv6" else []
    return ["ping", *flags, "-c", "1", "-W", wait, target.address]


def _icmp(target: Target, timeout: float) -> bool:
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv built from a validated target
            ping_command(target, timeout),
            capture_output=True,
            timeout=timeout + 3,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return False  # a missing ping fails the ICMP positive control, never passes a block
    return done.returncode == 0


def probe_target(target: Target, protocol: str, timeout: float) -> bool:
    if protocol == "tcp":
        return _tcp(target, timeout)
    if protocol == "udp":
        return _udp(target, timeout)
    if protocol == "icmp":
        return _icmp(target, timeout)
    raise ValueError("protocol is tcp, udp or icmp")


def load_targets(path: str) -> list[Target]:
    with open(path, encoding="utf-8") as handle:
        rows = json.load(handle)
    return [Target(**{**row, "protocols": tuple(row.get("protocols", PROTOCOLS))}) for row in rows]


def main(argv: Sequence[str] | None = None, *, prober: Prober = probe_target) -> int:
    args = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args.add_argument("--targets", required=True, help="JSON list of targets")
    args.add_argument("--timeout", type=float, default=3.0)
    opts = args.parse_args(argv)
    report = evaluate(load_targets(opts.targets), prober, timeout=opts.timeout)
    print(json.dumps(report.as_dict(), sort_keys=True))
    return 0 if report.verdict == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
