"""PiecesWorld (#1767): the driver wired to the real pieces of #1759 (the
smtplib sender, the pinned-TLS capture, the supervised sync) against local
TLS responders and a stub isync. No account, no network: the responders are
on the loopback interface and the isync is a script that exits 0.

What these tests do not prove: that Bridge serves anything like this, or
that a real isync writes these files. The steps only the owner or the guest
can do (moves, drafts, cache clears, re-add) go through an operator world
that this PR does not provide."""

import json
import shutil
import stat
import sys

import pytest
from evidence_run import driver, fixtures, frozen
from evidence_run.pieces import ImapEndpoint, PiecesConfig, PiecesWorld, SmtpRoute
from evidence_run.simulated import SimulatedWorld
from evidence_run.world import WorldError
from tlsutil import ImapResponder, SmtpResponder, make_cert

SERVICE_RULES = [{"name": "welcome", "sender_domain": "service.example"}]


def _bridge(raw: bytes, n: int) -> bytes:
    """What Bridge serves: one Gluon ID topmost and one Internal ID."""
    head, sep, body = raw.partition(b"\r\n\r\n")
    fields = [f"X-Pm-Gluon-Id: SYNTHGLUON-{n}".encode(), *head.split(b"\r\n")]
    fields.append(f"X-Pm-Internal-Id: SYNTHPM{n:06d}".encode())
    return b"\r\n".join(fields) + sep + body


def _as_file(served: bytes) -> bytes:
    head, sep, body = served.replace(b"\r\n", b"\n").partition(b"\n\n")
    lines = head.split(b"\n")
    lines.insert(1, b"X-TUID: synthetic")
    return b"\n".join(lines) + sep + body


@pytest.fixture
def secret(tmp_path):
    path = tmp_path / "pass"
    path.write_text("synthetic-password\n")
    path.chmod(0o600)
    return path


