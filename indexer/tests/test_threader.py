"""
Tests for src/threader.py.

Covers: Thread.text_for_embedding, Thread.snippet, Threader.assign_thread
(new thread, In-Reply-To match, References match, subject fallback),
and participant deduplication.
"""

import re
import time
from datetime import UTC, datetime

from src import threader
from src.database import EMBEDDING_DIM  # noqa: F401  -- via reuse
from src.threader import Thread, Threader, _normalize_subject, canonical_addr

from tests.conftest import make_message, make_thread

# ---------------------------------------------------------------------------
# Thread.text_for_embedding
# ---------------------------------------------------------------------------


class TestFtsSubjectText:
    """#303: the ``threads_fts`` subject column carries the thread
    subject plus each distinct changed reply subject, bounded."""

    def test_adds_only_distinct_changed_subjects(self):
        from src.threader import fts_subject_text

        text = fts_subject_text(
            "budget review",
            [
                "Budget review",
                "Re: FWD: budget  review",
                "Re: Budget review ZX731",
                "RE: budget review zx731",
                "",
            ],
        )
        assert text == "budget review\nbudget review zx731"

    def test_unchanged_thread_keeps_its_subject_verbatim(self):
        from src.threader import fts_subject_text

        assert fts_subject_text("budget review", ["Re: budget review"]) == "budget review"

    def test_bounded_by_count_and_chars(self):
        from src.threader import (
            FTS_REPLY_SUBJECTS_MAX,
            FTS_REPLY_SUBJECTS_MAX_CHARS,
            fts_subject_text,
        )

        many = [f"Re: topic {i}" for i in range(FTS_REPLY_SUBJECTS_MAX * 3)]
        lines = fts_subject_text("topic", many).split("\n")
        assert len(lines) == 1 + FTS_REPLY_SUBJECTS_MAX
        long = ["a" * (FTS_REPLY_SUBJECTS_MAX_CHARS // 2 + 1) + str(i) for i in range(3)]
        text = fts_subject_text("topic", long)
        assert len(text) - len("topic") <= FTS_REPLY_SUBJECTS_MAX_CHARS + 2


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

    def test_per_message_body_truncated_at_shared_char_cap(self):
        # ``Thread.text_for_embedding`` caps each message's body
        # contribution at ``PER_MESSAGE_BODY_CAP_CHARS`` so one outlier
        # message cannot dominate the thread's FTS body before the
        # thread-wide token cap fires. ``Database._compute_body`` (the
        # update path) shares this constant — see
        # ``test_per_message_body_cap_matches_across_insert_and_update_paths``
        # in test_database.py — so the two paths agree on what each
        # message contributes regardless of arrival ordering.
        from src.threader import PER_MESSAGE_BODY_CAP_CHARS

        long_body = "x" * (PER_MESSAGE_BODY_CAP_CHARS + 1000)
        msg = make_message(body_text=long_body)
        thread = make_thread(messages=[msg])
        text = thread.text_for_embedding()
        assert "x" * PER_MESSAGE_BODY_CAP_CHARS in text
        assert "x" * (PER_MESSAGE_BODY_CAP_CHARS + 1) not in text

    def test_thread_output_capped_at_shared_max_tokens(self):
        """Both the fresh-insert embedding text and the accumulated body
        written on update must respect the same cap so updated threads
        don't silently retain more context than brand-new ones. The
        cap is now token-based (``THREAD_BODY_TEXT_MAX_TOKENS``) — a
        char-based cap under-counted CJK / dense content by 4-6× and
        forced unnecessarily aggressive truncation in ASCII-heavy
        threads."""
        from datetime import timedelta

        from src.chunker import estimate_tokens
        from src.threader import THREAD_BODY_TEXT_MAX_TOKENS

        base_date = datetime(2024, 1, 1, tzinfo=UTC)
        msgs = [
            make_message(
                message_id=f"msg{i}@example.com",
                body_text="y" * 500,
                filepath=f"/maildir/INBOX/cur/msg{i}",
                date=base_date + timedelta(days=i),
            )
            for i in range(40)
        ]
        thread = make_thread(messages=msgs)
        assert estimate_tokens(thread.text_for_embedding()) <= THREAD_BODY_TEXT_MAX_TOKENS

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
        db.upsert_thread(t1, [0.0] * EMBEDDING_DIM)

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

    def test_already_threaded_message_keeps_its_thread(self, db, threader):
        """#204: reprocessing a message must not re-resolve its thread
        from headers — its chunks stay under the thread it has."""
        reply = make_message(
            message_id="reply@example.com",
            subject="Budget reply",
            in_reply_to="parent@example.com",
            filepath="/maildir/INBOX/cur/reply",
        )
        db.upsert_thread(threader.assign_thread(reply), [0.0] * EMBEDDING_DIM)
        parent = make_message(
            message_id="parent@example.com",
            subject="Quarterly plan",
            filepath="/maildir/INBOX/cur/parent",
        )
        db.upsert_thread(threader.assign_thread(parent), [0.0] * EMBEDDING_DIM)

        again = threader.assign_thread(reply)

        assert again.thread_id == "reply@example.com"

    def test_joins_existing_thread_via_references(self, db, threader):
        original = make_message(message_id="root@example.com")
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, [0.0] * EMBEDDING_DIM)

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
        db.upsert_thread(t_a, [0.0] * EMBEDDING_DIM)

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
        db.upsert_thread(t1, [0.0] * EMBEDDING_DIM)

        # No In-Reply-To or References — subject match should group them
        followup = make_message(
            message_id="subj_follow@example.com",
            subject="Re: Budget discussion",
            filepath="/maildir/INBOX/cur/followup",
            date=datetime(2024, 1, 2, tzinfo=UTC),
        )
        t2 = threader.assign_thread(followup)
        assert t2.thread_id == "subj_orig@example.com"

    def test_subject_fallback_rejected_when_no_participant_overlap(self, db, threader):
        """Regression: subject-only fallback merged any two messages with
        the same normalized subject in the same folder — "Re: Follow up"
        from one sender/recipient pair would merge into an unrelated
        thread with the same subject. Require participant overlap."""
        original = make_message(
            message_id="sub_no_overlap_orig@example.com",
            subject="Follow up",
            from_addr="alice@example.com",
            to_addrs=["bob@example.com"],
        )
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, [0.0] * EMBEDDING_DIM)

        # Same subject, same folder, totally different participants.
        unrelated = make_message(
            message_id="sub_no_overlap_unrelated@example.com",
            subject="Re: Follow up",
            from_addr="carol@example.com",
            to_addrs=["dave@example.com"],
            filepath="/maildir/INBOX/cur/unrelated",
            date=datetime(2024, 1, 2, tzinfo=UTC),
        )
        t2 = threader.assign_thread(unrelated)
        # Must NOT merge — no shared participant.
        assert t2.thread_id == "sub_no_overlap_unrelated@example.com"

    def test_subject_fallback_ignores_a_shared_recipient_alone(self, db, threader):
        """Regression (#205): the mailbox owner is a recipient of nearly
        every message, so sharing only them is no evidence of one
        conversation. Two vendors' "Invoice" mails merged into one
        thread, mixing their content in retrieval and summaries."""
        first = make_message(
            message_id="inv_a@example.com",
            subject="Invoice",
            from_addr="billing@vendor-a.example",
            to_addrs=["owner@example.com"],
        )
        db.upsert_thread(threader.assign_thread(first), [0.0] * EMBEDDING_DIM)
        second = make_message(
            message_id="inv_b@example.com",
            subject="Invoice",
            from_addr="billing@vendor-b.example",
            to_addrs=["owner@example.com"],
            filepath="/maildir/INBOX/cur/inv_b",
            date=datetime(2024, 1, 15, tzinfo=UTC),
        )

        assert threader.assign_thread(second).thread_id == "inv_b@example.com"

    def test_subject_fallback_counts_every_from_author(self, db, threader):
        """Review round 1: a multi-author From is parsed into
        ``from_addrs``, but only the first author reached the thread's
        participants and the fallback check, so a co-author's headerless
        follow-up was split off."""
        first = make_message(
            message_id="coauth@example.com",
            subject="Draft proposal",
            from_addr="alice@example.com",
            to_addrs=["owner@example.com"],
        )
        first.from_addrs = ["alice@example.com", "bob@example.com"]
        thread = threader.assign_thread(first)
        assert "bob@example.com" in thread.participants
        db.upsert_thread(thread, [0.0] * EMBEDDING_DIM)
        followup = make_message(
            message_id="coauth_follow@example.com",
            subject="Re: Draft proposal",
            from_addr="bob@example.com",
            to_addrs=["owner@example.com"],
            filepath="/maildir/INBOX/cur/coauth_follow",
            date=datetime(2024, 1, 2, tzinfo=UTC),
        )

        assert threader.assign_thread(followup).thread_id == "coauth@example.com"

    def test_subject_fallback_ignores_a_shared_sender_alone(self, db, threader):
        """The same owner writing "Meeting" to two different people is
        two conversations."""
        first = make_message(
            message_id="meet_x@example.com",
            subject="Meeting",
            from_addr="owner@example.com",
            to_addrs=["x@example.com"],
        )
        db.upsert_thread(threader.assign_thread(first), [0.0] * EMBEDDING_DIM)
        second = make_message(
            message_id="meet_y@example.com",
            subject="Meeting",
            from_addr="owner@example.com",
            to_addrs=["y@example.com"],
            filepath="/maildir/INBOX/cur/meet_y",
            date=datetime(2024, 1, 15, tzinfo=UTC),
        )

        assert threader.assign_thread(second).thread_id == "meet_y@example.com"

    def test_subject_fallback_rejected_when_too_distant_in_time(self, db, threader):
        """Subject fallback must also respect a time window — a matching
        subject more than a year later is almost certainly a different
        conversation even if the participants overlap."""
        original = make_message(
            message_id="sub_stale_orig@example.com",
            subject="Invoice",
            from_addr="alice@example.com",
            to_addrs=["bob@example.com"],
            date=datetime(2023, 1, 1, tzinfo=UTC),
        )
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, [0.0] * EMBEDDING_DIM)

        # Shares participants but arrives well outside the fallback window.
        later = make_message(
            message_id="sub_stale_later@example.com",
            subject="Re: Invoice",
            from_addr="alice@example.com",
            to_addrs=["bob@example.com"],
            filepath="/maildir/INBOX/cur/later",
            date=datetime(2024, 6, 1, tzinfo=UTC),
        )
        t2 = threader.assign_thread(later)
        assert t2.thread_id == "sub_stale_later@example.com"

    def test_subject_fallback_does_not_cross_folders(self, db, threader):
        """Subject matching is scoped to the same folder."""
        inbox_msg = make_message(
            message_id="inbox_msg@example.com",
            subject="Hello world",
            folder="INBOX",
        )
        t1 = threader.assign_thread(inbox_msg)
        db.upsert_thread(t1, [0.0] * EMBEDDING_DIM)

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
        db.upsert_thread(t1, [0.0] * EMBEDDING_DIM)

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
        db.upsert_thread(t1, [0.0] * EMBEDDING_DIM)

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
        db.upsert_thread(t1, [0.0] * EMBEDDING_DIM)

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
# _normalize_subject
# ---------------------------------------------------------------------------


