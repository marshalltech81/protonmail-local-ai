"""Tests for src.lib.security redaction helpers."""

from src.lib.security import redact_sensitive_text, safe_exception_text


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
