"""
Security helpers for redaction and safe error formatting.
"""

import re
from collections.abc import Iterable

_COMMON_SECRET_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),
    re.compile(r"(?i)(x-api-key['\":=\s]+)([^\s,'\"}]+)"),
    re.compile(r"(?i)(authorization['\":=\s]+bearer\s+)([^\s,'\"}]+)"),
)


def redact_sensitive_text(text: str, secrets: Iterable[str] | None = None) -> str:
    redacted = text

    for secret in secrets or ():
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")

    for pattern in _COMMON_SECRET_PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED]", redacted)

    return redacted


def safe_exception_text(error: Exception, secrets: Iterable[str] | None = None) -> str:
    return redact_sensitive_text(str(error), secrets)