class TestNormalizeSubject:
    def test_strips_english_prefixes(self):
        assert _normalize_subject("Re: Hello") == "hello"
        assert _normalize_subject("Fwd: Hello") == "hello"
        assert _normalize_subject("FW: Hello") == "hello"

    def test_strips_german_prefixes(self):
        assert _normalize_subject("Aw: Hallo") == "hallo"
        assert _normalize_subject("Ant: Hallo") == "hallo"

    def test_strips_swedish_and_french_prefixes(self):
        assert _normalize_subject("Sv: Hej") == "hej"
        assert _normalize_subject("Tr: Bonjour") == "bonjour"

    def test_strips_chinese_reply_prefixes(self):
        assert _normalize_subject("回复: 你好") == "你好"
        assert _normalize_subject("答复: 你好") == "你好"

    def test_strips_nested_mixed_prefixes(self):
        assert _normalize_subject("Re: Fwd: Sv: Hello") == "hello"

    def test_preserves_subjects_without_prefix_punctuation(self):
        # ``R&D meeting`` must not be mangled: the regex requires
        # whitespace/colon/bracket after the prefix candidate. Without
        # that gate, single-letter or short prefixes would silently strip
        # the start of unrelated subjects.
        assert _normalize_subject("R&D meeting") == "r&d meeting"
        assert _normalize_subject("Trend report") == "trend report"
        assert _normalize_subject("Aware of the issue") == "aware of the issue"

    def test_does_not_strip_space_separated_two_letter_lookalikes(self):
        # ``aw|ant|sv|tr`` are language-specific reply prefixes whose
        # two-letter form collides with English / cross-language
        # subjects starting with the same letters. Real clients
        # universally emit the colon shape (``Aw:``, ``Sv:``, ``Tr:``,
        # ``Ant:``), so the normalizer requires explicit ``:``/``[``
        # punctuation rather than bare whitespace. Without that
        # restriction, ``Tr report on Q1`` collapsed to ``report on q1``
        # and silently merged onto an unrelated thread.
        assert _normalize_subject("Tr report on Q1") == "tr report on q1"
        assert _normalize_subject("Sv anything") == "sv anything"
        assert _normalize_subject("Aw report") == "aw report"
        assert _normalize_subject("Ant question?") == "ant question?"

    def test_still_strips_two_letter_prefixes_with_colon(self):
        # The colon-only restriction must NOT affect real reply
        # detection: every legitimate client emits the colon shape, so
        # ``Aw:``, ``Sv:``, ``Tr:``, ``Ant:`` continue to strip.
        assert _normalize_subject("Aw: Hallo") == "hallo"
        assert _normalize_subject("Sv: Hej") == "hej"
        assert _normalize_subject("Tr: Bonjour") == "bonjour"
        assert _normalize_subject("Ant: Hallo") == "hallo"

    def test_collapses_whitespace(self):
        assert _normalize_subject("Re:    Project   Update") == "project update"


