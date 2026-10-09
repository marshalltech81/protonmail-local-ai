"""
Email threader.
Groups messages into threads using In-Reply-To and References headers.
Indexes at the thread level — the unit Claude reasons about.
"""

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import islice

from .extractors import warn_rate_limited
from .parser import NO_SUBJECT, Message, canonical_addr

# Reply / forward prefixes the subject normalizer strips before grouping.
# Hoisted to module level so the compiled regex is reused across every
# incoming message instead of recompiling per call.
#
# Two prefix classes, distinguished by what punctuation must follow.
#
# CLASS 1 — well-known, low false-strip risk: ``re|fwd|fw|回复|答复``.
# Accept the full punctuation set ``[\s:\[\]]+`` because the prefix
# is universally recognized and a real subject starting with
# ``Re report ...`` (missing colon) is functionally always a reply,
# not English prose. ``回复`` / ``答复`` are CJK so cannot collide
# with English word starts.
#
# CLASS 2 — language-specific two-letter abbreviations, high
# false-strip risk against English prose: ``aw|ant|sv|tr``. Require
# explicit ``[:\[\]]+`` punctuation (no bare whitespace) so English /
# cross-language subjects like ``Tr report on Q1``, ``Sv anything``,
# ``Ant question?``, or ``Aw report`` are NOT silently treated as
# reply prefixes and merged onto an unrelated thread. Real
# ``Aw``/``Ant`` (German), ``Sv`` (Swedish), ``Tr`` (French) clients
# universally emit the colon shape (``Aw:``, ``Sv:``, ``Tr:``), so
# this restriction does not affect real reply detection. The colon
# requirement is what makes the existing reasoning (single-letter
# prefixes are excluded because of false-strip risk) actually hold
# for the two-character cases that share surface with English prose.
#
# Single-letter prefixes (Italian ``R:``, French ``Réf:``) remain
# excluded — even with the colon-only restriction, the false-strip
# surface for one-letter starts (e.g. ``R: meeting`` vs ``R&D recap``)
# is too narrow to disambiguate.
#
# No ``^`` anchor: callers use ``.match(s, pos)``, which anchors at
# ``pos``, where ``^`` would only ever match at index 0.
_SUBJECT_PREFIX_RE = re.compile(
    r"(?:(?:re|fwd|fw|回复|答复)[\s:\[\]]+|(?:aw|ant|sv|tr)[:\[\]]+)",
    re.IGNORECASE,
)
_SUBJECT_WHITESPACE_RE = re.compile(r"\s+")


# Subject-only fallback is a last-resort threading path: any two messages
# with the same normalized subject in the same folder would otherwise
# collapse into a single thread. That produces false merges for common
# subjects ("Re: Hello", "Invoice", "Follow up"), which is particularly
# dangerous for invoice/legal/HOA records. Require at least one shared
# participant AND proximity in time before accepting a subject-only
# match; otherwise start a new thread.
SUBJECT_FALLBACK_WINDOW = timedelta(days=60)

# Cap for the stored thread body text (the thread-level FTS input; the
# thread vector is the mean of its chunk vectors, not an embedding of
# this text). Used by both the fresh insert path
# (``Thread.build_body_text``) and the accumulation path in
# ``Database._compute_body``. Defining a single
# constant keeps brand-new threads and later-updated threads on the
# same footing: without it, a reply that arrives after the initial
# insert could expand the stored body well past what the insert path
# would have kept, and the FTS input would drift between the two code
# paths.
#
# Token-based, not char-based: a char cap under-counts CJK / URL /
# Base64 / dense code text by 4-6× and forces unnecessarily aggressive
# truncation in ASCII-heavy threads. 4000 tokens still bounds the
# stored body and its FTS row on pathological threads.
THREAD_BODY_TEXT_MAX_TOKENS = 4000

