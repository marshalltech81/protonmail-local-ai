"""
Email threader.
Groups messages into threads using In-Reply-To and References headers.
Indexes at the thread level — the unit Claude reasons about.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parseaddr

from .parser import Message


def canonical_addr(value: str) -> str:
    """Normalize an address string to a lowercase bare email for matching.

    RFC 2822 ``From`` / ``To`` headers can carry the same person as
    ``Bob Smith <bob@example.com>``, ``bob@example.com``, or
    ``"Bob S." <bob@example.com>`` — stable string comparison treats
    those three as different participants and produces false misses for
    subject-fallback matching and duplicate entries in participant
    lists. ``parseaddr`` extracts the bare address; lowercasing makes
    the match case-insensitive.

    Returns an empty string when no usable email address can be
    recovered. ``parseaddr`` is permissive and will return a first-token
    value like ``"just"`` for a header like ``"just a name"`` — rejecting
    results without an ``@`` keeps malformed entries from becoming their
    own spurious "participant" and from matching other malformed entries
    to each other.
    """
    if not value:
        return ""
    _, addr = parseaddr(value)
    addr = addr.strip().lower()
    if "@" not in addr:
        return ""
    return addr


# Subject-only fallback is a last-resort threading path: any two messages
# with the same normalized subject in the same folder would otherwise
# collapse into a single thread. That produces false merges for common
# subjects ("Re: Hello", "Invoice", "Follow up"), which is particularly
# dangerous for invoice/legal/HOA records. Require at least one shared
# participant AND proximity in time before accepting a subject-only
# match; otherwise start a new thread.
SUBJECT_FALLBACK_WINDOW = timedelta(days=60)

log = logging.getLogger("indexer.threader")


@dataclass
class Thread:
    thread_id: str
    subject: str
    participants: list[str]
    messages: list[Message]
    folder: str
    date_first: datetime
    date_last: datetime

    def text_for_embedding(self) -> str:
        """
        Build a single text representation of the thread for embedding.
        Includes subject, participants, and all message bodies.
        Trimmed to ~8000 chars to stay within embedding model context.
        """
        parts = [
            f"Subject: {self.subject}",
            f"Participants: {', '.join(self.participants)}",
            "",
        ]
        for msg in self.messages:
            parts.append(f"From: {msg.from_addr}")
            parts.append(f"Date: {msg.date.isoformat()}")
            # nomic-embed-text has a 2048-token context window (~4 chars/token).
            # 500 chars ≈ 125 tokens per message, leaving room for multiple
            # messages and the subject/participants header within the 4000-char
            # thread cap (~1000 tokens, safely under the model limit).
            parts.append(msg.body_text[:500])
            parts.append("")

        return "\n".join(parts)[:4000]

    def snippet(self) -> str:
        """Short preview for search results."""
        if self.messages:
            return self.messages[-1].body_text[:200].replace("\n", " ")
        return ""


class Threader:
    """
    Assigns messages to threads using a two-pass strategy:
    1. Check In-Reply-To header
    2. Check References headers
    3. Fall back to subject-based matching (normalized subject)
    4. Create a new thread if no match found
    """

    def __init__(self, db):
        self.db = db

    def assign_thread(self, message: Message) -> Thread:
        # Try to find an existing thread this message belongs to
        thread_id = self._find_thread_id(message)

        if thread_id:
            # Add to existing thread. get_thread() returns messages=[] by
            # design — the caller is expected to append only the new message
            # and the database layer merges accumulated fields on upsert.
            # Participants loaded from the DB must therefore be unioned with
            # the new message's participants rather than replaced, and dates
            # must widen (min for date_first, max for date_last) to tolerate
            # out-of-order Maildir delivery.
            thread = self.db.get_thread(thread_id)
            if thread:
                thread.messages.append(message)
                thread.messages.sort(key=lambda m: m.date)
                # Dedup by canonical address so ``Bob <bob@x>`` does not
                # shadow ``bob@x`` already in the list. Keep the existing
                # (richer) display string when a canonical duplicate
                # arrives; add the new display string only when we have
                # no entry for that canonical address yet.
                seen_canonical = {canonical_addr(addr) for addr in thread.participants}
                for addr in self._participants([message]):
                    key = canonical_addr(addr)
                    if key and key not in seen_canonical:
                        thread.participants.append(addr)
                        seen_canonical.add(key)
                thread.date_first = min(thread.date_first, message.date)
                thread.date_last = max(thread.date_last, message.date)
                return thread

        # Create a new thread rooted at this message
        thread_id = message.message_id
        thread = Thread(
            thread_id=thread_id,
            subject=_normalize_subject(message.subject),
            participants=self._participants([message]),
            messages=[message],
            folder=message.folder,
            date_first=message.date,
            date_last=message.date,
        )
        return thread

    def _find_thread_id(self, message: Message) -> str | None:
        # Check In-Reply-To
        if message.in_reply_to:
            thread_id = self.db.find_thread_by_message_id(message.in_reply_to)
            if thread_id:
                return thread_id

        # Check References (most recent first)
        for ref in reversed(message.references):
            thread_id = self.db.find_thread_by_message_id(ref)
            if thread_id:
                return thread_id

        # Fall back to normalized subject matching within the same folder.
        # Only accept the fallback when the candidate thread shares at least
        # one participant with the incoming message and the message's date
        # falls within SUBJECT_FALLBACK_WINDOW of the thread's last activity.
        # Without these guards, any two "Re: Hello" / "Invoice" / "Follow
        # up" messages in the same folder would merge into one thread.
        #
        # Check multiple candidates newest-first: the most recent thread
        # with this normalized subject may be an unrelated "Invoice" /
        # "Follow up" from a different sender, but an older thread in the
        # same folder can still be a valid match.
        normalized = _normalize_subject(message.subject)
        if normalized:
            candidate_ids = self.db.find_threads_by_subject(normalized, message.folder)
            for candidate_id in candidate_ids:
                if self._subject_fallback_accepts(message, candidate_id):
                    return candidate_id

        return None

    def _subject_fallback_accepts(self, message: Message, candidate_id: str) -> bool:
        """Gate the subject-only thread merge with participant overlap +
        date proximity checks. Returns True if the fallback is safe.

        Both sides are compared by canonical address so display-name
        variants (``Bob Smith <bob@x>`` vs ``bob@x``) do not cause
        spurious "no participant overlap" results.
        """
        thread = self.db.get_thread(candidate_id)
        if thread is None:
            return False

        incoming_canonical = {
            canonical_addr(addr)
            for addr in [message.from_addr, *message.to_addrs, *message.cc_addrs]
        }
        incoming_canonical.discard("")
        thread_canonical = {canonical_addr(addr) for addr in thread.participants}
        thread_canonical.discard("")
        if not incoming_canonical.intersection(thread_canonical):
            return False

        delta = abs(message.date - thread.date_last)
        return delta <= SUBJECT_FALLBACK_WINDOW

    @staticmethod
    def _participants(messages: list[Message]) -> list[str]:
        # Dedup by canonical lowercase email so ``Bob <bob@x>`` does not
        # appear separately from ``bob@x`` (or ``BOB@X``) in the output.
        # The richer display string wins when duplicates exist because
        # the first-seen entry is preserved; headers without an email
        # address part are skipped.
        seen_canonical: set[str] = set()
        result: list[str] = []
        for msg in messages:
            for addr in [msg.from_addr, *msg.to_addrs, *msg.cc_addrs]:
                stripped = addr.strip()
                if not stripped:
                    continue
                key = canonical_addr(stripped)
                if not key or key in seen_canonical:
                    continue
                seen_canonical.add(key)
                result.append(stripped)
        return result


def _normalize_subject(subject: str) -> str:
    """
    Strip Re:, Fwd:, and whitespace variants from subject for matching.
    Loops until no more prefixes can be removed so deeply-nested reply
    chains ('Re: Re: Fwd: Hello') collapse to the bare subject ('hello').
    """
    import re

    prefix = re.compile(r"^(re|fwd|fw|aw|ant)[\s:\[\]]+", re.IGNORECASE)
    s = subject.lower().strip()
    while True:
        stripped = prefix.sub("", s).strip()
        if stripped == s:
            break
        s = stripped
    return re.sub(r"\s+", " ", s).strip()
