"""
Tests for src/threader.py.

Covers: Thread.text_for_embedding, Thread.snippet, Threader.assign_thread
(new thread, In-Reply-To match, References match, subject fallback),
and participant deduplication.
"""

from datetime import UTC, datetime

from src.threader import Thread, Threader, _normalize_subject

from tests.conftest import make_message, make_thread

# ---------------------------------------------------------------------------
# Thread.text_for_embedding
# ---------------------------------------------------------------------------


class TestTextForEmbedding:
    def test_includes_subject_and_participants(self):
        thread = make_thread(subject="project update")
        text = thread.text_for_embedding()
        assert "Subject: project update" in text
        assert "Participants:" in text

    def test_includes_message_body(self):
        msg = make_message(body_text="This is the body content.")
        thread = make_thread(messages=[msg])
        assert "This is the body content." in thread.text_for_embedding()

    def test_per_message_body_truncated_at_500_chars(self):
        long_body = "x" * 1000
        msg = make_message(body_text=long_body)
        thread = make_thread(messages=[msg])
        text = thread.text_for_embedding()
        # The long body should appear but be capped at 500 chars
        assert "x" * 500 in text
        assert "x" * 501 not in text

    def test_thread_output_capped_at_4000_chars(self):
        msgs = [
            make_message(
                message_id=f"msg{i}@example.com",
                body_text="y" * 500,
                filepath=f"/maildir/INBOX/cur/msg{i}",
                date=datetime(2024, 1, i + 1, tzinfo=UTC),
            )
            for i in range(20)
        ]
        thread = make_thread(messages=msgs)
        assert len(thread.text_for_embedding()) <= 4000

    def test_multiple_messages_all_represented(self):
        msg1 = make_message(
            message_id="msg1@example.com",
            body_text="First message content.",
            filepath="/maildir/INBOX/cur/msg1",
            date=datetime(2024, 1, 1, tzinfo=UTC),
        )
        msg2 = make_message(
            message_id="msg2@example.com",
            body_text="Second message content.",
            filepath="/maildir/INBOX/cur/msg2",
            date=datetime(2024, 1, 2, tzinfo=UTC),
        )
        thread = make_thread(messages=[msg1, msg2])
        text = thread.text_for_embedding()
        assert "First message content." in text
        assert "Second message content." in text

    def test_empty_messages_list_returns_header_only(self):
        thread = Thread(
            thread_id="t1",
            subject="no messages",
            participants=["alice@example.com"],
            messages=[],
            folder="INBOX",
            date_first=datetime(2024, 1, 1, tzinfo=UTC),
            date_last=datetime(2024, 1, 1, tzinfo=UTC),
        )
        text = thread.text_for_embedding()
        assert "Subject: no messages" in text


# ---------------------------------------------------------------------------
# Thread.snippet
# ---------------------------------------------------------------------------


class TestSnippet:
    def test_returns_last_message_body_preview(self):
        msg1 = make_message(
            message_id="msg1@example.com",
            body_text="First message.",
            filepath="/maildir/INBOX/cur/msg1",
            date=datetime(2024, 1, 1, tzinfo=UTC),
        )
        msg2 = make_message(
            message_id="msg2@example.com",
            body_text="Last message body here.",
            filepath="/maildir/INBOX/cur/msg2",
            date=datetime(2024, 1, 2, tzinfo=UTC),
        )
        thread = make_thread(messages=[msg1, msg2])
        assert "Last message body here." in thread.snippet()

    def test_snippet_max_200_chars(self):
        msg = make_message(body_text="z" * 500)
        thread = make_thread(messages=[msg])
        assert len(thread.snippet()) <= 200

    def test_newlines_replaced_with_spaces(self):
        msg = make_message(body_text="line one\nline two")
        thread = make_thread(messages=[msg])
        assert "\n" not in thread.snippet()

    def test_empty_messages_returns_empty_string(self):
        thread = Thread(
            thread_id="t1",
            subject="empty",
            participants=[],
            messages=[],
            folder="INBOX",
            date_first=datetime(2024, 1, 1, tzinfo=UTC),
            date_last=datetime(2024, 1, 1, tzinfo=UTC),
        )
        assert thread.snippet() == ""


# ---------------------------------------------------------------------------
# Threader.assign_thread
# ---------------------------------------------------------------------------


