"""
Security helpers for redaction and safe error formatting.
"""

import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any

_COMMON_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # No prefix group: replace the whole match outright.
    (re.compile(r"sk-ant-[A-Za-z0-9_-]+"), "[REDACTED]"),
    # Preserve the header/key prefix, redact only the value.
    (re.compile(r"(?i)(x-api-key['\":=\s]+)([^\s,'\"}]+)"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(authorization['\":=\s]+bearer\s+)([^\s,'\"}]+)"), r"\1[REDACTED]"),
)


def redact_sensitive_text(text: str, secrets: Iterable[str] | None = None) -> str:
    redacted = text

    for secret in secrets or ():
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")

    for pattern, replacement in _COMMON_SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)

    return redacted


def safe_exception_text(error: Exception, secrets: Iterable[str] | None = None) -> str:
    return redact_sensitive_text(str(error), secrets)


def safe_provider_exception_text(
    error: Exception,
    secrets: Iterable[str] | None = None,
) -> str:
    """Render a provider-SDK exception into a log/MCP-callable-safe string.

    Mirrors the indexer's ``scrub_embed_error`` posture for the mcp-server
    side. The OpenAI / Anthropic / Cohere SDKs all surface HTTP errors as
    exceptions whose stringification can echo the provider's response
    body. For tools that send retrieved email content into the request
    (intelligence prompts, reranker documents), that body can quote
    fragments of mailbox content back at us — and ``safe_exception_text``
    would propagate the full string to logs and MCP callers.

    Detection is duck-typed on the ``status_code`` attribute every
    SDK status error carries (``openai.APIStatusError``,
    ``anthropic.APIStatusError``, ``cohere.errors.*Error``). When
    matched, the formatter returns ``type + status`` only — never the
    body. Connection / timeout / unrelated exceptions fall through to
    the standard secret-redacting formatter so non-provider failures
    keep the diagnostic detail an operator needs.
    """
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return f"{type(error).__name__}: status={status}"
    return safe_exception_text(error, secrets)


# Tool parameters that never carry mailbox content. Everything else a tool
# receives — query / question text, addresses, names, folders, message and
# thread IDs (which embed sender domains), MIME types, extraction schemas —
# can quote exactly what the user wants private, so it is never logged.
_LOGGABLE_TOOL_PARAMS = frozenset(
    {
        "mode",
        "limit",
        "max_threads",
        "offset",
        "style",
        "filter_type",
        "body_format",
        "has_attachments",
        "include_scores",
        "extracted_only",
        "include_attachments_metadata",
        "date_from",
        "date_to",
    }
)


def log_tool_call(logger: logging.Logger, tool: str, params: Mapping[str, Any]) -> None:
    """Log a tool invocation as metadata only.

    Logs the content-free parameters that were supplied and the *names*
    of the withheld ones, so an operator can see which filters a call
    used without the log recording what was searched for.
    """
    provided = {k: v for k, v in params.items() if v is not None}
    loggable = {k: v for k, v in provided.items() if k in _LOGGABLE_TOOL_PARAMS}
    withheld = sorted(k for k in provided if k not in _LOGGABLE_TOOL_PARAMS)
    logger.info("tool=%s %s withheld=%s", tool, loggable, withheld)
