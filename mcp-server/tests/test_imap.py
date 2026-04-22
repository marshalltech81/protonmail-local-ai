"""Tests for src.lib.imap.

Covers the security-critical synchronous paths: TLS context construction,
the fail-closed insecure-IMAP refusal, error redaction, and the pure
RFC822 parser. Async IMAP network methods (fetch/list/move/flag) are out
of scope here and require aioimaplib-level mocking.
"""

import asyncio
import ssl
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.lib.imap import IMAPClient

# Self-signed RSA-2048 cert, CN=test, valid 2026-04-22..2036-04-19.
# Used only to satisfy ssl.SSLContext.load_verify_locations in TLS-context tests.
# No secrets here; this cert is never presented to any network peer.
_TEST_CERT_PEM = """-----BEGIN CERTIFICATE-----
MIIC/zCCAeegAwIBAgIUelJn/+8Ywn0zfR/2d3BAY2c8EjIwDQYJKoZIhvcNAQEL
BQAwDzENMAsGA1UEAwwEdGVzdDAeFw0yNjA0MjIwMDE0MTNaFw0zNjA0MTkwMDE0
MTNaMA8xDTALBgNVBAMMBHRlc3QwggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEK
AoIBAQDh69JYlodoaRBdtL87pVcJTnKTjDnwARbLMWy9o/bIdUdeUCv51KnUyBCL
DErAUnYAOcgNkXz+DM0cykUzZ4tZhI/KKQrr+6FAKh3T6trsIa9Dt+O897WCHryB
L0b3WRYkQgbBFTN72o7uN9aaI6cMa8h00jhJRWVe+ffpmoXD6i/G/c7HDxpI5dWx
Q6MBWleAf8WHCE8iTvY5w25xaw5h4/DM3Y9JjR1BGFpB2mMtmjix5IYyPp6/PRI5
+3qpuEsQF+x9b9hlfNaijIDft24mJpi/S0zD/PCO3OrWBzbBHOYrdtDzGLVlz/6C
G3lRKiImfQu//brjMW5BMx7PJUFBAgMBAAGjUzBRMB0GA1UdDgQWBBTnp1qGynXH
DNpeh95I+3yxKWv1uzAfBgNVHSMEGDAWgBTnp1qGynXHDNpeh95I+3yxKWv1uzAP
BgNVHRMBAf8EBTADAQH/MA0GCSqGSIb3DQEBCwUAA4IBAQDGRZ8i9YhPIWM4e28n
ygS9sn+hQQQ0lOmoTZKdu2GJ1Ie619FdUb/KnQoCcbaHq8+QzM+Y72haNCxTvhl6
zLTMg02ci4L9g3qhKtfwMro0G+El6MWG6emcQN/3p/CUYKDboeEGkczx6VeNeqd6
B59hmGEoWEXhCBSkwY/hm1XeBw1Yst6JjbG/imQcUMaYnUFxKXpJEGafYtfLxEeE
3KTPEPHU6ixSlv+3o01JYD2X3GP0W2bVLb/+4kbn9AzypooivnDz9UxLuI0KeAD8
UhrbGSMB8X4DlxDN68T4zyGARibWyiSRsWsRbyERkKuw+UOIymC5wVNh9UfzGfUq
I/Hp
-----END CERTIFICATE-----
"""


def _make_client(tmp_path: Path, **overrides) -> IMAPClient:
    cert = tmp_path / "bridge-cert.pem"
    cert.write_text(_TEST_CERT_PEM)
    defaults = dict(
        host="protonmail-bridge",
        imap_port=1143,
        user="bridge-user@example.com",
        password="super-secret-pass",  # pragma: allowlist secret
        smtp_port=1025,
        tls_cert_file=str(cert),
        use_implicit_imap_tls=False,
    )
    defaults.update(overrides)
    return IMAPClient(**defaults)


class TestTlsContext:
    def test_returns_hardened_context_when_cert_present(self, tmp_path: Path):
        client = _make_client(tmp_path)
        ctx = client._tls_context()
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.check_hostname is True
        assert ctx.verify_mode == ssl.CERT_REQUIRED

    def test_refuses_when_cert_file_unset(self, tmp_path: Path):
        client = _make_client(tmp_path, tls_cert_file=None)
        with pytest.raises(RuntimeError, match="Pinned Bridge TLS certificate path"):
            client._tls_context()

    def test_refuses_when_cert_file_missing(self, tmp_path: Path):
        client = _make_client(tmp_path, tls_cert_file=str(tmp_path / "does-not-exist.pem"))
        with pytest.raises(RuntimeError, match="not found"):
            client._tls_context()


class TestFailClosedConnect:
    def test_refuses_plaintext_starttls_path(self, tmp_path: Path):
        client = _make_client(tmp_path, use_implicit_imap_tls=False)
        with pytest.raises(RuntimeError, match="Refusing insecure live IMAP login"):
            asyncio.run(client._connect())

    def test_refuses_when_cert_missing_even_with_implicit_tls(self, tmp_path: Path):
        # Even with implicit TLS, we must still have a pinned cert; without one
        # _tls_context raises before we ever attempt to open a socket.
        client = _make_client(tmp_path, use_implicit_imap_tls=True, tls_cert_file=None)
        with pytest.raises(RuntimeError, match="Pinned Bridge TLS certificate path"):
            asyncio.run(client._connect())


