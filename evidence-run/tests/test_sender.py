"""The stdlib smtplib external sender (#1759, procedure v5 2.4)."""

import pytest
from evidence_run import sender
from tlsutil import SmtpResponder, make_cert

RAW = (
    b"From: c@sender.example\r\nTo: a@mail.example\r\nSubject: SYNTH-r-1\r\n"
    b"Message-ID: <s1@sender.example>\r\n"
    b"X-Pm-Internal-Id: SYNTHFORGED-1\r\nX-Pm-Internal-Id: SYNTHFORGED-2\r\n"
    b"\r\n.starts with a dot\r\nbody\r\n"
)


@pytest.fixture
def secret(tmp_path):
    path = tmp_path / "smtp_pass"
    path.write_text("synthetic-smtp-password\n")
    path.chmod(0o600)
    return path


def _send(server, cert, secret, raw=RAW):
    return sender.send(
        raw,
        host="127.0.0.1",
        port=server.port,
        mail_from="c@sender.example",
        rcpt_to="a@mail.example",
        user="synthetic-user",
        password_file=secret,
        cafile=cert.cert,
    )


def test_the_raw_bytes_are_delivered_as_submitted_duplicates_included(tmp_path, secret):
    cert = make_cert(tmp_path, "a")
    with SmtpResponder([cert]) as server:
        result = _send(server, cert, secret)
    assert result.token == "sent"
    received = server.state.received[0]
    assert received.rstrip(b"\r\n") == RAW.rstrip(b"\r\n")
    assert received.count(b"X-Pm-Internal-Id:") == 2  # not rewritten or merged


def test_a_certificate_the_client_does_not_trust_is_refused_before_auth(tmp_path, secret):
    served, trusted = make_cert(tmp_path, "served"), make_cert(tmp_path, "trusted")
    with SmtpResponder([served]) as server:
        result = _send(server, trusted, secret)
    assert result.token == "tls_failed"
    assert server.state.auth_attempts == 0
    assert server.state.commands == []


def test_a_connection_that_drops_after_data_is_an_ambiguous_outcome(tmp_path, secret):
    cert = make_cert(tmp_path, "a")
    with SmtpResponder([cert], drop_after_data=True) as server:
        result = _send(server, cert, secret)
    assert result.token == "ambiguous"


def test_a_refused_recipient_is_a_refusal_not_an_ambiguity(tmp_path, secret):
    cert = make_cert(tmp_path, "a")
    with SmtpResponder([cert], refuse_rcpt=True) as server:
        result = _send(server, cert, secret)
    assert result.token == "refused"
    assert server.state.received == []


def test_a_password_file_that_is_not_mode_600_is_refused_before_connecting(tmp_path):
    cert = make_cert(tmp_path, "a")
    loose = tmp_path / "loose"
    loose.write_text("x\n")
    loose.chmod(0o644)
    with SmtpResponder([cert]) as server:
        with pytest.raises(sender.SendError) as err:
            _send(server, cert, loose)
        assert err.value.token == "password_file_mode"
        assert server.connections == 0


def test_the_password_never_appears_in_a_result_or_error(tmp_path, secret):
    served, trusted = make_cert(tmp_path, "s"), make_cert(tmp_path, "t")
    with SmtpResponder([served]) as server:
        result = _send(server, trusted, secret)
    assert "synthetic-smtp-password" not in repr(result)
