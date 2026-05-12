"""Tests for src.lib.security redaction helpers."""

from src.lib.security import (
    redact_sensitive_text,
    safe_exception_text,
    safe_provider_exception_text,
)


class TestRedactSensitiveText:
    def test_leaves_benign_text_unchanged(self):
        assert redact_sensitive_text("hello world") == "hello world"

    def test_redacts_explicit_secret_values(self):
        secret = "my-bridge-password-123"  # pragma: allowlist secret
        text = f"failed to auth with {secret} on bridge"
        redacted = redact_sensitive_text(text, secrets=[secret])
        assert secret not in redacted
        assert "[REDACTED]" in redacted

    def test_redacts_anthropic_api_key_pattern(self):
        text = "api call failed with key sk-ant-abc123_XYZ-def in header"
        redacted = redact_sensitive_text(text)
        assert "sk-ant-abc123_XYZ-def" not in redacted
        assert "[REDACTED]" in redacted

    def test_redacts_x_api_key_header(self):
        text = 'headers: {"x-api-key": "very-secret-token"}'
        redacted = redact_sensitive_text(text)
        assert "very-secret-token" not in redacted
        assert "[REDACTED]" in redacted

    def test_redacts_bearer_authorization_header(self):
        text = "Authorization: Bearer abc.def.ghi-jkl"
        redacted = redact_sensitive_text(text)
        assert "abc.def.ghi-jkl" not in redacted
        assert "[REDACTED]" in redacted

    def test_empty_and_none_secrets_are_ignored(self):
        text = "nothing to redact"
        assert redact_sensitive_text(text, secrets=None) == text
        assert redact_sensitive_text(text, secrets=["", None]) == text  # type: ignore[list-item]

    def test_multiple_secrets_all_redacted(self):
        text = "user alice with password p@ss and token abc123"
        redacted = redact_sensitive_text(text, secrets=["p@ss", "abc123"])
        assert "p@ss" not in redacted
        assert "abc123" not in redacted
        assert redacted.count("[REDACTED]") == 2


class TestSafeExceptionText:
    def test_wraps_exception_message_with_redaction(self):
        err = RuntimeError("auth failed for user with pass=hunter2")
        result = safe_exception_text(err, secrets=["hunter2"])
        assert "hunter2" not in result
        assert "[REDACTED]" in result

    def test_preserves_non_sensitive_exception_message(self):
        err = ValueError("invalid folder: INBOX.Archive")
        assert safe_exception_text(err) == "invalid folder: INBOX.Archive"


class TestSafeProviderExceptionText:
    """Provider-aware formatter trims SDK status errors to type+status.

    The OpenAI / Anthropic / Cohere SDKs all raise exceptions whose
    stringification can echo the provider's response body — and for
    intelligence/rerank calls the request body contains retrieved
    email content. ``safe_provider_exception_text`` short-circuits any
    exception with a ``status_code`` attribute so the body never
    reaches logs or MCP callers.
    """

    def test_status_error_returns_type_and_status_only(self):
        # Synthesize an SDK-shaped status error: any exception with a
        # ``status_code`` attribute is treated as a provider error.
        # The duck-typed check covers OpenAI APIStatusError,
        # Anthropic APIStatusError, and Cohere errors uniformly without
        # importing the SDKs at test time.
        class FakeSDKStatusError(Exception):
            def __init__(self, status_code: int, body: str) -> None:
                super().__init__(body)
                self.status_code = status_code

        # The body would otherwise echo retrieved email content (subject
        # lines, addresses, body fragments quoted in a 400 validation
        # error from the provider).
        err = FakeSDKStatusError(
            429,
            "Rate limited; request body included: 'Subject: confidential ...'",
        )
        assert safe_provider_exception_text(err) == "FakeSDKStatusError: status=429"

    def test_non_status_exception_falls_through_to_redaction(self):
        # Connection / timeout / unrelated exceptions don't carry a
        # status_code, so the helper falls through to the standard
        # secret-redacting formatter and keeps diagnostic detail an
        # operator needs (timeout duration, DNS failure, etc.).
        err = TimeoutError("read timeout after 60s")
        assert safe_provider_exception_text(err) == "read timeout after 60s"

    def test_non_int_status_code_falls_through(self):
        # Defensive: an exception with a non-integer ``status_code``
        # (string, None) doesn't match the SDK contract — fall through
        # to the standard formatter rather than producing a misleading
        # ``status=<garbage>`` line.
        class WeirdError(Exception):
            status_code = "unknown"

        err = WeirdError("some message")
        assert safe_provider_exception_text(err) == "some message"

    def test_falls_through_path_still_redacts_secrets(self):
        # When the helper falls through (non-status exception), it
        # must still apply the standard redaction so a secret quoted
        # in the message doesn't leak just because the exception
        # wasn't a provider status error.
        err = RuntimeError("connect failed with key sk-ant-abc123XYZ")
        out = safe_provider_exception_text(err, secrets=[])
        assert "sk-ant-abc123XYZ" not in out
        assert "[REDACTED]" in out