@pytest.fixture
def stub_isync(tmp_path):
    path = tmp_path / "stub-isync"
    path.write_text(f"#!{sys.executable}\nprint('synthetic isync')\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def env(tmp_path, secret, stub_isync):
    imap_cert, smtp_cert = make_cert(tmp_path, "imap"), make_cert(tmp_path, "smtp")
    with ImapResponder([imap_cert], []) as imap, SmtpResponder([smtp_cert]) as smtp:
        route = SmtpRoute(
            host="127.0.0.1",
            port=smtp.port,
            mail_from="b@sender.example",
            rcpt_to="a@mail.example",
            user="synthetic",
            password_file=secret,
            cafile=smtp_cert.cert,
        )
        config = PiecesConfig(
            smtp={"B": route, "C": route, "A2": route},
            imap=ImapEndpoint("127.0.0.1", imap.port, imap_cert.fingerprint, "synthetic", secret),
            check_imap=None,
            maildir=tmp_path / "maildir",
            mbsync=str(stub_isync),
            sync_config=tmp_path / "unused.rc",
            channel="chan",
        )
        (config.maildir / "INBOX" / "cur").mkdir(parents=True)
        yield config, imap, smtp


class Delivering(PiecesWorld):
    """Delivery is the responders' job in a live run; here a send also puts
    the Bridge-shaped message on the IMAP responder and the isync-shaped
    file in the Maildir, as a Bridge and an isync would."""

    def __init__(self, config, imap, **kwargs):
        super().__init__(config, isolation_check=lambda: True, **kwargs)
        self._imap, self._n = imap, 0

    def send(self, route, raw):
        result = super().send(route, raw)
        if result == "sent":
            self._n += 1
            served = _bridge(raw, self._n)
            self._imap.messages.append(served)
            (self.config.maildir / "INBOX" / "cur" / f"{self._n}:2,S").write_bytes(_as_file(served))
        return result


def test_a_send_goes_out_through_the_smtplib_sender_as_the_bytes_given(env):
    config, _, smtp = env
    world = PiecesWorld(config, isolation_check=lambda: True)
    raw = fixtures.forged_message("r1g2", 1, "duplicated")
    assert world.send("C", raw) == "sent"
    assert smtp.state.received[0].rstrip(b"\r\n") == raw.rstrip(b"\r\n")
    assert smtp.state.received[0].count(b"X-Pm-Internal-Id:") == 2


def test_a_sync_runs_the_supervised_isync_and_reports_a_token(env):
    config, _, _ = env
    assert PiecesWorld(config, isolation_check=lambda: True).sync() == "ok"


def test_served_joins_the_captured_literals_with_the_files_isync_wrote(env):
    config, imap, _ = env
    world = Delivering(config, imap)
    for n in (1, 2):
        world.send("B", fixtures.control_message("r1g1", n))
    served = world.served("INBOX")
    assert len(served) == 2
    assert [s.uidvalidity for s in served] == [1, 1]
    for item in served:
        assert item.file_bytes == _as_file(item.raw)  # joined by the correlation token


def test_a_message_isync_has_not_written_yet_has_no_file_bytes(env):
    config, imap, _ = env
    world = Delivering(config, imap)
    world.send("B", fixtures.control_message("r1g1", 1))
    for path in (config.maildir / "INBOX" / "cur").iterdir():
        path.unlink()
    assert world.served("INBOX")[0].file_bytes is None


def test_the_maildir_is_only_read_never_written(env):
    config, imap, _ = env
    world = Delivering(config, imap)
    world.send("B", fixtures.control_message("r1g1", 1))
    before = sorted(
        (p.name, p.stat().st_mtime_ns) for p in (config.maildir / "INBOX" / "cur").iterdir()
    )
    world.served("INBOX")
    world.sync()
    after = sorted(
        (p.name, p.stat().st_mtime_ns) for p in (config.maildir / "INBOX" / "cur").iterdir()
    )
    assert before == after


def test_steps_only_the_operator_can_do_are_refused_without_one(env):
    config, _, _ = env
    world = PiecesWorld(config, isolation_check=lambda: True)
    for call in (
        lambda: world.mutate("move", "SYNTH-r1g3-1"),
        lambda: world.append(b"x", "INBOX"),
        lambda: world.h11("a", b"x"),
        lambda: world.header_pass(cold=False),
        lambda: world.race(1),
        lambda: world.operator("a"),
        lambda: world.flag_scope(),
    ):
        with pytest.raises(WorldError) as caught:
            call()
        assert caught.value.token == "operator_unavailable"
    assert world.warnings() == [] and world.restart_needed() is False
    assert world.can_prove_cold() is False


def test_the_operator_world_handles_what_the_pieces_cannot(env):
    config, _, _ = env
    operator = SimulatedWorld()
    world = PiecesWorld(config, isolation_check=lambda: True, operator=operator)
    world.append(fixtures.control_message("r1g4", 1), "Drafts")
    assert world.mutate("draft_edit", "SYNTH-r1g4-1") == "done"
    assert world.operator("a") == "done"
    assert operator.operator_log == ["a"]


def test_isolation_is_refused_when_no_targets_are_configured(env):
    config, _, _ = env
    with pytest.raises(WorldError) as caught:
        PiecesWorld(config).isolation_ok()
    assert caught.value.token == "isolation_not_configured"


def test_isolation_comes_from_the_matrix_over_the_configured_targets(env, tmp_path):
    config, _, _ = env
    targets = tmp_path / "targets.json"
    rows = [
        {
            "name": f"{kind}-{family}",
            "family": family,
            "kind": kind,
            "address": "::1" if family == "ipv6" else "127.0.0.1",
            "tcp_port": 9,
            "udp_port": 9,
        }
        for family in ("ipv4", "ipv6")
        for kind in ("allow", "block")
    ]
    targets.write_text(json.dumps(rows))
    cfg = PiecesConfig(**{**config.__dict__, "isolation_targets": targets})
    passing = lambda target, protocol, timeout: target.kind == "allow"  # noqa: E731
    leaking = lambda target, protocol, timeout: True  # noqa: E731
    assert PiecesWorld(cfg, isolation_prober=passing).isolation_ok() is True
    assert PiecesWorld(cfg, isolation_prober=leaking).isolation_ok() is False


def _check_env(tmp_path, secret, *, served_messages):
    cert = make_cert(tmp_path, "check")
    imap = ImapResponder([cert], served_messages)
    smtp_cert = make_cert(tmp_path, "check-smtp")
    smtp = SmtpResponder([smtp_cert])
    return cert, imap, smtp_cert, smtp


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda raw: raw, True),  # the second address's copy kept every forged field
        (lambda raw: raw.replace(b": SYNTHFORGED", b":\r\n SYNTHFORGED"), True),
        (
            lambda raw: raw.replace(b"X-Pm-Internal-Id: SYNTHFORGED", b"X-Dropped: SYNTHFORGED"),
            False,
        ),
        (
            lambda raw: raw.replace(
                b"X-Pm-Gluon-Id: SYNTHFORGEDGLUON", b"X-Dropped: SYNTHFORGEDGLUON"
            ),
            False,
        ),
    ],
)
def test_the_c_route_check_reads_the_copy_at_the_second_address(
    env, tmp_path, secret, mutate, expected
):
    config, _, _ = env
    check = fixtures.forged_message("r1g2", 900, "forged")
    cert, imap, smtp_cert, smtp = _check_env(tmp_path, secret, served_messages=[mutate(check)])
    with imap, smtp:
        route = SmtpRoute(
            "127.0.0.1",
            smtp.port,
            "c@sender.example",
            "c2@sender.example",
            "synthetic",
            secret,
            smtp_cert.cert,
        )
        cfg = PiecesConfig(
            **{
                **config.__dict__,
                "smtp": {**config.smtp, "C_check": route},
                "check_imap": ImapEndpoint(
                    "127.0.0.1", imap.port, cert.fingerprint, "synthetic", secret
                ),
            }
        )
        world = PiecesWorld(cfg, isolation_check=lambda: True, sleep=lambda s: None)
        assert world.route_preserves("C", check) is expected
        assert smtp.state.received  # the check message was really submitted


