"""The driver's view of the world (procedure v5 section 4).

The driver composes the groups G1 to G8 and applies the frozen predictions
and the stop rules; everything that touches an account goes through a
``World``. Two exist: ``SimulatedWorld`` (in memory, deterministic, used by
``--dry-run`` and the tests) and ``PiecesWorld`` (the real sender, pinned-TLS
capture and supervised sync of #1759; the steps only the owner or the guest
can do go through an operator port). Neither carries a value out: a world
returns fixed tokens, booleans, counts and the served bytes, which the
driver reads in memory and never writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

ROUTES = ("B", "C", "A2")
SEND_TOKENS = frozenset(
    {"sent", "refused", "ambiguous", "tls_failed", "auth_failed", "connection_failed"}
)


class WorldError(Exception):
    """A world could not do what was asked. ``token`` is a fixed reason."""

    def __init__(self, token: str) -> None:
        super().__init__(token)
        self.token = token


@dataclass(frozen=True)
class Served:
    """One message as Bridge served it (``raw``) and as isync wrote it
    (``file_bytes``, ``None`` until a sync has written it)."""

    folder: str
    uid: int
    uidvalidity: int
    flags: frozenset[str]
    raw: bytes
    file_bytes: bytes | None


@dataclass(frozen=True)
class HeaderPass:
    flags_set: int  # messages whose flags a header-only read changed
    membership_ok: bool  # each message once per label folder, under one ID
    connector_fetches: int | None  # counted only for a pass proved cold
    cold_proved: bool
    bytes_transferred: int


@dataclass(frozen=True)
class RaceResult:
    outcomes: tuple[str, ...]  # per message: old | new | both | neither
    id_changed: bool


@dataclass(frozen=True)
class FlagScope:
    mailbox_local_deleted_ok: bool  # \Deleted stays in the mailbox that set it (S11, H8)
    shared_flags_ok: bool  # other flags are shared across the message's mailboxes


class World(Protocol):
    def send(self, route: str, raw: bytes) -> str: ...

    def sync(self) -> str: ...

    def served(self, folder: str | None = None) -> list[Served]: ...

    def route_preserves(self, route: str, raw: bytes) -> bool | None: ...

    def fresh_files(self, folder: str) -> dict[str, bytes] | None:
        """A fresh download of ``folder`` (isync into a new, empty Maildir),
        the files by correlation token; ``None`` when it cannot be made."""
        ...

    def mutate(self, op: str, token: str) -> str: ...

    def append(self, raw: bytes, folder: str) -> str: ...

    def h11(self, case: str, raw: bytes) -> str: ...

    def header_pass(self, *, cold: bool) -> HeaderPass: ...

    def race(self, repeats: int) -> RaceResult: ...

    def operator(self, step: str) -> str: ...

    def flag_scope(self) -> FlagScope: ...

    def isolation_ok(self) -> bool: ...

    def warnings(self) -> list[str]: ...

    def accounts(self) -> set[str]: ...

    def restart_needed(self) -> bool: ...

    def can_prove_cold(self) -> bool: ...
