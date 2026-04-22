"""
Security helpers for redaction and safe error formatting.
"""

import re
from collections.abc import Iterable

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
