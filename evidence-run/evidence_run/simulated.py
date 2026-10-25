"""An in-memory world for ``--dry-run`` and the tests (procedure v5 4).

It models what the procedure predicts of Bridge, never what Proton does:
delivery rebuilds a message with one ``X-Pm-Internal-Id`` and one
``X-Pm-Gluon-Id`` (topmost) and drops forged fields, moves change a UID,
a sync writes the isync-shaped copy of each message (LF endings, one
``X-TUID``). A ``Scenario`` flips each behaviour, so a test can make any
prediction fail, a route not preserve fields, a service or foreign message
arrive, a warning appear or isolation fail. Nothing here touches a
network, a file or an account.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from . import correlation
from .world import FlagScope, HeaderPass, RaceResult, Served, WorldError

DEFAULT_H11 = {
    "a": "rebuilt",
    "a2": "refused",
    "b": "no_trigger",
    "c1": "recovered_unchanged",
    "c2": "rebuilt",
    "d": "reused_existing",
    "recovery_copy": "imported",
    "recovery_move": "imported",
}
STEP_EFFECTS = {  # what each G7 step does to every message, by default
    "b": {"bytes"},
    "c2": {"gluon_id", "uid"},
    "d": {"gluon_id", "uid", "bytes"},
    "g": {"uidvalidity"},
}


@dataclass
class Scenario:
    route_preserves: dict[str, bool | None] = field(
        default_factory=lambda: {"B": True, "C": True, "A2": True}
    )
    forged_internal_survives: bool = False
    forged_gluon_survives: bool = False
    proton_id_changes_on: frozenset[str] = frozenset()  # mutate ops that change the Proton ID
    draft_replace_changes_id: bool = True
    draft_send_keeps_id: bool = True
    append_distinct_ids: bool = True
    h11: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_H11))
    header_read_sets_flags: int = 0
    membership_ok: bool = True
    race_neither: int = 0
    race_id_changed: bool = False
    cold_provable: bool = False
    unavailable_steps: frozenset[str] = frozenset({"e", "i"})  # no reviewed trigger
    step_effects: dict[str, set[str]] = field(default_factory=lambda: dict(STEP_EFFECTS))
    # What changes proton_id for a step (default: nothing does).
    step_changes_proton_id: frozenset[str] = frozenset()
    service_after_sends: int | None = None  # a service message arrives
    foreign_after_sends: int | None = None  # a message from nobody on the list
    foreign_party_after_sends: int | None = None  # a test token from a foreign domain
    warning_after_sends: int | None = None
    warning_token: str = "rate_limit"
    extra_account_after_sends: int | None = None
    isolation_fails_on_call: int | None = None
    restart_after_sends: int | None = None
    uid_reuse: bool = False  # the Spam folder hands out a UID twice
    file_drift: bool = False  # isync's copy differs by more than LF and X-TUID
    fresh_drift: bool = False  # a fresh download differs by more than LF and X-TUID
    fresh_available: bool = True
    flag_scope_ok: bool = True
    ambiguous_sends: int = 0  # the first N sends report an ambiguous outcome
    send_result: str = "sent"
    never_deliver: frozenset[str] = frozenset()  # tokens that never appear
    dup_internal_in_control: bool = False  # Bridge keeps a second Internal-Id field


@dataclass
class _Msg:
    token: str | None
    folder: str
    proton_id: str
    gluon_id: str
    uid: int
    flags: set[str]
    raw: bytes
    keep_internal: bool
    keep_gluon: bool
    salt: int = 0
    synced: bool = False
    verbatim: bool = False  # kept as submitted (Recovered Messages, H11)


class SimulatedWorld:
    reviewed_trigger: ClassVar[bool] = False

    def __init__(self, scenario: Scenario | None = None) -> None:
        self.s = scenario or Scenario()
        self.msgs: list[_Msg] = []
        self.sends = 0
        self.syncs = 0
        self.isolation_calls = 0
        self.uidvalidity = 1
        self._next_uid: dict[str, int] = {}
        self._counter = 0
        self.operator_log: list[str] = []
        self.mutate_log: list[tuple[str, str]] = []

    # ---- internals -------------------------------------------------

    def _new_ids(self) -> tuple[str, str]:
        self._counter += 1
        return f"SYNTHPM{self._counter:06d}", f"SYNTHGLUON-{self._counter:06d}"

    def _uid(self, folder: str) -> int:
        if self.s.uid_reuse and folder == "Spam" and folder in self._next_uid:
            return 1  # a reused UID: the failure G8 must catch
        self._next_uid[folder] = self._next_uid.get(folder, 0) + 1
        return self._next_uid[folder]

    @staticmethod
    def _token(raw: bytes) -> str | None:
        return correlation.summary(raw).token

    def _add(self, raw: bytes, folder: str, *, same_id_as: _Msg | None = None) -> _Msg:
        proton, gluon = self._new_ids()
        if same_id_as is not None:
            proton = same_id_as.proton_id
        msg = _Msg(
            token=self._token(raw),
            folder=folder,
            proton_id=proton,
            gluon_id=gluon,
            uid=self._uid(folder),
            flags=set(),
            raw=raw,
            keep_internal=self.s.forged_internal_survives,
            keep_gluon=self.s.forged_gluon_survives,
        )
        self.msgs.append(msg)
        return msg

    def _find(self, token: str) -> _Msg:
        for msg in reversed(self.msgs):
            if msg.token == token:
                return msg
        raise WorldError("message_not_found")

    def _served_raw(self, m: _Msg) -> bytes:
        if m.verbatim:
            return m.raw
        head, sep, body = m.raw.partition(b"\r\n\r\n")
        kept = []
        for line in head.split(b"\r\n"):
            low = line.lower()
            if low.startswith(b"x-pm-internal-id:") and not m.keep_internal:
                continue
            if low.startswith(b"x-pm-gluon-id:") and not m.keep_gluon:
                continue
            kept.append(line)
        fields = [b"X-Pm-Gluon-Id: " + m.gluon_id.encode(), *kept]
        fields.append(b"X-Pm-Internal-Id: " + m.proton_id.encode())
        if self.s.dup_internal_in_control and m.token and m.token.startswith("SYNTH-"):
            fields.append(b"X-Pm-Internal-Id: " + m.proton_id.encode())
        if m.salt:
            fields.append(f"X-Synth-Build: {m.salt}".encode())
        return b"\r\n".join(fields) + sep + body

    def _as_file(self, served: bytes, *, drift: bool | None = None) -> bytes:
        """isync's copy: LF endings and one X-TUID field (H3)."""
        text = served.replace(b"\r\n", b"\n")
        head, sep, body = text.partition(b"\n\n")
        lines = head.split(b"\n")
        lines.insert(1, b"X-TUID: synthetic")
        if self.s.file_drift if drift is None else drift:
            lines.append(b"X-Drift: 1")
        return b"\n".join(lines) + sep + body

    def _visible(self, m: _Msg) -> bool:
        return m.token is None or m.token not in self.s.never_deliver

    # ---- World -----------------------------------------------------

    def send(self, route: str, raw: bytes) -> str:
        self.sends += 1
        if self.s.send_result != "sent":
            return self.s.send_result
        self._add(raw, "INBOX")
        if self.sends <= self.s.ambiguous_sends:
            return "ambiguous"
        return "sent"

    def sync(self) -> str:
        self.syncs += 1
        for msg in self.msgs:
            msg.synced = True
        return "ok"

    def served(self, folder: str | None = None) -> list[Served]:
        out: list[Served] = []
        for m in self.msgs:
            if not self._visible(m) or (folder is not None and m.folder != folder):
                continue
            raw = self._served_raw(m)
            out.append(
                Served(
                    m.folder,
                    m.uid,
                    self.uidvalidity,
                    frozenset(m.flags),
                    raw,
                    self._as_file(raw) if m.synced else None,
                )
            )
        sends = self.sends
        if self.s.service_after_sends is not None and sends >= self.s.service_after_sends:
            out.append(self._injected(b"noreply@service.example", b"Welcome"))
        if self.s.foreign_after_sends is not None and sends >= self.s.foreign_after_sends:
            out.append(self._injected(b"someone@other.example", b"Hello"))
        if (
            self.s.foreign_party_after_sends is not None
            and sends >= self.s.foreign_party_after_sends
            and self.msgs
        ):
            out.append(self._injected(b"stranger@other.example", b"SYNTH-r1g1-1"))
        return out

    def _injected(self, sender: bytes, subject: bytes) -> Served:
        raw = (
            b"From: Synthetic <" + sender + b">\r\nTo: Recipient <recipient@mail.example>\r\n"
            b"Subject: "
            + subject
            + b"\r\nMessage-ID: <injected@"
            + sender.split(b"@")[1]
            + b">\r\n"
            b"Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n\r\ninjected\r\n"
        )
        return Served("INBOX", 9000, self.uidvalidity, frozenset(), raw, None)

    def route_preserves(self, route: str, raw: bytes) -> bool | None:
        return self.s.route_preserves.get(route)

    def fresh_files(self, folder: str) -> dict[str, bytes] | None:
        if not self.s.fresh_available:
            return None
        return {
            m.token: self._as_file(self._served_raw(m), drift=self.s.fresh_drift)
            for m in self.msgs
            if m.folder == folder and m.token and self._visible(m)
        }

    def mutate(self, op: str, token: str) -> str:
        self.mutate_log.append((op, token))
        msg = self._find(token)
        if op in self.s.proton_id_changes_on:
            msg.proton_id = self._new_ids()[0]
        if op == "move":
            msg.folder, msg.uid = "Folder", self._uid("Folder")
        elif op == "archive":
            msg.folder, msg.uid = "Archive", self._uid("Archive")
        elif op == "trash":
            msg.folder, msg.uid = "Trash", self._uid("Trash")
        elif op == "restore":
            msg.folder, msg.uid = "INBOX", self._uid("INBOX")
        elif op == "spam":
            msg.folder, msg.uid = "Spam", self._uid("Spam")
        elif op == "flag":
            msg.flags ^= {"\\Flagged"}
        elif op in ("label_add", "label_remove", "draft_edit"):
            pass
        elif op == "draft_replace":
            if self.s.draft_replace_changes_id:
                msg.proton_id = self._new_ids()[0]
        elif op == "draft_send":
            msg.folder, msg.uid = "Sent", self._uid("Sent")
            if not self.s.draft_send_keeps_id:
                msg.proton_id = self._new_ids()[0]
        else:
            raise WorldError("unknown_operation")
        return "done"

    def append(self, raw: bytes, folder: str) -> str:
        same = None
        if not self.s.append_distinct_ids:
            same = next((m for m in self.msgs if m.token == self._token(raw)), None)
        self._add(raw, folder, same_id_as=same)
        return "done"

    def h11(self, case: str, raw: bytes) -> str:
        outcome = self.s.h11[case]
        if outcome in ("refused", "recovered_unchanged"):
            # Recovery keeps the literal as submitted, forged fields and all.
            msg = self._add(raw, "Recovered Messages")
            msg.verbatim = True
        elif outcome == "rebuilt":
            self._add(raw, "INBOX")
        return outcome

    def header_pass(self, *, cold: bool) -> HeaderPass:
        proved = cold and self.s.cold_provable
        return HeaderPass(
            flags_set=self.s.header_read_sets_flags,
            membership_ok=self.s.membership_ok,
            connector_fetches=len(self.msgs) if proved else None,
            cold_proved=proved,
            bytes_transferred=20_518 if not cold else 52_457_470,
        )

    def race(self, repeats: int) -> RaceResult:
        outcomes = ["neither"] * min(self.s.race_neither, repeats)
        rest = repeats - len(outcomes)
        outcomes += ["both"] * (rest // 2) + ["old"] * (rest - rest // 2)
        return RaceResult(tuple(outcomes), self.s.race_id_changed)

    def operator(self, step: str) -> str:
        self.operator_log.append(step)
        if step in self.s.unavailable_steps:
            return "unavailable"
        effects = self.s.step_effects.get(step, set())
        for m in self.msgs:
            if "bytes" in effects:
                m.salt += 1
            if "gluon_id" in effects:
                m.gluon_id = self._new_ids()[1]
            if "uid" in effects:
                m.uid = self._uid(m.folder)
            if step in self.s.step_changes_proton_id:
                m.proton_id = self._new_ids()[0]
        if "uidvalidity" in effects:
            self.uidvalidity += 1
        return "done"

    def flag_scope(self) -> FlagScope:
        return FlagScope(self.s.flag_scope_ok, True)

    def isolation_ok(self) -> bool:
        self.isolation_calls += 1
        return self.s.isolation_fails_on_call != self.isolation_calls

    def warnings(self) -> list[str]:
        after = self.s.warning_after_sends
        return [self.s.warning_token] if after is not None and self.sends >= after else []

    def accounts(self) -> set[str]:
        accounts = {"A", "B", "C"}
        after = self.s.extra_account_after_sends
        if after is not None and self.sends >= after:
            accounts.add("X")
        return accounts

    def restart_needed(self) -> bool:
        after = self.s.restart_after_sends
        return after is not None and self.sends >= after

    def can_prove_cold(self) -> bool:
        return self.s.cold_provable