class TestAssignThread:
    def test_creates_new_thread_for_first_message(self, threader):
        msg = make_message()
        thread = threader.assign_thread(msg)
        assert thread.thread_id == msg.message_id
        assert msg in thread.messages
        assert thread.subject == _normalize_subject(msg.subject)

    def test_joins_existing_thread_via_in_reply_to(self, db, threader):
        # Index the first message
        original = make_message(
            message_id="orig@example.com",
            subject="Project update",
        )
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, [0.0] * 768)

        # Reply referencing the original via In-Reply-To
        reply = make_message(
            message_id="reply@example.com",
            subject="Re: Project update",
            in_reply_to="orig@example.com",
            filepath="/maildir/INBOX/cur/reply",
            date=datetime(2024, 1, 2, tzinfo=UTC),
        )
        t2 = threader.assign_thread(reply)

        # Should be the same thread
        assert t2.thread_id == "orig@example.com"
        assert reply in t2.messages

    def test_joins_existing_thread_via_references(self, db, threader):
        original = make_message(message_id="root@example.com")
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, [0.0] * 768)

        # Second message references the original in its References header
        msg2 = make_message(
            message_id="second@example.com",
            subject="Re: Hello world",
            references=["root@example.com"],
            filepath="/maildir/INBOX/cur/second",
            date=datetime(2024, 1, 2, tzinfo=UTC),
        )
        t2 = threader.assign_thread(msg2)
        assert t2.thread_id == "root@example.com"

    def test_references_matched_most_recent_first(self, db, threader):
        """Most recent reference (last in list) is checked first."""
        msg_a = make_message(message_id="a@example.com")
        t_a = threader.assign_thread(msg_a)
        db.upsert_thread(t_a, [0.0] * 768)

        msg_b = make_message(
            message_id="b@example.com",
            subject="Re: Hello world",
            references=["unknown@example.com", "a@example.com"],
            filepath="/maildir/INBOX/cur/b",
            date=datetime(2024, 1, 2, tzinfo=UTC),
        )
        t_b = threader.assign_thread(msg_b)
        # Should join thread rooted at a@example.com via the last reference
        assert t_b.thread_id == "a@example.com"

    def test_falls_back_to_subject_matching(self, db, threader):
        original = make_message(
            message_id="subj_orig@example.com",
            subject="Budget discussion",
        )
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, [0.0] * 768)

        # No In-Reply-To or References — subject match should group them
        followup = make_message(
            message_id="subj_follow@example.com",
            subject="Re: Budget discussion",
            filepath="/maildir/INBOX/cur/followup",
            date=datetime(2024, 1, 2, tzinfo=UTC),
        )
        t2 = threader.assign_thread(followup)
        assert t2.thread_id == "subj_orig@example.com"

    def test_subject_fallback_does_not_cross_folders(self, db, threader):
        """Subject matching is scoped to the same folder."""
        inbox_msg = make_message(
            message_id="inbox_msg@example.com",
            subject="Hello world",
            folder="INBOX",
        )
        t1 = threader.assign_thread(inbox_msg)
        db.upsert_thread(t1, [0.0] * 768)

        sent_msg = make_message(
            message_id="sent_msg@example.com",
            subject="Re: Hello world",
            folder="Sent",
            filepath="/maildir/Sent/cur/sent_msg",
            date=datetime(2024, 1, 2, tzinfo=UTC),
        )
        t2 = threader.assign_thread(sent_msg)
        # Different folder — must not merge even though subject matches
        assert t2.thread_id == "sent_msg@example.com"

    def test_date_last_updated_when_new_message_joins(self, db, threader):
        original = make_message(
            message_id="date_orig@example.com",
            date=datetime(2024, 1, 1, tzinfo=UTC),
        )
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, [0.0] * 768)

        later = make_message(
            message_id="date_later@example.com",
            subject="Re: Hello world",
            in_reply_to="date_orig@example.com",
            filepath="/maildir/INBOX/cur/later",
            date=datetime(2024, 6, 1, tzinfo=UTC),
        )
        t2 = threader.assign_thread(later)
        assert t2.date_last == datetime(2024, 6, 1, tzinfo=UTC)

    def test_existing_participants_preserved_when_new_message_joins(self, db, threader):
        """Regression: appending a reply must not drop previously-seen
        participants from the thread. Prior behavior overwrote participants
        with only the new message's addresses."""
        original = make_message(
            message_id="part_orig@example.com",
            from_addr="alice@example.com",
            to_addrs=["bob@example.com"],
        )
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, [0.0] * 768)

        # Reply from a third party introduces a new participant; existing
        # participants must still be present after threader runs.
        reply = make_message(
            message_id="part_reply@example.com",
            subject="Re: Hello world",
            from_addr="carol@example.com",
            to_addrs=["alice@example.com"],
            in_reply_to="part_orig@example.com",
            filepath="/maildir/INBOX/cur/part_reply",
            date=datetime(2024, 1, 2, tzinfo=UTC),
        )
        t2 = threader.assign_thread(reply)

        assert "alice@example.com" in t2.participants
        assert "bob@example.com" in t2.participants
        assert "carol@example.com" in t2.participants

    def test_out_of_order_older_message_widens_date_first(self, db, threader):
        """An older message arriving after newer ones must lower date_first
        and leave date_last unchanged."""
        newer = make_message(
            message_id="ooo_newer@example.com",
            date=datetime(2024, 6, 1, tzinfo=UTC),
        )
        t1 = threader.assign_thread(newer)
        db.upsert_thread(t1, [0.0] * 768)

        older = make_message(
            message_id="ooo_older@example.com",
            subject="Re: Hello world",
            in_reply_to="ooo_newer@example.com",
            filepath="/maildir/INBOX/cur/older",
            date=datetime(2024, 1, 1, tzinfo=UTC),
        )
        t2 = threader.assign_thread(older)

        assert t2.date_first == datetime(2024, 1, 1, tzinfo=UTC)
        assert t2.date_last == datetime(2024, 6, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Threader._participants
# ---------------------------------------------------------------------------


class TestParticipants:
    def test_deduplicates_across_messages(self):
        msg1 = make_message(
            from_addr="alice@example.com",
            to_addrs=["bob@example.com"],
        )
        msg2 = make_message(
            message_id="msg2@example.com",
            from_addr="bob@example.com",
            to_addrs=["alice@example.com"],
        )
        result = Threader._participants([msg1, msg2])
        assert result.count("alice@example.com") == 1
        assert result.count("bob@example.com") == 1

    def test_includes_cc_addresses(self):
        msg = make_message(
            from_addr="alice@example.com",
            to_addrs=["bob@example.com"],
            cc_addrs=["carol@example.com"],
        )
        result = Threader._participants([msg])
        assert "carol@example.com" in result

    def test_preserves_insertion_order(self):
        msg = make_message(
            from_addr="alice@example.com",
            to_addrs=["bob@example.com", "carol@example.com"],
        )
        result = Threader._participants([msg])
        assert result[0] == "alice@example.com"
