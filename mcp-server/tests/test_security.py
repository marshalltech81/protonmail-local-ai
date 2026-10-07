"""Tests for src.lib.security redaction helpers."""

import json
import sqlite3

import anthropic
import httpx2
import openai
import pytest
from src.lib.inference import InferenceTruncatedError
from src.lib.security import (
    ProviderResponseError,
    log_tool_call,
    redact_sensitive_text,
    safe_exception_text,
    safe_provider_exception_text,
)

_REQUEST = httpx2.Request("POST", "http://host.docker.internal:1234/v1/chat/completions")


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

    @pytest.mark.parametrize(
        "error",
        [
            TimeoutError("read timeout after 60s"),
            ConnectionError("connection refused by host.docker.internal"),
            openai.APIConnectionError(message="Connection error.", request=_REQUEST),
            openai.APITimeoutError(request=_REQUEST),
            anthropic.APIConnectionError(message="Connection error.", request=_REQUEST),
            anthropic.APITimeoutError(request=_REQUEST),
            ProviderResponseError("Inference provider returned empty content (mode=openai)"),
            InferenceTruncatedError(partial="partial answer"),
        ],
        ids=lambda e: type(e).__name__,
    )
    def test_connection_timeout_and_fixed_message_errors_keep_their_text(self, error):
        # These carry no provider response or mail content, and their
        # text is the diagnostic an operator needs.
        assert safe_provider_exception_text(error) == str(error)

    @pytest.mark.parametrize(
        "error",
        [
            RuntimeError("bad field 'Subject: confidential'"),
            ValueError("could not convert 'Subject: confidential'"),
            TypeError("unexpected 'Subject: confidential'"),
            json.JSONDecodeError("Unexpected 'Subject: confidential'", "doc", 0),
            sqlite3.OperationalError("fts5: syntax error near 'Subject: confidential'"),
        ],
        ids=lambda e: type(e).__name__,
    )
    def test_anything_else_is_reduced_to_its_type(self, error):
        # Parse, validation and conversion errors quote the values they
        # reject, and a provider's response can echo the mail sent to it.
        assert safe_provider_exception_text(error) == type(error).__name__

    def test_non_int_status_code_is_reduced_to_its_type(self):
        # An exception with a non-integer ``status_code`` doesn't match
        # the SDK contract, so it gets neither a misleading
        # ``status=<garbage>`` line nor its message.
        class WeirdError(Exception):
            status_code = "unknown"

        err = WeirdError("some message")
        assert safe_provider_exception_text(err) == "WeirdError"

    def test_kept_text_still_redacts_secrets(self):
        # A message that is kept must still be secret-redacted.
        err = ConnectionError("connect failed with key sk-ant-abc123XYZ")
        out = safe_provider_exception_text(err, secrets=[])
        assert "sk-ant-abc123XYZ" not in out
        assert "[REDACTED]" in out