class TestSafeError:
    def test_redacts_configured_password(self, tmp_path: Path):
        client = _make_client(tmp_path, password="hunter2-secret")
        err = RuntimeError("auth failed with hunter2-secret on login")
        assert "hunter2-secret" not in client._safe_error(err)
        assert "[REDACTED]" in client._safe_error(err)


class TestParseFullMessage:
    def _wrap_fetch_bytes(self, raw: bytes) -> bytes:
        """aioimaplib surfaces fetched messages as raw RFC822 bytes; mirror that."""
        return raw

    def test_parses_simple_plaintext_message(self, tmp_path: Path):
        client = _make_client(tmp_path)
        msg = EmailMessage()
        msg["Message-ID"] = "<abc@example.com>"
        msg["From"] = "alice@example.com"
        msg["To"] = "bob@example.com, carol@example.com"
        msg["Subject"] = "hello"
        msg["Date"] = "Mon, 01 Jan 2024 09:00:00 +0000"
        msg.set_content("plain body text")

        result = client._parse_full_message(bytes(msg), "INBOX", "42")
        assert result.message_id == "abc@example.com"
        assert result.subject == "hello"
        assert "plain body text" in result.body_text
        assert result.to_addrs == ["bob@example.com", "carol@example.com"]
        assert result.folder == "INBOX"
        assert result.imap_uid == "42"
        assert result.attachments == []

    def test_parses_multipart_with_attachment(self, tmp_path: Path):
        client = _make_client(tmp_path)
        msg = EmailMessage()
        msg["Message-ID"] = "<m2@example.com>"
        msg["From"] = "a@x"
        msg["To"] = "b@x"
        msg["Subject"] = "with attachment"
        msg["Date"] = "Mon, 01 Jan 2024 09:00:00 +0000"
        msg.set_content("text body")
        msg.add_alternative("<p>html body</p>", subtype="html")
        msg.add_attachment(
            b"binary-data",
            maintype="application",
            subtype="octet-stream",
            filename="report.pdf",
        )

        result = client._parse_full_message(bytes(msg), "INBOX", "7")
        assert "text body" in result.body_text
        assert "<p>html body</p>" in result.body_html
        assert len(result.attachments) == 1
        assert result.attachments[0]["filename"] == "report.pdf"
        assert result.attachments[0]["content_type"] == "application/octet-stream"

    def test_falls_back_to_utcnow_on_unparseable_date(self, tmp_path: Path):
        client = _make_client(tmp_path)
        raw = (
            b"Message-ID: <x@x>\r\n"
            b"From: a@x\r\n"
            b"To: b@x\r\n"
            b"Subject: s\r\n"
            b"Date: totally not a date\r\n"
            b"\r\n"
            b"body\r\n"
        )
        result = client._parse_full_message(raw, "INBOX", "1")
        # Should not raise; date is a fallback datetime.
        assert result.date is not None


def _mock_imap_client() -> MagicMock:
    """Build an AsyncMock that mimics the aioimaplib.IMAP4 API surface used here."""
    client = MagicMock()
    client.wait_hello_from_server = AsyncMock()
    client.login = AsyncMock()
    client.select = AsyncMock()
    client.logout = AsyncMock()
    client.search = AsyncMock()
    client.fetch = AsyncMock()
    client.list = AsyncMock()
    client.copy = AsyncMock()
    client.store = AsyncMock()
    client.expunge = AsyncMock()
    return client


