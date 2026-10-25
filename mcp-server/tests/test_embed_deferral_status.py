"""A new outage marker stays deferred despite earlier failures (#1176)."""

import pytest

from tests.test_mailbox_status_queue import _queue


@pytest.mark.parametrize("attempts", [0, 1, 3])
def test_explicit_embed_outage_is_deferred_at_every_attempt_count(empty_db, attempts):
    counts = _queue(
        empty_db,
        ("queued", attempts, "retryable", "reparse", "embed_deferred", "APIConnectionError"),
    )
    assert counts["deferred"] == 1
    assert counts["retrying"] == counts["pending"] == 0