# Per-message char cap applied when packing message bodies into the
# thread-level body_text. Char-based here (vs token-based for the
# thread-wide cap above) because this is a crude per-message limiter to
# stop one outlier from dominating the thread's FTS contribution before
# the thread-wide token cap fires. The two paths that build body_text
# (``Thread.build_body_text`` on fresh insert,
# ``Database._compute_body`` on update) must share this constant so a
# thread's FTS coverage does not depend on whether it arrived as one
# message or as a sequence of replies — the previous shape kept fresh
# inserts at 500 and updates at 2000, producing a permanent FTS
# asymmetry tied to arrival ordering.
PER_MESSAGE_BODY_CAP_CHARS = 2000

log = logging.getLogger("indexer.threader")


def message_date_line(msg: Message) -> str:
    """The ``Date:`` line a message contributes to its thread's FTS body,
    or nothing when its send date is unknown (#1080): no made-up date
    is indexed as text."""
    return f"Date: {msg.date.isoformat()}\n" if msg.date is not None else ""


@dataclass
class Thread:
    thread_id: str
    subject: str
    participants: list[str]
    messages: list[Message]
    folder: str
    date_first: datetime
    date_last: datetime

    def build_body_text(self) -> str:
        """
        Build the thread's stored ``body_text`` (its FTS input).

        Thread vectors are the mean of the thread's chunk vectors, so
        this text is not embedded. Includes subject, participants, and
        all message bodies, trimmed to ``THREAD_BODY_TEXT_MAX_TOKENS``
        real BPE tokens to match the cap the accumulation path in
        ``Database._compute_body`` applies on update.
        """
        from .chunker import truncate_to_tokens

        parts = [
            f"Subject: {self.subject}",
            f"Participants: {', '.join(self.participants)}",
            "",
        ]
        for msg in self.messages:
            parts.append(f"From: {msg.from_addr}")
            if msg.date is not None:
                parts.append(f"Date: {msg.date.isoformat()}")
            # Cap per-message body so the joined string stays bounded
            # before the thread-level truncation below. Shared with
            # ``Database._compute_body`` via ``PER_MESSAGE_BODY_CAP_CHARS``
            # so fresh-insert and update paths agree on what each
            # message contributes to the thread's FTS body field.
            parts.append(msg.body_text[:PER_MESSAGE_BODY_CAP_CHARS])
            parts.append("")

        return truncate_to_tokens("\n".join(parts), THREAD_BODY_TEXT_MAX_TOKENS)

    def snippet(self) -> str:
        """Short preview for search results."""
        if self.messages:
            return self.messages[-1].body_text[:200].replace("\n", " ")
        return ""