class TestAsyncIMAPOperations:
    """Cover the async aioimaplib-backed methods with an in-process mock."""

    def test_fetch_message_returns_parsed_message(self, tmp_path: Path):
        raw_eml = (
            b"Message-ID: <found@example.com>\r\n"
            b"From: a@x\r\n"
            b"To: b@x\r\n"
            b"Subject: s\r\n"
            b"Date: Mon, 01 Jan 2024 09:00:00 +0000\r\n"
            b"\r\n"
            b"body\r\n"
        )
        mock = _mock_imap_client()
        mock.search.return_value = (None, [b"42"])
        mock.fetch.return_value = (None, [None, raw_eml])

        client = _make_client(tmp_path, use_implicit_imap_tls=True)
        with patch("src.lib.imap.aioimaplib.IMAP4_SSL", return_value=mock):
            result = asyncio.run(client.fetch_message("found@example.com"))
        assert result is not None
        assert result.message_id == "found@example.com"

    def test_fetch_message_returns_none_when_no_match(self, tmp_path: Path):
        mock = _mock_imap_client()
        mock.search.return_value = (None, [b""])

        client = _make_client(tmp_path, use_implicit_imap_tls=True)
        with patch("src.lib.imap.aioimaplib.IMAP4_SSL", return_value=mock):
            assert asyncio.run(client.fetch_message("missing@example.com")) is None

    def test_fetch_message_swallows_errors_and_returns_none(self, tmp_path: Path):
        mock = _mock_imap_client()
        mock.search.side_effect = RuntimeError("boom")

        client = _make_client(tmp_path, use_implicit_imap_tls=True)
        with patch("src.lib.imap.aioimaplib.IMAP4_SSL", return_value=mock):
            assert asyncio.run(client.fetch_message("err@example.com")) is None

    def test_list_folders_parses_imap_list_response(self, tmp_path: Path):
        mock = _mock_imap_client()
        mock.list.return_value = (
            None,
            [b'(\\HasNoChildren) "." "INBOX"', b'(\\HasNoChildren) "." "Archive"'],
        )

        client = _make_client(tmp_path, use_implicit_imap_tls=True)
        with patch("src.lib.imap.aioimaplib.IMAP4_SSL", return_value=mock):
            folders = asyncio.run(client.list_folders())
        names = [f["name"] for f in folders]
        assert "INBOX" in names
        assert "Archive" in names

    def test_list_folders_returns_empty_on_error(self, tmp_path: Path):
        mock = _mock_imap_client()
        mock.list.side_effect = RuntimeError("down")

        client = _make_client(tmp_path, use_implicit_imap_tls=True)
        with patch("src.lib.imap.aioimaplib.IMAP4_SSL", return_value=mock):
            assert asyncio.run(client.list_folders()) == []

    def test_move_message_success(self, tmp_path: Path):
        mock = _mock_imap_client()
        client = _make_client(tmp_path, use_implicit_imap_tls=True)
        with patch("src.lib.imap.aioimaplib.IMAP4_SSL", return_value=mock):
            assert asyncio.run(client.move_message("7", "INBOX", "Archive")) is True
        mock.copy.assert_awaited_once_with("7", "Archive")
        mock.expunge.assert_awaited_once()

    def test_move_message_returns_false_on_error(self, tmp_path: Path):
        mock = _mock_imap_client()
        mock.copy.side_effect = RuntimeError("nope")
        client = _make_client(tmp_path, use_implicit_imap_tls=True)
        with patch("src.lib.imap.aioimaplib.IMAP4_SSL", return_value=mock):
            assert asyncio.run(client.move_message("7", "INBOX", "Archive")) is False

    def test_set_flag_adds_and_removes(self, tmp_path: Path):
        mock = _mock_imap_client()
        client = _make_client(tmp_path, use_implicit_imap_tls=True)
        with patch("src.lib.imap.aioimaplib.IMAP4_SSL", return_value=mock):
            assert asyncio.run(client.set_flag("1", "INBOX", "\\Seen", True)) is True
            assert asyncio.run(client.set_flag("1", "INBOX", "\\Seen", False)) is True
        # First call uses +FLAGS, second uses -FLAGS.
        calls = [c.args for c in mock.store.await_args_list]
        assert calls[0][1] == "+FLAGS"
        assert calls[1][1] == "-FLAGS"

    def test_set_flag_returns_false_on_error(self, tmp_path: Path):
        mock = _mock_imap_client()
        mock.store.side_effect = RuntimeError("nope")
        client = _make_client(tmp_path, use_implicit_imap_tls=True)
        with patch("src.lib.imap.aioimaplib.IMAP4_SSL", return_value=mock):
            assert asyncio.run(client.set_flag("1", "INBOX", "\\Seen", True)) is False


class TestSendEmail:
    def test_sends_plaintext_message(self, tmp_path: Path):
        client = _make_client(tmp_path)
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        smtp.__exit__.return_value = False
        with patch("src.lib.imap.smtplib.SMTP", return_value=smtp):
            ok = client.send_email(
                to=["bob@example.com"],
                subject="hi",
                body="plain text",
            )
        assert ok is True
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with(client.user, client.password)
        smtp.sendmail.assert_called_once()

    def test_sends_html_message_with_cc_and_references(self, tmp_path: Path):
        client = _make_client(tmp_path)
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        smtp.__exit__.return_value = False
        with patch("src.lib.imap.smtplib.SMTP", return_value=smtp):
            ok = client.send_email(
                to=["bob@example.com"],
                subject="hi",
                body="<p>hi</p>",
                body_format="html",
                cc=["eve@example.com"],
                bcc=["oversight@example.com"],
                in_reply_to="<prev@example.com>",
                references="<root@example.com>",
            )
        assert ok is True
        # All three recipient buckets should appear in the envelope recipients list.
        envelope_recipients = smtp.sendmail.call_args.args[1]
        assert "bob@example.com" in envelope_recipients
        assert "eve@example.com" in envelope_recipients
        assert "oversight@example.com" in envelope_recipients

    def test_returns_false_on_smtp_error(self, tmp_path: Path):
        client = _make_client(tmp_path)
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        smtp.__exit__.return_value = False
        smtp.sendmail.side_effect = RuntimeError("rejected")
        with patch("src.lib.imap.smtplib.SMTP", return_value=smtp):
            ok = client.send_email(to=["b@x"], subject="s", body="t")
        assert ok is False
