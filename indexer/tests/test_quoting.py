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

    def test_on_wrote_marker_drops_quoted_history(self):
        body = (
            "Thanks, that works.\n"
            "On Mon, Jan 1, 2024 at 10:00 AM Alice <alice@example.com> wrote:\n"
            "> Please confirm receipt."
        )
        assert strip_for_embedding(body) == "Thanks, that works."

    def test_on_wrote_only_matches_bounded_line(self):
        """A line that merely contains 'wrote:' somewhere should not be
        treated as a reply header. The pattern requires the full
        single-line ``On ... wrote:`` form."""
        body = "I wrote the following report on Monday as requested."
        result = strip_for_embedding(body)
        assert "wrote the following report" in result


class TestInlineReplies:
    """Reply-header lines (``On ... wrote:``) skip but do not cut.

    Inline-reply mail clients place the user's new content *between*
    quoted blocks that follow the reply header. Treating the header
    as a hard cut would silently drop those answers from the embedding
    input, which is the opposite of the module's stated philosophy.
    """

    def test_inline_answers_between_quoted_blocks_are_preserved(self):
        body = (
            "On Mon, Jan 1, 2024 at 10:00 AM Alice <alice@example.com> wrote:\n"
            "> What's your ETA?\n"
            "Probably Friday.\n"
            "> Will you be in office?\n"
            "No, remote."
        )
        result = strip_for_embedding(body)
        # User's inline answers must survive the strip.
        assert "Probably Friday." in result
        assert "No, remote." in result
        # The reply header and the quoted questions must not.
        assert "wrote:" not in result
        assert "ETA" not in result
        assert "in office" not in result

    def test_top_post_with_inline_annotations_keeps_both(self):
        """A reply that top-posts a summary AND inline-annotates the
        quoted thread below it must keep both portions."""
        body = (
            "Short version: yes, ship it.\n"
            "\n"
            "On Mon, Jan 1, 2024 at 10:00 AM Alice <alice@example.com> wrote:\n"
            "> Are you OK with the timeline?\n"
            "Yes, fine.\n"
            "> Any concerns about the budget?\n"
            "None."
        )
        result = strip_for_embedding(body)
        assert "Short version: yes, ship it." in result
        assert "Yes, fine." in result
        assert "None." in result
        assert "timeline" not in result
        assert "budget" not in result


class TestNonEnglishReplyHeaders:
    """Reply-header lines in major non-English variants are skipped,
    matching the English ``On ... wrote:`` behavior.

    The threader already strips German / Swedish / French / Chinese
    subject prefixes (see ``TestNormalizeSubject``); leaving the
    embedding-side stripper English-only meant the indexer pipeline
    dropped quoted history in English while leaking the same noise in
    every other language. Each line below is a synthetic placeholder
    in the Gmail/Apple Mail single-line reply-header shape.
    """

    def test_german_schrieb_header_drops_quoted_history(self):
        body = (
            "Danke, passt so.\n"
            "Am Mo., 1. Jan. 2024 um 10:00 schrieb Alice <alice@example.com>:\n"
            "> Bitte um Bestätigung."
        )
        assert strip_for_embedding(body) == "Danke, passt so."

    def test_french_a_ecrit_header_drops_quoted_history(self):
        # French convention is "a écrit :" with a non-breaking space
        # before the colon. Accept the regular-space form too.
        body = (
            "Merci, c'est bon.\n"
            "Le lun. 1 janv. 2024 à 10:00, Alice <alice@example.com> a écrit :\n"
            "> Pouvez-vous confirmer ?"
        )
        assert strip_for_embedding(body) == "Merci, c'est bon."

    def test_spanish_escribio_header_drops_quoted_history(self):
        body = (
            "Gracias, perfecto.\n"
            "El lun., 1 ene 2024 a las 10:00, Alice <alice@example.com> escribió:\n"
            "> Por favor confirma."
        )
        assert strip_for_embedding(body) == "Gracias, perfecto."

    def test_italian_ha_scritto_header_drops_quoted_history(self):
        body = (
            "Va bene, grazie.\n"
            "Il giorno lun 1 gen 2024 alle ore 10:00 Alice <alice@example.com> ha scritto:\n"
            "> Conferma per favore."
        )
        assert strip_for_embedding(body) == "Va bene, grazie."

    def test_dutch_schreef_header_drops_quoted_history(self):
        body = (
            "Akkoord, bedankt.\n"
            "Op ma 1 jan. 2024 om 10:00 schreef Alice <alice@example.com>:\n"
            "> Bevestig graag."
        )
        assert strip_for_embedding(body) == "Akkoord, bedankt."

    def test_german_inline_answers_are_preserved(self):
        # Non-English reply headers must also be a SKIP, not a hard cut,
        # so inline annotations between quoted blocks survive.
        body = (
            "Am Mo., 1. Jan. 2024 um 10:00 schrieb Alice <alice@example.com>:\n"
            "> Wann ist die Lieferung?\n"
            "Wahrscheinlich am Freitag.\n"
            "> Bist du im Büro?\n"
            "Nein, remote."
        )
        result = strip_for_embedding(body)
        assert "Wahrscheinlich am Freitag." in result
        assert "Nein, remote." in result
        assert "schrieb" not in result
        assert "Lieferung" not in result
        assert "Büro" not in result

    def test_prose_containing_schrieb_is_not_falsely_cut(self):
        # A line that merely contains the verb in prose, not as the
        # closing keyword of a reply header, must survive.
        body = "Ich schrieb gestern einen langen Bericht über das Thema."
        result = strip_for_embedding(body)
        assert "schrieb gestern einen langen Bericht" in result