_REFERENCE_PREFIX_RE = re.compile(
    r"^(?:(?:re|fwd|fw|回复|答复)[\s:\[\]]+|(?:aw|ant|sv|tr)[:\[\]]+)",
    re.IGNORECASE,
)


def _reference_normalize_subject(subject: str) -> str:
    """The pre-#293 algorithm: strip one prefix per pass, copying the rest.

    Ground truth for the differential test below; quadratic in the number
    of prefixes, so only called on short inputs.
    """
    s = subject.lower().strip()
    while True:
        stripped = _REFERENCE_PREFIX_RE.sub("", s).strip()
        if stripped == s:
            break
        s = stripped
    return re.sub(r"\s+", " ", s).strip()


# Fragments covering every prefix alternative, the separator class
# (whitespace, colon, brackets), Unicode whitespace, lookalike words and
# the empty string. Every ordered triple is checked, so prefixes appear
# nested, mid-subject, separated by each kind of gap, and alone.
_SUBJECT_FRAGMENTS = [
    "Re:",
    "re ",
    "RE",
    "Fwd:",
    "fw[",
    "FW]",
    "Aw:",
    "ant:",
    "Sv",
    "tr]",
    "回复:",
    "答复 ",
    " ",
    "\t",
    "　",
    " ",
    ":",
    "[",
    "x",
    "Report",
    "",
]