class TestLogToolCall:
    """Tool-call logs carry metadata, never mailbox content: a query,
    sender, folder, or ID can quote exactly what the user wants private
    ("lawsuit against...", a salary negotiation, a medical sender)."""

    def test_logs_only_content_free_params_and_names_the_rest(self, caplog):
        import logging

        logger = logging.getLogger("test-tool-log")
        with caplog.at_level(logging.DEBUG, logger="test-tool-log"):
            log_tool_call(
                logger,
                "search_emails",
                {
                    "query": "settlement with opposing counsel",
                    "from_addr": "lawyer@example.com",
                    "folders": ["Legal"],
                    "mode": "hybrid",
                    "limit": 10,
                    "date_from": None,
                },
            )

        text = caplog.text
        assert "tool=search_emails" in text
        assert "'mode': 'hybrid'" in text
        assert "'limit': 10" in text
        assert "withheld=['folders', 'from_addr', 'query']" in text
        for secret in ("settlement", "lawyer@example.com", "Legal"):
            assert secret not in text
        assert "date_from" not in text

    def test_allowlisted_names_still_withhold_unvalidated_values(self, caplog):
        """A parameter *name* being allowlisted does not make its *value*
        safe: tool arguments come from an LLM before any validation, so a
        free-string field like ``style`` or ``date_from`` can carry
        arbitrary text. Only values that pass the field's own check are
        logged; anything else is withheld by name."""
        import logging

        logger = logging.getLogger("test-tool-log-values")
        private_text = "Confidential acquisition: Example Corp"
        with caplog.at_level(logging.INFO, logger="test-tool-log-values"):
            log_tool_call(
                logger,
                "probe",
                {
                    "mode": private_text,
                    "style": private_text,
                    "filter_type": private_text,
                    "date_from": private_text,
                    "date_to": "2024-13-45",
                    "limit": private_text,
                    "has_attachments": private_text,
                },
            )
        assert private_text not in caplog.text
        assert "2024-13-45" not in caplog.text
        for name in (
            "date_from",
            "date_to",
            "filter_type",
            "has_attachments",
            "limit",
            "mode",
            "style",
        ):
            assert f"'{name}'" in caplog.text  # listed as withheld

    def test_filter_type_values_list_threads_rejects_are_withheld(self, caplog):
        """``list_threads`` accepts ``all``, ``unread`` and ``flagged``,
        so the allowlist logs exactly those: anything the tool rejects is
        withheld by name."""
        import logging

        logger = logging.getLogger("test-tool-log-filter-type")
        with caplog.at_level(logging.INFO, logger="test-tool-log-filter-type"):
            for value in ("unread", "flagged", "Confidential-marker"):
                log_tool_call(logger, "list_threads", {"filter_type": value})
        text = caplog.text
        assert "'filter_type': 'unread'" in text
        assert "'filter_type': 'flagged'" in text
        assert "Confidential-marker" not in text
        assert text.count("withheld=['filter_type']") == 1

    def test_state_filters_log_only_booleans(self, caplog):
        import logging

        logger = logging.getLogger("test-tool-log-state")
        with caplog.at_level(logging.INFO, logger="test-tool-log-state"):
            log_tool_call(logger, "query_messages", {"seen": False, "flagged": True})
            log_tool_call(logger, "query_messages", {"seen": "Confidential-marker"})
        text = caplog.text
        assert "'seen': False" in text
        assert "'flagged': True" in text
        assert "Confidential-marker" not in text

    def test_fields_are_logged_only_when_every_name_is_a_row_field(self, caplog):
        # #990: query_messages' projection names row fields; one name
        # outside the fixed set withholds the whole list.
        import logging

        logger = logging.getLogger("test-tool-log-fields")
        with caplog.at_level(logging.INFO, logger="test-tool-log-fields"):
            log_tool_call(logger, "query_messages", {"fields": ["subject", "from"]})
            log_tool_call(logger, "query_messages", {"fields": ["subject", "Confidential-marker"]})
            log_tool_call(logger, "query_messages", {"fields": "subject"})
        text = caplog.text
        assert "'fields': ['subject', 'from']" in text
        assert "Confidential-marker" not in text
        assert text.count("withheld=['fields']") == 2

    def test_long_repeated_fields_list_is_withheld(self, caplog):
        # Review round 1: a list of valid names longer than the row has
        # fields (repeats add nothing) is withheld by name, so the line
        # stays bounded.
        import logging

        logger = logging.getLogger("test-tool-log-fields-long")
        with caplog.at_level(logging.INFO, logger="test-tool-log-fields-long"):
            log_tool_call(logger, "query_messages", {"fields": ["subject"] * 10_000})
            log_tool_call(
                logger, "query_messages", {"fields": ["subject"] * 50 + ["Confidential-marker"]}
            )
        text = caplog.text
        assert text.count("withheld=['fields']") == 2
        assert "subject" not in text
        assert "Confidential-marker" not in text
        assert len(text) < 1000

    def test_fields_allowlist_is_every_query_messages_row_field(self):
        """The logging allowlist is the row model's own field names, so a
        new row field cannot be accepted by the tool but withheld here."""
        from src.lib.security import QUERY_MESSAGE_FIELDS
        from src.tools.outputs import ListedMessage

        schema = ListedMessage.model_json_schema(by_alias=True)
        assert list(QUERY_MESSAGE_FIELDS) == list(schema["properties"])

    def test_valid_metadata_values_are_logged(self, caplog):
        import logging

        logger = logging.getLogger("test-tool-log-valid")
        with caplog.at_level(logging.INFO, logger="test-tool-log-valid"):
            log_tool_call(
                logger,
                "probe",
                {
                    "mode": "keyword",
                    "style": "action-items",
                    "filter_type": "all",
                    "date_from": "2024-01-31",
                    "date_to": "2024-02-01T12:00:00+00:00",
                    "limit": 5,
                    "include_scores": True,
                },
            )
        text = caplog.text
        for fragment in (
            "'mode': 'keyword'",
            "'style': 'action-items'",
            "'filter_type': 'all'",
            "'date_from': '2024-01-31'",
            "'date_to': '2024-02-01T12:00:00+00:00'",
            "'limit': 5",
            "'include_scores': True",
            "withheld=[]",
        ):
            assert fragment in text
