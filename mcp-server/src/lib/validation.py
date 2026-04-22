"""
Input validation helpers for MCP tool boundaries.

MCP tool arguments can come from an LLM, which may pass the wrong type
(``limit="ten"``) or a wildly out-of-range value. The helpers here coerce
caller-supplied numeric inputs to a clamped integer so individual tools
do not have to re-implement defensive ``int(...)`` + ``max/min`` patterns
that would otherwise raise a bare ``ValueError`` before the tool's
try/except and surface through the MCP protocol as a hard tool failure.
"""


def clamp_int(value, default: int, minimum: int, maximum: int) -> int:
    """Return ``value`` coerced to an int and clamped to [minimum, maximum].

    Falls back to ``default`` when ``value`` is None or cannot be parsed
    as an integer (e.g. an LLM passed the string ``"ten"``). The result
    is always clamped to ``[minimum, maximum]``, so a caller-supplied
    ``default`` outside that range is brought back in-bounds as well.
    """
    try:
        parsed = int(value)
    except TypeError, ValueError:
        parsed = default
    if parsed < minimum:
        return minimum
    if parsed > maximum:
        return maximum
    return parsed