class TestNormalizeSubjectMatchesReference:
    def test_every_fragment_triple_matches_the_previous_algorithm(self):
        mismatches = [
            subject
            for subject in (
                a + b + c
                for a in _SUBJECT_FRAGMENTS
                for b in _SUBJECT_FRAGMENTS
                for c in _SUBJECT_FRAGMENTS
            )
            if _normalize_subject(subject) != _reference_normalize_subject(subject)
        ]
        assert mismatches == []


class _CopyCountingPattern:
    """Wrap a compiled pattern and total the length of each new string
    object it is handed, i.e. the characters the caller copied to get
    there. Holds only the last string, so a quadratic caller cannot
    exhaust memory while being measured."""

    def __init__(self, pattern: re.Pattern[str]) -> None:
        self._pattern = pattern
        self._last: str | None = None
        self.chars_copied = 0

    def _see(self, string: str) -> None:
        if string is not self._last:
            self.chars_copied += len(string)
            self._last = string

    def sub(self, repl: str, string: str) -> str:
        self._see(string)
        return self._pattern.sub(repl, string)

    def match(self, string: str, pos: int = 0) -> re.Match[str] | None:
        self._see(string)
        return self._pattern.match(string, pos)


class TestNormalizeSubjectBoundedWork:
    def test_repeated_prefixes_are_not_copied_once_per_prefix(self, monkeypatch):
        # #293: stripping one prefix per pass copied the remaining
        # subject each time, so n prefixes cost O(n^2) characters.
        counter = _CopyCountingPattern(threader._SUBJECT_PREFIX_RE)
        monkeypatch.setattr(threader, "_SUBJECT_PREFIX_RE", counter)
        subject = "Re: " * 20_000 + "Hello"

        assert _normalize_subject(subject) == "hello"
        # One lowered copy of the subject, not one per prefix.
        assert counter.chars_copied <= len(subject)

    def test_worst_case_repeated_prefixes_finish_quickly(self):
        # 1.6 MB of ``Re: `` took about 8 s before the fix (plain timing).
        subject = "Re: " * 400_000 + "Hello"
        start = time.perf_counter()
        assert _normalize_subject(subject) == "hello"
        assert time.perf_counter() - start < 2.0


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

    def test_deduplicates_display_name_variants(self):
        """``Bob Smith <bob@x>`` and ``bob@x`` are the same person — the
        older substring-comparison dedup treated them as distinct and
        left duplicate participant entries in the list."""
        msg1 = make_message(
            from_addr="Bob Smith <bob@example.com>",
            to_addrs=["alice@example.com"],
        )
        msg2 = make_message(
            message_id="msg2@example.com",
            from_addr="bob@example.com",
            to_addrs=['"Bob S." <bob@example.com>'],
        )
        result = Threader._participants([msg1, msg2])
        assert sum(1 for p in result if canonical_addr(p) == "bob@example.com") == 1
        # First-seen display form wins.
        assert "Bob Smith <bob@example.com>" in result

    def test_skips_entries_without_an_email_part(self):
        msg = make_message(from_addr="   ", to_addrs=["just a name"])
        result = Threader._participants([msg])
        assert result == []