def test_a_check_copy_that_never_arrives_is_not_checkable(env, tmp_path, secret):
    config, _, _ = env
    cert, imap, smtp_cert, smtp = _check_env(tmp_path, secret, served_messages=[])
    with imap, smtp:
        route = SmtpRoute(
            "127.0.0.1",
            smtp.port,
            "c@sender.example",
            "c2@sender.example",
            "synthetic",
            secret,
            smtp_cert.cert,
        )
        cfg = PiecesConfig(
            **{
                **config.__dict__,
                "smtp": {**config.smtp, "C_check": route},
                "check_imap": ImapEndpoint(
                    "127.0.0.1", imap.port, cert.fingerprint, "synthetic", secret
                ),
                "check_polls": 3,
            }
        )
        world = PiecesWorld(cfg, isolation_check=lambda: True, sleep=lambda s: None)
        assert world.route_preserves("C", fixtures.forged_message("r1g2", 900, "forged")) is None


def test_the_other_routes_are_not_checkable_without_an_operator(env):
    config, _, _ = env
    world = PiecesWorld(config, isolation_check=lambda: True)
    assert world.route_preserves("B", fixtures.forged_message("r1g2", 900, "forged")) is None


def test_group_one_runs_end_to_end_on_the_real_pieces(env, tmp_path):
    directory = tmp_path / "frozen"
    shutil.copytree(frozen.FROZEN_DIR, directory)
    path = directory / "service_messages.json"
    data = json.loads(path.read_text())
    data["rules"], data["frozen"] = SERVICE_RULES, True
    data["frozen_hash"] = frozen.service_hash(SERVICE_RULES)
    path.write_text(json.dumps(data))
    config, imap, smtp = env
    world = Delivering(config, imap)
    clock = driver.VirtualClock()
    drv = driver.Driver(
        world,
        frozen.load(directory),
        clock=clock,
        sleep=clock.sleep,
        isolation_checked=True,
        tls_checked=True,
    )
    result = drv.run(("g1",))
    assert result.stop is None
    assert result.states["P1"] == "observed" and result.states["P12"] == "observed"
    assert len(smtp.state.received) == 25
    assert result.export["counts"]["isync_commands"] >= 2  # each sync is the driver's


