"""The isolation test matrix and its local self-test (#1759, procedure v5
1A.4): the pass/fail logic is proved here against local responders; the
real run happens in the guest."""

import dataclasses
import json
import shutil
import socket
import threading

import pytest
from evidence_run import isolation
from evidence_run.isolation import Target

CELLS = [(f, p) for f in ("ipv4", "ipv6") for p in ("tcp", "udp", "icmp")]


def targets():
    """A complete matrix: per cell one positive control and one target
    that must be blocked, each probed over that cell's protocol only."""
    out = []
    for family, protocol in CELLS:
        allow_addr = "127.0.0.1" if family == "ipv4" else "::1"
        block_addr = "192.0.2.1" if family == "ipv4" else "2001:db8::1"
        out.append(
            Target(f"allow-{family}-{protocol}", family, "allow", allow_addr, 9, 9, (protocol,))
        )
        out.append(
            Target(f"block-{family}-{protocol}", family, "block", block_addr, 9, 9, (protocol,))
        )
    return out


class Reach:
    """A fake prober: reachable iff the (target, protocol) says so."""

    def __init__(self, reachable):
        self.reachable = set(reachable)

    def __call__(self, target, protocol, timeout):
        return (target.name, protocol) in self.reachable


def ideal():
    return {(t.name, p) for t in targets() if t.kind == "allow" for p in t.protocols}


def test_a_correct_setup_passes():
    report = isolation.evaluate(targets(), Reach(ideal()), timeout=0.1)
    assert report.verdict == "pass"
    assert report.violations == 0 and report.control_failures == 0 and report.incomplete == 0


def test_a_blocked_target_that_is_reachable_is_a_violation():
    leaked = ideal() | {("block-ipv6-udp", "udp")}
    report = isolation.evaluate(targets(), Reach(leaked), timeout=0.1)
    assert report.verdict == "fail" and report.violations == 1


def test_a_positive_control_that_fails_means_the_test_proves_nothing():
    broken = ideal() - {("allow-ipv4-tcp", "tcp")}
    report = isolation.evaluate(targets(), Reach(broken), timeout=0.1)
    assert report.verdict == "fail" and report.control_failures == 1


def test_a_matrix_missing_a_cell_or_a_control_is_incomplete():
    without_v6_icmp = [
        t for t in targets() if not (t.family == "ipv6" and t.protocols == ("icmp",))
    ]
    report = isolation.evaluate(without_v6_icmp, Reach(ideal()), timeout=0.1)
    assert report.verdict == "fail" and report.incomplete >= 1
    no_controls = [t for t in targets() if t.kind != "allow" or t.family != "ipv4"]
    assert isolation.evaluate(no_controls, Reach(ideal()), timeout=0.1).incomplete >= 1


def test_the_public_disallowed_responder_is_a_blocked_target_like_any_other():
    matrix = targets() + [Target("public", "ipv4", "block", "198.51.100.7", 443, 443, ("tcp",))]
    leaked = ideal() | {("public", "tcp")}
    assert isolation.evaluate(matrix, Reach(leaked), timeout=0.1).violations == 1


def test_the_report_is_fixed_tokens_and_counts_only():
    report = isolation.evaluate(targets(), Reach(ideal()), timeout=0.1)
    text = json.dumps(report.as_dict())
    assert "127.0.0.1" not in text and "block-" not in text and "allow-" not in text
    assert set(report.as_dict()) == {
        "verdict",
        "violations",
        "control_failures",
        "incomplete",
        "cells",
    }


def _tcp_listener(family):
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1" if family == socket.AF_INET else "::1", 0))
    sock.listen(8)
    return sock


def _udp_echo(family):
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1" if family == socket.AF_INET else "::1", 0))

    def serve():
        while True:
            try:
                data, peer = sock.recvfrom(64)
            except OSError:
                return
            sock.sendto(data, peer)

    threading.Thread(target=serve, daemon=True).start()
    return sock


def test_the_real_tcp_and_udp_probers_see_a_local_responder_and_a_closed_port():
    for family, name, addr in (
        (socket.AF_INET, "ipv4", "127.0.0.1"),
        (socket.AF_INET6, "ipv6", "::1"),
    ):
        try:
            tcp, udp = _tcp_listener(family), _udp_echo(family)
        except OSError:
            pytest.skip(f"no {name} loopback in this environment")
        open_t = Target(
            "o", name, "allow", addr, tcp.getsockname()[1], udp.getsockname()[1], ("tcp", "udp")
        )
        closed = socket.socket(family, socket.SOCK_STREAM)
        closed.bind((addr, 0))
        closed_port = closed.getsockname()[1]
        closed.close()
        closed_t = Target("c", name, "block", addr, closed_port, closed_port, ("tcp", "udp"))
        assert isolation.probe_target(open_t, "tcp", 2.0) is True
        assert isolation.probe_target(open_t, "udp", 2.0) is True
        assert isolation.probe_target(closed_t, "tcp", 1.0) is False
        tcp.close()
        udp.close()


@pytest.mark.skipif(
    shutil.which("ping") is None, reason="no ping binary here; ICMP is verified in the guest"
)
def test_the_real_icmp_prober_reaches_loopback_when_ping_works():
    t = Target("l", "ipv4", "allow", "127.0.0.1", 0, 0, ("icmp",))
    if not isolation.probe_target(t, "icmp", 3.0):
        pytest.skip("ping not permitted in this environment; ICMP is verified in the guest")


def test_the_cli_exits_nonzero_on_a_violation_and_prints_only_the_report(tmp_path, capsys):
    cfg = tmp_path / "targets.json"
    cfg.write_text(json.dumps([dataclasses.asdict(t) for t in targets()]))
    code = isolation.main(
        ["--targets", str(cfg)], prober=Reach(ideal() | {("block-ipv4-icmp", "icmp")})
    )
    out = capsys.readouterr().out
    assert code == 1
    assert json.loads(out)["verdict"] == "fail"
    assert "192.0.2.1" not in out
    ok = isolation.main(["--targets", str(cfg)], prober=Reach(ideal()))
    assert ok == 0