# ---------------------------------------------------------------------------
# canonical_addr helper
# ---------------------------------------------------------------------------


class TestCanonicalAddr:
    def test_bare_email_is_lowercased(self):
        assert canonical_addr("BOB@Example.COM") == "bob@example.com"

    def test_display_name_variant_is_stripped(self):
        assert canonical_addr("Bob Smith <bob@example.com>") == "bob@example.com"

    def test_quoted_display_name_variant(self):
        assert canonical_addr('"Bob S." <bob@example.com>') == "bob@example.com"

    def test_empty_input_returns_empty(self):
        assert canonical_addr("") == ""

    def test_input_without_address_part_returns_empty(self):
        # parseaddr("just a name") returns ("just a name", ""); keep the
        # empty string so callers can filter rather than matching all
        # such rows together.
        assert canonical_addr("just a name") == ""


class TestSubjectFallbackCanonicalMatching:
    def test_accepts_display_name_vs_bare_email(self, db, threader):
        """Regression: ``Bob Smith <bob@x>`` in the incoming message's
        From must still overlap the existing thread's ``bob@x``
        participant. Prior string-equality match would have rejected
        the fallback and incorrectly started a new thread."""
        original = make_message(
            message_id="canon_orig@example.com",
            subject="Project kickoff",
            from_addr="alice@example.com",
            to_addrs=["bob@example.com"],
        )
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, [0.0] * EMBEDDING_DIM)

        # Same bob but with display name + different letter case. No
        # In-Reply-To / References headers so only subject fallback can
        # match — which now has to canonicalize to succeed.
        followup = make_message(
            message_id="canon_follow@example.com",
            subject="Re: Project kickoff",
            from_addr="Bob Smith <BOB@example.com>",
            to_addrs=["alice@example.com"],
            filepath="/maildir/INBOX/cur/canon_follow",
            date=datetime(2024, 1, 2, tzinfo=UTC),
        )
        t2 = threader.assign_thread(followup)
        assert t2.thread_id == "canon_orig@example.com"