def test_a_run_that_needs_an_operator_step_stops_with_a_fixed_token_without_one(env, tmp_path):
    directory = tmp_path / "frozen"
    shutil.copytree(frozen.FROZEN_DIR, directory)
    config, imap, _ = env
    clock = driver.VirtualClock()
    drv = driver.Driver(
        Delivering(config, imap),
        frozen.load(directory),
        clock=clock,
        sleep=clock.sleep,
        account_stage=False,
        isolation_checked=True,
    )
    result = drv.run(("g1", "g2", "g3"))
    assert result.stop is not None and result.stop[0] in {"world_error"}
    assert result.stop[1] in {"g2", "g3"}


def test_configured_fresh_paths_cannot_prove_an_independent_download(env, tmp_path):
    config, imap, _ = env
    fresh = tmp_path / "fresh-maildir"
    (fresh / "INBOX" / "cur").mkdir(parents=True)
    cfg = PiecesConfig(
        **{**config.__dict__, "fresh_sync_config": tmp_path / "fresh.rc", "fresh_maildir": fresh}
    )
    world = Delivering(cfg, imap)
    raw = fixtures.control_message("r1g3", 1)
    world.send("B", raw)
    served = _bridge(raw, 1)
    (fresh / "INBOX" / "cur" / "1:2,S").write_bytes(_as_file(served))
    files = world.fresh_files("INBOX")
    assert files is None


def test_a_fresh_download_is_not_possible_without_its_configuration_or_an_operator(env):
    config, _, _ = env
    assert PiecesWorld(config, isolation_check=lambda: True).fresh_files("INBOX") is None
    operator = SimulatedWorld()
    assert (
        PiecesWorld(config, isolation_check=lambda: True, operator=operator).fresh_files("INBOX")
        == {}
    )


def test_c_route_sender_failure_is_a_world_error(env, monkeypatch):
    from dataclasses import replace

    from evidence_run import sender

    config, _, _ = env
    cfg = replace(config, smtp={**config.smtp, "C_check": config.smtp["C"]}, check_imap=config.imap)

    def fail(*args, **kwargs):
        raise sender.SendError("bad_password_file")

    monkeypatch.setattr(sender, "send", fail)
    with pytest.raises(WorldError) as caught:
        PiecesWorld(cfg).route_preserves("C", fixtures.forged_message("r1g2", 900, "forged"))
    assert caught.value.token == "bad_password_file"


def test_each_fresh_comparison_calls_the_operator_again(env):
    config, _, _ = env

    class Independent(SimulatedWorld):
        def __init__(self):
            super().__init__()
            self.downloads = []

        def fresh_files(self, folder):
            self.downloads.append(folder)
            return {}

    operator = Independent()
    world = PiecesWorld(config, operator=operator)
    assert world.fresh_files("INBOX") == {}
    assert world.fresh_files("INBOX") == {}
    assert operator.downloads == ["INBOX", "INBOX"]


def test_missing_uidvalidity_refuses_capture_instead_of_fabricating_zero(env, monkeypatch):
    from evidence_run import tlscapture

    config, _, _ = env
    monkeypatch.setattr(
        tlscapture, "capture_state", lambda *a, **k: tlscapture.CaptureState(None, {}, {})
    )
    with pytest.raises(WorldError) as refused:
        PiecesWorld(config).served()
    assert refused.value.token == "capture_failed"