class TestWrappedReplyHeaders:
    """Gmail wraps the attribution line onto two lines when the
    address is long, pushing the verb (and colon) onto a line of its
    own. The single-line pattern can't catch the wrapped form, so a
    pre-pass joins the two lines before the main loop runs.
    """

    def test_english_wrapped_on_wrote_is_dropped(self):
        body = (
            "Thanks for the heads up.\n"
            "On Mon, Jan 1, 2024 at 10:00 AM Alice <alice@verylongdomain.example.com>\n"
            "wrote:\n"
            "> Please confirm."
        )
        result = strip_for_embedding(body)
        assert "Thanks for the heads up." in result
        assert "wrote:" not in result
        assert "verylongdomain" not in result
        assert "Please confirm" not in result

    def test_german_wrapped_schrieb_is_dropped(self):
        body = (
            "Vielen Dank.\n"
            "Am Mo., 1. Jan. 2024 um 10:00 Uhr Alice <alice@verylongdomain.example.com>\n"
            "schrieb:\n"
            "> Bitte um Bestätigung."
        )
        result = strip_for_embedding(body)
        assert "Vielen Dank." in result
        assert "schrieb:" not in result
        assert "verylongdomain" not in result

    def test_wrapped_pattern_requires_verb_on_continuation(self):
        # If the second line is NOT the verb-only continuation, the
        # pre-pass must NOT collapse — otherwise an unrelated body line
        # starting with "On" could pull its successor in.
        body = "On Monday I sent the report,\nbut did not hear back.\nToday I followed up."
        result = strip_for_embedding(body)
        assert "On Monday I sent the report," in result
        assert "but did not hear back." in result
        assert "Today I followed up." in result


class TestOutlookBlockCut:
    """Newer Outlook omits the ``-----Original Message-----`` dashed
    delimiter and emits a bare ``From:/Sent:/To:/Subject:`` block at
    the start of the quoted history. Detect that block as a hard cut.
    """

    def test_outlook_from_sent_block_cuts(self):
        body = (
            "Approved, thanks.\n"
            "\n"
            "From: Alice <alice@example.com>\n"
            "Sent: Monday, January 1, 2024 10:00 AM\n"
            "To: Bob <bob@example.com>\n"
            "Subject: Approval needed\n"
            "\n"
            "Please approve the attached invoice."
        )
        assert strip_for_embedding(body) == "Approved, thanks."

    def test_outlook_from_date_block_cuts(self):
        # Some Outlook configurations emit ``Date:`` rather than
        # ``Sent:`` for the timestamp line.
        body = (
            "Looks good.\n"
            "\n"
            "From: Alice <alice@example.com>\n"
            "Date: Monday, January 1, 2024 10:00 AM\n"
            "Subject: Status"
        )
        assert strip_for_embedding(body) == "Looks good."

    def test_outlook_block_with_blank_line_still_cuts(self):
        # A blank line between ``From:`` and ``Sent:`` is common when
        # the client double-spaces metadata. The detector must still
        # trip.
        body = (
            "Reply text.\n"
            "\n"
            "From: Alice <alice@example.com>\n"
            "\n"
            "Sent: Monday, January 1, 2024 10:00 AM\n"
            "Subject: Status"
        )
        assert strip_for_embedding(body) == "Reply text."

    def test_prose_mentioning_from_is_not_falsely_cut(self):
        # A body that mentions "From: someone" in prose, without a
        # following ``Sent:`` or ``Date:`` line, must survive.
        body = "The note read: From: Anonymous.\nThat was all it said.\nStrange."
        result = strip_for_embedding(body)
        assert "From: Anonymous" in result
        assert "That was all it said." in result
        assert "Strange." in result


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
