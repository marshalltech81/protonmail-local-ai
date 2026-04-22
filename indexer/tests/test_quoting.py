"""
Tests for src/quoting.py.

Covers quoted-reply stripping, signature cut-offs, forward markers, and
the empty-result fallback used to keep the embedding input non-empty.
"""

from src.quoting import strip_for_embedding


class TestQuotedLineStripping:
    def test_drops_plain_quoted_line(self):
        body = "New reply text.\n> quoted reply from earlier"
        assert strip_for_embedding(body) == "New reply text."

    def test_drops_nested_quoted_lines(self):
        body = "New reply.\n> level one\n>> level two\n>>> level three"
        assert strip_for_embedding(body) == "New reply."

    def test_drops_quoted_lines_with_leading_whitespace(self):
        """Some clients indent quoted blocks by a space or tab."""
        body = "New reply.\n  > indented quote\n\t> tab-indented quote"
        assert strip_for_embedding(body) == "New reply."

    def test_preserves_non_quoted_content(self):
        body = "Line one.\nLine two.\nLine three."
        assert strip_for_embedding(body) == "Line one.\nLine two.\nLine three."


class TestSignatureCutoff:
    def test_rfc3676_signature_delimiter_cuts_the_rest(self):
        """``-- \\n`` (two dashes + space + newline) is the RFC 3676
        signature delimiter. Everything below is a signature and should
        not contribute to the embedding."""
        body = "New reply.\n-- \nJane Doe\nSenior Engineer\nCompany Inc."
        assert strip_for_embedding(body) == "New reply."

    def test_delimiter_without_trailing_space_is_not_cut(self):
        """The RFC 3676 marker requires the trailing space. A literal
        ``--`` line alone is used for horizontal rules and must not
        trigger a signature cut."""
        body = "New reply.\n--\nLooks like a separator but is not a sig."
        result = strip_for_embedding(body)
        assert "separator" in result


class TestForwardAndReplyMarkers:
    def test_outlook_original_message_marker_cuts(self):
        body = (
            "New reply text.\n"
            "-----Original Message-----\n"
            "From: alice@example.com\n"
            "Sent: Monday\n"
            "Subject: Old thread"
        )
        assert strip_for_embedding(body) == "New reply text."

    def test_gmail_forwarded_marker_cuts(self):
        body = (
            "Check this out.\n"
            "---------- Forwarded message ---------\n"
            "From: alice@example.com\n"
            "Subject: Original"
        )
        assert strip_for_embedding(body) == "Check this out."

    def test_apple_begin_forwarded_marker_cuts(self):
        body = "Forwarding for your records.\nBegin forwarded message:\nFrom: alice@example.com"
        assert strip_for_embedding(body) == "Forwarding for your records."

    def test_on_wrote_marker_cuts(self):
        body = (
            "Thanks, that works.\n"
            "On Mon, Jan 1, 2024 at 10:00 AM Alice <alice@example.com> wrote:\n"
            "> Please confirm receipt."
        )
        assert strip_for_embedding(body) == "Thanks, that works."

    def test_on_wrote_only_matches_bounded_line(self):
        """A line that merely contains 'wrote:' somewhere should not cut.
        The pattern requires the full single-line ``On ... wrote:`` form."""
        body = "I wrote the following report on Monday as requested."
        result = strip_for_embedding(body)
        assert "wrote the following report" in result


class TestEmptyFallback:
    def test_empty_input_returns_empty(self):
        assert strip_for_embedding("") == ""

    def test_reply_that_is_only_quoted_returns_original(self):
        """If the stripper would produce an empty string — e.g. a reply
        that is literally just the Gmail reply header and a quoted
        thread — return the original body so the embedding has some
        content to ground on rather than an empty vector seed."""
        body = (
            "On Mon, Jan 1, 2024 at 10:00 AM Alice <alice@example.com> wrote:\n"
            "> Original question text."
        )
        # Stripped form is empty (the marker cuts before the quoted
        # line would have been dropped anyway). The fallback keeps the
        # original body intact.
        assert strip_for_embedding(body) == body

    def test_reply_of_only_quoted_lines_returns_original(self):
        body = "> line one\n> line two\n> line three"
        assert strip_for_embedding(body) == body


class TestRealisticCombinations:
    def test_gmail_style_reply_with_signature(self):
        body = (
            "That sounds good. Let's meet on Wednesday.\n"
            "\n"
            "Thanks,\n"
            "Bob\n"
            "-- \n"
            "Bob Example | Senior PM | +1 555 0100\n"
            "\n"
            "On Mon, Jan 1, 2024 at 10:00 AM Alice <alice@example.com> wrote:\n"
            "> Can we schedule the review?\n"
            ">\n"
            "> Alice"
        )
        result = strip_for_embedding(body)
        assert "Wednesday" in result
        assert "Thanks," in result
        assert "Bob" in result
        # Signature gone
        assert "Senior PM" not in result
        # Quoted thread gone
        assert "schedule the review" not in result

    def test_outlook_style_reply(self):
        body = (
            "Approved.\n"
            "\n"
            "-----Original Message-----\n"
            "From: alice@example.com\n"
            "Subject: Approval needed\n"
            "\n"
            "Please approve the attached invoice."
        )
        assert strip_for_embedding(body) == "Approved."
