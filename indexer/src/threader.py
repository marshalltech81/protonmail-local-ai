"""
Email threader.
Groups messages into threads using In-Reply-To and References headers.
Indexes at the thread level — the unit Claude reasons about.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from .parser import Message

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
            parts.append(msg.body_text[:2000])
            parts.append("")

        return "\n".join(parts)[:8000]

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
            # Add to existing thread
            thread = self.db.get_thread(thread_id)
            if thread:
                thread.messages.append(message)
                thread.messages.sort(key=lambda m: m.date)
                thread.date_last = max(m.date for m in thread.messages)
                thread.participants = self._participants(thread.messages)
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

        # Fall back to normalized subject matching within the same folder
        normalized = _normalize_subject(message.subject)
        if normalized:
            thread_id = self.db.find_thread_by_subject(normalized, message.folder)
            if thread_id:
                return thread_id

        return None

    @staticmethod
    def _participants(messages: list[Message]) -> list[str]:
        seen = set()
        result = []
        for msg in messages:
            for addr in [msg.from_addr] + msg.to_addrs + msg.cc_addrs:
                addr = addr.strip()
                if addr and addr not in seen:
                    seen.add(addr)
                    result.append(addr)
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