class TestCanonicalAddrHostileInput:
    def test_unparseable_input_returns_empty_never_raises(self):
        """canonical_addr re-parses strings from many attacker-influenced
        paths (thread participants, the from_addr fallback for unparseable
        From headers), so a parser blow-up must degrade to "no address"."""
        from src.threader import canonical_addr

        assert canonical_addr("(" * 1200 + ")" * 1200 + " <bob@example.com>") == ""
        assert canonical_addr("bob@example.com") == "bob@example.com"


class TestLongSubjectCap:
    """#541: subjects are stored at most ``SUBJECT_MAX_CHARS`` characters
    (cut in ``parse_email``), so the stored thread and message subjects
    the ``threads_fts`` and rerank subject scans read are bounded, and
    subject-fallback threading compares the capped subjects."""

    @staticmethod
    def _parse(tmp_path, name: str, subject: str, day: int, message_id: str):
        from src.parser import parse_email

        path = tmp_path / "INBOX" / "cur" / f"{name}:2,S"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            b"From: alice@example.com\r\n"
            b"To: bob@example.com\r\n"
            b"Subject: " + subject.encode() + b"\r\n"
            b"Message-ID: <" + message_id.encode() + b">\r\n"
            b"Date: Mon, 0" + str(day).encode() + b" Jan 2024 12:00:00 +0000\r\n"
            b"\r\n"
            b"Body.\r\n"
        )
        msg = parse_email(path, maildir_root=tmp_path)
        assert msg is not None
        return msg

    def test_stored_subjects_are_bounded(self, tmp_path, db, threader):
        from src.parser import SUBJECT_MAX_CHARS

        msg = self._parse(tmp_path, "a", "Q" * 3_000_000, 1, "long_a@example.com")
        t = threader.assign_thread(msg)
        db.upsert_thread(t, [0.0] * EMBEDDING_DIM)

        stored = db._conn.execute(
            "SELECT max(length(m.subject)), max(length(t.subject)), "
            "max(length(t.display_subject)) "
            "FROM messages m JOIN threads t ON t.thread_id = m.thread_id"
        ).fetchone()
        assert tuple(stored) == (SUBJECT_MAX_CHARS,) * 3

    def test_subjects_sharing_the_capped_prefix_compare_equal(self, tmp_path, db, threader):
        """Documented limitation: two subjects identical in their first
        ``SUBJECT_MAX_CHARS`` characters are the same subject to the
        fallback, which still requires the same folder, a shared sender
        and recipient, and the 60-day window."""
        from src.parser import SUBJECT_MAX_CHARS

        prefix = "w" * SUBJECT_MAX_CHARS
        first = self._parse(tmp_path, "a", prefix + " first", 1, "pfx_a@example.com")
        t1 = threader.assign_thread(first)
        db.upsert_thread(t1, [0.0] * EMBEDDING_DIM)

        second = self._parse(tmp_path, "b", prefix + " second", 2, "pfx_b@example.com")
        assert second.subject == first.subject
        assert threader.assign_thread(second).thread_id == "pfx_a@example.com"

    def test_reply_prefix_past_the_cap_misses_the_subject_fallback(self, tmp_path, db, threader):
        """Documented limitation: ``Re: `` takes four of the capped
        characters, so a reply to a subject longer than the cap, with no
        In-Reply-To / References, no longer matches it by subject."""
        from src.parser import SUBJECT_MAX_CHARS

        subject = "v" * SUBJECT_MAX_CHARS
        original = self._parse(tmp_path, "a", subject, 1, "shift_a@example.com")
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, [0.0] * EMBEDDING_DIM)

        reply = self._parse(tmp_path, "b", "Re: " + subject, 2, "shift_b@example.com")
        assert threader.assign_thread(reply).thread_id == "shift_b@example.com"

    def test_reply_to_a_subject_within_the_cap_still_matches(self, tmp_path, db, threader):
        from src.parser import SUBJECT_MAX_CHARS

        subject = "u" * (SUBJECT_MAX_CHARS - len("Re: "))
        original = self._parse(tmp_path, "a", subject, 1, "fits_a@example.com")
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, [0.0] * EMBEDDING_DIM)

        reply = self._parse(tmp_path, "b", "Re: " + subject, 2, "fits_b@example.com")
        assert threader.assign_thread(reply).thread_id == "fits_a@example.com"
