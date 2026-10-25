"""The pinned-TLS IMAP capture (#1759, procedure v5 1.9, 5.3)."""

import pytest
from evidence_run import tlscapture
from tlsutil import WRITE_COMMANDS, ImapResponder, make_cert

MESSAGES = [
    b"From: a@mail.example\r\nSubject: first\r\nMessage-ID: <1@mail.example>\r\n\r\none\r\n",
    b"From: a@mail.example\r\nSubject: second\r\nMessage-ID: <2@mail.example>\r\n\r\ntwo\r\n",
]


@pytest.fixture
def secret(tmp_path):
    path = tmp_path / "imap_pass"
    path.write_text("synthetic-imap-password\n")
    path.chmod(0o600)
    return path


def _capture(server, cert, secret, **kwargs):
    return tlscapture.capture(
        "127.0.0.1",
        server.port,
        expected_fingerprint=cert.fingerprint,
        user="synthetic-user",
        password_file=secret,
        **kwargs,
    )


def test_the_served_literals_are_captured_read_only(tmp_path, secret):
    cert = make_cert(tmp_path, "a")
    with ImapResponder([cert], MESSAGES) as server:
        captured = _capture(server, cert, secret)
    assert [captured[uid] for uid in sorted(captured)] == MESSAGES
    assert server.logins == [("synthetic-user", "synthetic-imap-password")]
    # EXAMINE and a PEEK fetch; never a command that changes the far side.
    assert "EXAMINE" in server.commands and "SELECT" not in server.commands
    assert not set(server.commands) & (WRITE_COMMANDS | {f"UID {c}" for c in WRITE_COMMANDS})


def test_a_wrong_certificate_is_refused_before_login(tmp_path, secret):
    served, pinned = make_cert(tmp_path, "served"), make_cert(tmp_path, "pinned")
    with ImapResponder([served], MESSAGES) as server:
        with pytest.raises(tlscapture.CaptureError) as err:
            tlscapture.capture(
                "127.0.0.1",
                server.port,
                expected_fingerprint=pinned.fingerprint,
                user="synthetic-user",
                password_file=secret,
            )
    assert err.value.token == "fingerprint_mismatch"
    assert server.logins == []
    assert "LOGIN" not in server.commands  # no credential left the client


def test_a_certificate_swapped_after_the_fingerprint_check_is_refused_before_login(
    tmp_path, secret
):
    """The preliminary look and the real connection are two connections;
    the second trusts only the certificate the first showed, so a swap
    between them cannot get credentials."""
    good, swapped = make_cert(tmp_path, "good"), make_cert(tmp_path, "swapped")
    with ImapResponder([good, swapped], MESSAGES) as server:
        with pytest.raises(tlscapture.CaptureError) as err:
            _capture(server, good, secret)
    assert err.value.token == "tls_verification_failed"
    assert server.logins == []


def test_a_password_file_that_is_not_mode_600_is_refused_before_connecting(tmp_path):
    cert = make_cert(tmp_path, "a")
    loose = tmp_path / "loose"
    loose.write_text("synthetic\n")
    loose.chmod(0o644)
    with ImapResponder([cert], MESSAGES) as server:
        with pytest.raises(tlscapture.CaptureError) as err:
            _capture(server, cert, loose)
        assert err.value.token == "password_file_mode"
        assert server.connections == 0


def test_the_password_never_appears_in_an_error(tmp_path, secret):
    pinned, served = make_cert(tmp_path, "p"), make_cert(tmp_path, "s")
    with ImapResponder([served], MESSAGES) as server:
        with pytest.raises(tlscapture.CaptureError) as err:
            tlscapture.capture(
                "127.0.0.1",
                server.port,
                expected_fingerprint=pinned.fingerprint,
                user="synthetic-user",
                password_file=secret,
            )
    assert "synthetic-imap-password" not in str(err.value) + repr(err.value)


def test_the_captured_bytes_are_bounded(tmp_path, secret):
    cert = make_cert(tmp_path, "a")
    big = [b"X-Fill: " + b"a" * 5000 + b"\r\n\r\nbody\r\n"] * 3
    with ImapResponder([cert], big) as server:
        with pytest.raises(tlscapture.CaptureError) as err:
            _capture(server, cert, secret, max_total_bytes=8000)
    assert err.value.token == "capture_cap"


def test_the_fingerprint_text_is_validated():
    with pytest.raises(tlscapture.CaptureError) as err:
        tlscapture.normalize_fingerprint("not-a-fingerprint")
    assert err.value.token == "fingerprint_format"
    assert tlscapture.normalize_fingerprint("AB:" * 31 + "AB") == "ab" * 32


def test_a_failed_size_fetch_is_not_an_empty_folder(tmp_path, secret):
    cert = make_cert(tmp_path, "a")
    with ImapResponder([cert], MESSAGES, fail_size_fetch=True) as server:
        with pytest.raises(tlscapture.CaptureError) as caught:
            _capture(server, cert, secret)
    assert str(caught.value) == "size_fetch_failed"


def test_an_empty_folder_is_captured_as_empty_without_a_fetch(tmp_path, secret):
    cert = make_cert(tmp_path, "a")
    with ImapResponder([cert], []) as server:
        assert _capture(server, cert, secret) == {}
    assert "UID FETCH" not in server.commands


def test_a_folder_name_with_spaces_is_quoted(tmp_path, secret):
    cert = make_cert(tmp_path, "a")
    with ImapResponder([cert], MESSAGES) as server:
        captured = _capture(server, cert, secret, folder="Recovered Messages")
    assert [captured[uid] for uid in sorted(captured)] == MESSAGES
    assert server.mailboxes == ['"Recovered Messages"']


def test_quote_and_backslash_in_a_folder_name_are_escaped(tmp_path, secret):
    cert = make_cert(tmp_path, "a")
    with ImapResponder([cert], MESSAGES) as server:
        _capture(server, cert, secret, folder='odd "name" \\ here')
    assert server.mailboxes == ['"odd \\"name\\" \\\\ here"']


@pytest.mark.parametrize("folder", ["a\r\nb", "a\nb", "a\x00b", ""])
def test_a_folder_name_that_cannot_be_sent_is_refused_before_connecting(tmp_path, secret, folder):
    cert = make_cert(tmp_path, "a")
    with ImapResponder([cert], MESSAGES) as server:
        with pytest.raises(tlscapture.CaptureError) as caught:
            _capture(server, cert, secret, folder=folder)
    assert str(caught.value) == "bad_folder_name"
    assert server.commands == []
