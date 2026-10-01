"""
Security helpers for redaction and safe error formatting.
"""

import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime
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


class ProviderResponseError(RuntimeError):
    """A provider call failed in a way we describe in fixed text.

    Raised for a response we reject (no choices, empty or non-text
    content, a wrong-sized vector). The message never quotes the
    response, the prompt or the query, so it is safe to log and return
    in full. Subclasses ``RuntimeError`` so existing ``except
    RuntimeError`` paths still catch it.
    """


def _is_connection_error(error: Exception) -> bool:
    """True for a connection or timeout failure.

    Duck-typed on the class name so this module never imports a
    provider SDK: ``openai.APIConnectionError`` and
    ``anthropic.APIConnectionError`` (and their ``APITimeoutError``
    subclasses) carry fixed SDK text, and the builtin errors carry the
    socket's.
    """
    if isinstance(error, (ConnectionError, TimeoutError)):
        return True
    return any(
        cls.__name__ == "APIConnectionError"
        and cls.__module__.split(".")[0] in ("openai", "anthropic")
        for cls in type(error).__mro__
    )


def safe_provider_exception_text(
    error: Exception,
    secrets: Iterable[str] | None = None,
) -> str:
    """Render an exception from a provider-calling tool for logs and callers.

    Mirrors the indexer's ``scrub_embed_error``. A provider's response
    can echo the request, and for the search and intelligence tools the
    request carries the query or retrieved mail, so:

    - an SDK status error (any exception with an int ``status_code``:
      ``openai.APIStatusError``, ``anthropic.APIStatusError``, Cohere's
      errors) becomes its type plus status, never the body;
    - a connection or timeout error and our own fixed-message
      ``ProviderResponseError`` keep their (secret-redacted) text;
    - anything else becomes its type name alone, since parse,
      validation, conversion and database errors quote the values they
      reject.
    """
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return f"{type(error).__name__}: status={status}"
    if isinstance(error, ProviderResponseError) or _is_connection_error(error):
        return safe_exception_text(error, secrets)
    return type(error).__name__


def _one_of(*allowed: str) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, str) and v in allowed


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _is_iso_date(v: Any) -> bool:
    if not isinstance(v, str) or len(v) > 40:
        return False
    try:
        datetime.fromisoformat(v)
    except ValueError:
        return False
    return True


# Tool parameters whose values can be logged — but only when the value
# passes that field's own check. Arguments arrive from an LLM before any
# validation, so a name alone proves nothing: ``style`` or
# ``date_from`` can carry arbitrary text. Enum sets mirror what each tool
# accepts (search modes: ``tools/search._VALID_SEARCH_MODES``; summary
# styles: ``summarize_thread``; ``filter_type``: ``Database.list_threads``,
# which accepts only ``all``). Everything else a tool receives — query
# / question text, addresses, names, folders, message and thread IDs
# (which embed sender domains), MIME types, extraction schemas — can
# quote exactly what the user wants private and is never logged.
_LOGGABLE_TOOL_PARAMS: dict[str, Callable[[Any], bool]] = {
    "mode": _one_of("hybrid", "semantic", "keyword"),
    "style": _one_of("brief", "detailed", "action-items", "timeline"),
    "filter_type": _one_of("all"),
    "limit": _is_int,
    "max_threads": _is_int,
    "offset": _is_int,
    "has_attachments": _is_bool,
    "include_scores": _is_bool,
    "extracted_only": _is_bool,
    "include_attachments_metadata": _is_bool,
    "date_from": _is_iso_date,
    "date_to": _is_iso_date,
    # ``lib/sqlite.AUTHORITY_CLASSES``.
    "authority_class": _one_of(
        "counsel", "management", "vendor", "government", "personal", "other", "unclassified"
    ),
}


def log_tool_call(logger: logging.Logger, tool: str, params: Mapping[str, Any]) -> None:
    """Log a tool invocation as metadata only.

    Logs supplied parameters whose values pass their field's validator,
    and the *names* of everything else, so an operator can see which
    filters a call used without the log recording what was searched for.
    """
    provided = {k: v for k, v in params.items() if v is not None}
    loggable = {
        k: v
        for k, v in provided.items()
        if k in _LOGGABLE_TOOL_PARAMS and _LOGGABLE_TOOL_PARAMS[k](v)
    }
    withheld = sorted(k for k in provided if k not in loggable)
    logger.info("tool=%s %s withheld=%s", tool, loggable, withheld)


def same_origin_request_hook(
    get_base_url: Callable[[], Any], provider: str, logger: logging.Logger
) -> Callable[[Any], Awaitable[None]]:
    """An httpx request hook refusing any hop off the provider's origin.

    The SDK HTTP clients follow redirects and re-send the request body
    (prompts with mail excerpts, query text) to wherever ``Location``
    points (#325, #340). The hook runs before every hop, so a redirect
    whose scheme, host or port differs from the resolved base URL is
    refused before anything is sent; same-origin redirects still work.
    ``get_base_url`` is called per request because the client that owns
    the base URL is built after the hook. Log and error text are fixed:
    neither names the redirect target.
    """

    async def hook(request: Any) -> None:
        base = get_base_url()
        if (request.url.scheme, request.url.host, request.url.port) != (
            base.scheme,
            base.host,
            base.port,
        ):
            logger.warning("%s redirected to a different origin; request not sent", provider)
            raise RuntimeError(f"{provider} redirected to a different origin")

    return hook
