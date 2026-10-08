"""Source identity shared by startup logs, MCP metadata and status."""

import os
import re

# Same shape as make build's commit label, including the -dirty suffix.
# Reject values that could add arbitrary text or a line to diagnostics.
_GIT_COMMIT_PATTERN = re.compile(r"[0-9A-Za-z._-]{1,64}")


def git_commit() -> str:
    """The image's baked-in GIT_COMMIT, or unknown when unavailable."""
    value = os.environ.get("GIT_COMMIT", "").strip()
    return value if _GIT_COMMIT_PATTERN.fullmatch(value) else "unknown"