class Threader:
    """
    Assigns messages to threads using header lookups followed by a
    guarded subject fallback:
    0. Keep the thread an already-indexed message has
    1. Check In-Reply-To header
    2. Check References headers (most recent first)
    3. Fall back to normalized-subject matching within the same folder,
       gated by a shared correspondent pair and a 60-day proximity window
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
                thread.messages.sort(key=lambda m: m.effective_date)
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
                thread.date_first = min(thread.date_first, message.effective_date)
                thread.date_last = max(thread.date_last, message.effective_date)
                return thread

        # Create a new thread rooted at this message
        thread_id = message.message_id
        thread = Thread(
            thread_id=thread_id,
            subject=_normalize_subject(message.subject),
            participants=self._participants([message]),
            messages=[message],
            folder=message.folder,
            date_first=message.effective_date,
            date_last=message.effective_date,
        )
        return thread

    def _find_thread_id(self, message: Message) -> str | None:
        # A message already threaded keeps its thread. Reprocessing (a
        # rename seen while the indexer was down, a retry) must not
        # re-resolve it from headers: a reply indexed before its parent
        # would move to the parent's thread in the map while its chunks
        # and the old thread row still claimed it.
        existing = self.db.find_thread_by_message_id(message.message_id)
        if existing:
            return existing

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
        # Only accept the fallback when the incoming sender and one of its
        # recipients are both already thread participants and the
        # message's date falls within SUBJECT_FALLBACK_WINDOW of the
        # thread's last activity.
        # Without these guards, any two "Re: Hello" / "Invoice" / "Follow
        # up" messages in the same folder would merge into one thread.
        #
        # Check multiple candidates newest-first: the most recent thread
        # with this normalized subject may be an unrelated "Invoice" /
        # "Follow up" from a different sender, but an older thread in the
        # same folder can still be a valid match.
        #
        # A message with an ambiguous sender (#1144: a repeated From, or a
        # header scan that stopped at its field cap before a second From
        # could be ruled out) is never matched this way, since the check
        # trusts its author. Ambiguous messages cannot join by subject
        # alone or supply correspondent evidence for another subject-only
        # merge. NULL supplies no evidence; assessed messages in mixed
        # threads can still qualify (``_subject_fallback_accepts``).
        if message.sender_ambiguous:
            return None
        normalized = _normalize_subject(message.subject)
        if normalized:
            candidate_ids = self.db.find_threads_by_subject(normalized, message.folder)
            rejected = 0
            for candidate_id in candidate_ids:
                accepted, unproven = self._subject_fallback_accepts(message, candidate_id)
                if accepted:
                    self._log_provenance_rejections(rejected, message)
                    return candidate_id
                rejected += unproven
            self._log_provenance_rejections(rejected, message)

        return None

    @staticmethod
    def _log_provenance_rejections(rejected: int, message: Message) -> None:
        """One INFO line per message whose subject fallback turned down
        candidates only because their correspondents come from no
        assessed message (#1144): the count and the path, rate limited."""
        if rejected:
            warn_rate_limited(
                log,
                "subject fallback rejected %d candidate thread(s) without assessed "
                "correspondents for %s",
                rejected,
                message.filepath,
                level=logging.INFO,
                attachment=False,
            )

    def _subject_fallback_accepts(self, message: Message, candidate_id: str) -> tuple[bool, bool]:
        """Gate the subject-only thread merge with a correspondent check +
        date proximity. Returns whether the fallback is safe, and whether
        it was refused only for want of assessed evidence (the displayed
        participants would have passed).

        An incoming author and at least one of its other recipients must
        both already be thread participants — the same pair of people
        corresponding. Any single shared address is not enough: the
        mailbox owner is a recipient of nearly every message (two
        vendors' "Invoice" mails would merge) and the sender of every
        outgoing one (a "Meeting" note to X and another to Y would).

        Both sides are compared by canonical address so display-name
        variants (``Bob Smith <bob@x>`` vs ``bob@x``) do not cause
        spurious "no participant overlap" results.

        The evidence must come from the thread's messages assessed safe
        (#1144, ``Database.thread_has_assessed_correspondents``), the
        author and the recipient each from any such message: a message
        with an ambiguous or not yet assessed sender supplies none.
        """
        thread = self.db.get_thread(candidate_id)
        if thread is None:
            return False, False

        # The date window first: a candidate it rejects is not counted as
        # a provenance rejection whatever its evidence.
        if abs(message.effective_date - thread.date_last) > SUBJECT_FALLBACK_WINDOW:
            return False, False

        authors = {canonical_addr(addr) for addr in _authors(message)}
        authors.discard("")
        recipients = {canonical_addr(addr) for addr in [*message.to_addrs, *message.cc_addrs]}
        recipients.discard("")
        recipients -= authors
        if not self.db.thread_has_assessed_correspondents(
            candidate_id, sorted(authors), sorted(recipients)
        ):
            # Counted as a provenance rejection only when the displayed
            # participants would have passed the old check.
            thread_canonical = {canonical_addr(addr) for addr in thread.participants}
            unproven = bool(authors & thread_canonical) and bool(recipients & thread_canonical)
            return False, unproven
        return True, False

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
            for addr in [*_authors(msg), *msg.to_addrs, *msg.cc_addrs]:
                stripped = addr.strip()
                if not stripped:
                    continue
                key = canonical_addr(stripped)
                if not key or key in seen_canonical:
                    continue
                seen_canonical.add(key)
                result.append(stripped)
        return result


def _authors(msg: Message) -> list[str]:
    """Every From author (a From header may list several), falling back
    to ``from_addr`` when the parser found no structured address."""
    return msg.from_addrs or [msg.from_addr]


def _normalize_subject(subject: str) -> str:
    """
    Strip reply/forward prefixes and collapse whitespace for matching.

    Loops until no more prefixes can be removed so deeply-nested reply
    chains ('Re: Re: Fwd: Hello') collapse to the bare subject
    ('hello'). The prefix set is the conservative cross-language list
    described in ``_SUBJECT_PREFIX_RE``.

    Advances an offset past each prefix and the whitespace after it,
    then slices once, so the work is linear in the subject length. The
    earlier strip-and-copy per prefix was quadratic in the prefix count
    and let a crafted ``Re: Re: ...`` subject stall the worker (#293).
    """
    s = subject.lower().strip()
    pos = 0
    while match := _SUBJECT_PREFIX_RE.match(s, pos):
        pos = match.end()
        # Same whitespace set ``str.strip()`` removed between passes.
        while pos < len(s) and s[pos].isspace():
            pos += 1
    return _SUBJECT_WHITESPACE_RE.sub(" ", s[pos:]).strip()


# Bounds on the changed reply subjects ``fts_subject_text`` adds to a
# thread's ``threads_fts`` subject column (#303): at most this many
# distinct subjects, and this many characters across them.
FTS_REPLY_SUBJECTS_MAX = 20
FTS_REPLY_SUBJECTS_MAX_CHARS = 2000
# Work bound on building that column, applied on every thread rewrite:
# at most this many stored subjects are examined (oldest first), each
# cut to this many characters before normalization. The insert/update
# path applies both in SQL; the bytes it reads per row are bounded by
# the parser storing at most ``SUBJECT_MAX_CHARS`` characters of a
# subject (#541), since ``substr()`` bounds only what it returns.
# Without them each upsert re-read and re-normalized every stored
# subject, quadratic over a long thread of long subjects (#439).
FTS_SUBJECT_SCAN_ROWS = 200
FTS_SUBJECT_SCAN_CHARS = 500


def fts_subject_text(thread_subject: str, subjects: Iterable[str]) -> str:
    """Text for the ``threads_fts`` subject column: the thread subject,
    then each message subject that differs from it after normalization
    (deduplicated, normalized form, in input order).

    A reply can change the subject and still join the thread through
    References / In-Reply-To. ``threads.subject`` keeps the thread's
    subject for display and grouping; this column is what keeps the
    reply's words keyword-searchable (#303). Writing them here rather
    than into ``body_text`` means a body already at its token cap
    cannot drop them. Output is bounded by ``FTS_REPLY_SUBJECTS_MAX``
    subjects and ``FTS_REPLY_SUBJECTS_MAX_CHARS`` characters; work by
    ``FTS_SUBJECT_SCAN_ROWS`` inputs of ``FTS_SUBJECT_SCAN_CHARS``
    characters each. A changed subject first seen past the scanned rows,
    or differing only past the scanned characters, is not added.
    """
    seen = {_normalize_subject(thread_subject[:FTS_SUBJECT_SCAN_CHARS])}
    parts = [thread_subject]
    used = 0
    for subject in islice(subjects, FTS_SUBJECT_SCAN_ROWS):
        if len(parts) > FTS_REPLY_SUBJECTS_MAX:
            break
        normalized = _normalize_subject(subject[:FTS_SUBJECT_SCAN_CHARS])
        if not normalized or normalized in seen:
            continue
        if used + len(normalized) > FTS_REPLY_SUBJECTS_MAX_CHARS:
            break
        seen.add(normalized)
        parts.append(normalized)
        used += len(normalized)
    return "\n".join(parts)


def subject_embed_line(msg: Message) -> str | None:
    """``Subject: <msg.subject>``, or ``None`` when the subject is blank
    after normalization or is the parser's ``NO_SUBJECT`` placeholder
    (a missing header, a literal "(no subject)", or a reply to one).

    The indexer puts it in front of the message's first body chunk in
    the embedding input only, so the subject is in the chunk vector and
    the thread vector, their mean (#303, #687). Every message carries
    its own subject, not only a reply that changed it: a topic named
    only in the subject would otherwise be in no vector. The stored
    chunk text, its offsets and its ID stay body-only: chunks are the
    authoritative body store.
    """
    if _normalize_subject(msg.subject) in ("", NO_SUBJECT):
        return None
    return f"Subject: {msg.subject}"
