"""#1239: snippets use the same body segmentation as embedding."""

import pytest

from tests.conftest import make_message, make_thread


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "On Mon, Jan 1, 2024 at 10:00 AM Alice <alice@example.test> wrote:\n> old text\n\nThe new answer.",
            "The new answer.",
        ),
        ("\n\nThe new answer.\n-- \nSynthetic signature", "The new answer."),
        ("-- \nSynthetic signature", "--  Synthetic signature"),
        (
            "---------- Forwarded message ---------\nFrom: example@example.test\nForwarded text",
            "---------- Forwarded message --------- From: example@example.test Forwarded text",
        ),
        ("Substantive body\nsecond line", "Substantive body second line"),
        ("", ""),
    ],
)
def test_snippet_uses_body_or_raw_fallback(body, expected):
    thread = make_thread(messages=[make_message(body_text=body)])
    assert thread.snippet() == expected


def test_long_attribution_followed_by_body_is_capped_after_segmentation():
    body = (
        "On Mon, Jan 1, 2024 at 10:00 AM Alice <alice@example.test> wrote:\n> "
        + "old " * 100
        + "\n"
        + "new " * 100
    )
    assert make_thread(messages=[make_message(body_text=body)]).snippet() == ("new " * 100)[:200]
