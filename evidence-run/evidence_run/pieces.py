"""The driver's world made of the real pieces of #1759 (procedure v5 4).

Sending is the smtplib sender, reading is the pinned-TLS capture joined
with the files isync wrote (read only, never written), syncing is the
supervised isync, isolation is the matrix over the configured targets. The
steps only the owner or the guest can perform (moves, labels, drafts,
APPEND, cache clears, re-add) and the reads that need Bridge's own
instrumentation go to an *operator world*; without one they are refused
with ``operator_unavailable``, so a run that needs them stops with a fixed
token and never improvises.

A password never enters the config: it holds paths of mode-600 files.
Provider warnings (rate limit, abuse, terms) are seen by the owner, not by
this code: the operator world reports them, and without one this world
reports none (documented in the README, not assumed).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import correlation, isolation, sender, syncdriver, tlscapture
from .world import FlagScope, HeaderPass, RaceResult, Served, World, WorldError


@dataclass(frozen=True)
class SmtpRoute:
    host: str
    port: int
    mail_from: str
    rcpt_to: str
    user: str
    password_file: Path
    cafile: Path | None = None


@dataclass(frozen=True)
class ImapEndpoint:
    host: str
    port: int
    fingerprint: str
    user: str
    password_file: Path


@dataclass(frozen=True)
class PiecesConfig:
    smtp: Mapping[str, SmtpRoute]  # B, C, A2, and C_check (the second address)
    imap: ImapEndpoint  # A's Bridge
    check_imap: ImapEndpoint | None  # C's second address (the route check's read)
    maildir: Path
    mbsync: str
    sync_config: Path
    channel: str
    isolation_targets: Path | None = None
    fresh_sync_config: Path | None = None  # Reserved; independent live download awaits #1783.
    fresh_maildir: Path | None = None
    accounts: frozenset[str] = frozenset({"A", "B", "C"})
    check_polls: int = 12
    poll_seconds: float = 5.0


def _token(raw: bytes) -> str | None:
    return correlation.summary(raw).token


class PiecesWorld:
    def __init__(
        self,
        config: PiecesConfig,
        *,
        operator: World | None = None,
        isolation_check: Callable[[], bool] | None = None,
        isolation_prober: isolation.Prober = isolation.probe_target,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._operator = operator
        self._isolation_check = isolation_check
        self._prober = isolation_prober
        self._sleep = sleep
        self._syncer = syncdriver.SupervisedSync(
            config.sync_config, config.channel, mbsync=config.mbsync
        )

    def _need_operator(self) -> World:
        if self._operator is None:
            raise WorldError("operator_unavailable")
        return self._operator

    # ---- what the pieces do ----------------------------------------

    def send(self, route: str, raw: bytes) -> str:
        spec = self.config.smtp.get(route)
        if spec is None:
            raise WorldError("route_not_configured")
        try:
            return sender.send(
                raw,
                host=spec.host,
                port=spec.port,
                mail_from=spec.mail_from,
                rcpt_to=spec.rcpt_to,
                user=spec.user,
                password_file=spec.password_file,
                cafile=spec.cafile,
            ).token
        except sender.SendError as exc:
            raise WorldError(exc.token) from None

    def sync(self) -> str:
        return self._syncer.sync_once().token

    def _files(self, folder: str, root: Path | None = None) -> dict[str, list[bytes]]:
        """What isync wrote for ``folder``, by correlation token; read only."""
        out: dict[str, list[bytes]] = {}
        try:
            for sub in ("cur", "new"):
                directory = (root or self.config.maildir) / folder / sub
                if not directory.is_dir():
                    continue
                for path in sorted(directory.iterdir()):
                    if path.is_symlink() or not path.is_file():
                        continue
                    data = path.read_bytes()
                    token = _token(data)
                    if token is not None:
                        out.setdefault(token, []).append(data)
        except OSError:
            raise WorldError("capture_failed") from None
        return out

    def served(self, folder: str | None = None) -> list[Served]:
        folders = [folder] if folder is not None else ["INBOX"]
        out: list[Served] = []
        for name in folders:
            try:
                state = tlscapture.capture_state(
                    self.config.imap.host,
                    self.config.imap.port,
                    expected_fingerprint=self.config.imap.fingerprint,
                    user=self.config.imap.user,
                    password_file=self.config.imap.password_file,
                    folder=name,
                )
            except (tlscapture.CaptureError, OSError) as exc:
                raise WorldError(getattr(exc, "token", "capture_failed")) from None
            if state.uidvalidity is None:
                raise WorldError("capture_failed")
            files = self._files(name)
            for uid in sorted(state.messages):
                raw = state.messages[uid]
                token = _token(raw)
                pool = files.get(token or "", [])
                out.append(
                    Served(
                        name,
                        uid,
                        state.uidvalidity,
                        state.flags.get(uid, frozenset()),
                        raw,
                        pool.pop(0) if pool else None,
                    )
                )
        return out

    def fresh_files(self, folder: str) -> dict[str, bytes] | None:
        """Require an operator-owned independent download for each comparison.

        Reusing configured paths cannot prove empty Maildir and sync state;
        until the live interface exists (#1783), that evidence is unavailable.
        """
        return self._operator.fresh_files(folder) if self._operator else None

    def route_preserves(self, route: str, raw: bytes) -> bool | None:
        """The C route: send the same forged message to C's second address
        and read its headers there. Other routes need the operator."""
        check = self.config.smtp.get("C_check")
        if route != "C" or check is None or self.config.check_imap is None:
            return self._operator.route_preserves(route, raw) if self._operator else None
        spec = self.config.smtp["C_check"]
        try:
            sent = sender.send(
                raw,
                host=spec.host,
                port=spec.port,
                mail_from=spec.mail_from,
                rcpt_to=spec.rcpt_to,
                user=spec.user,
                password_file=spec.password_file,
                cafile=spec.cafile,
            )
        except sender.SendError as exc:
            raise WorldError(exc.token) from None
        if sent.token == "refused":
            raise WorldError("provider_warning")
        if sent.token != "sent":
            return None
        wanted = _token(raw)
        endpoint = self.config.check_imap
        for _ in range(self.config.check_polls):
            try:
                messages = tlscapture.capture(
                    endpoint.host,
                    endpoint.port,
                    expected_fingerprint=endpoint.fingerprint,
                    user=endpoint.user,
                    password_file=endpoint.password_file,
                ).values()
            except tlscapture.CaptureError, OSError:
                return None
            for message in messages:
                if _token(message) == wanted:
                    headers = correlation.read_headers(message)
                    if headers is None:
                        return None
                    return all(
                        any(
                            str(value).strip().startswith(prefix)
                            for value in headers.get_all(name, [])
                        )
                        for name, prefix in (
                            ("X-Pm-Internal-Id", "SYNTHFORGED-"),
                            ("X-Pm-Gluon-Id", "SYNTHFORGEDGLUON-"),
                        )
                    )
            self._sleep(self.config.poll_seconds)
        return None

    def isolation_ok(self) -> bool:
        if self._isolation_check is not None:
            return self._isolation_check()
        if self.config.isolation_targets is None:
            raise WorldError("isolation_not_configured")
        try:
            targets = isolation.load_targets(str(self.config.isolation_targets))
            return isolation.evaluate(targets, self._prober, timeout=3.0).verdict == "pass"
        except OSError, ValueError, TypeError, AttributeError:
            raise WorldError("isolation_not_configured") from None

    def accounts(self) -> set[str]:
        return set(self.config.accounts)

    # ---- what needs the operator (or the guest) --------------------

    def mutate(self, op: str, token: str) -> str:
        return self._need_operator().mutate(op, token)

    def append(self, raw: bytes, folder: str) -> str:
        return self._need_operator().append(raw, folder)

    def h11(self, case: str, raw: bytes) -> str:
        return self._need_operator().h11(case, raw)

    def header_pass(self, *, cold: bool) -> HeaderPass:
        return self._need_operator().header_pass(cold=cold)

    def race(self, repeats: int) -> RaceResult:
        return self._need_operator().race(repeats)

    def operator(self, step: str) -> str:
        return self._need_operator().operator(step)

    def flag_scope(self) -> FlagScope:
        return self._need_operator().flag_scope()

    def warnings(self) -> list[str]:
        return self._operator.warnings() if self._operator else []

    def restart_needed(self) -> bool:
        return self._operator.restart_needed() if self._operator else False

    def can_prove_cold(self) -> bool:
        return self._operator.can_prove_cold() if self._operator else False
