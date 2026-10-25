"""The evidence-run driver (procedure v5 section 4, issue #1767).

It composes the pieces of #1759 into the groups G1 to G8, in order, in one
process, from the frozen inputs (``frozen/``): the predictions, the fixture
plan, the service-message list and the group clocks. Every cross-step
comparison lives in this process's memory (the probe's keyed digests, the
snapshots, the continuity log) and nothing is written to disk except the
allowlisted export at the end; if the process cannot span the run it stops
and asks, it never persists.

The run stops at the first failed prediction, unexplained message, foreign
party, provider warning, failed isolation test, exhausted clock or budget,
output that would carry an identifier, or lost probe state. Each is a
fixed token in the export's ``stop`` section; nothing else is recorded
about it. A group's accounts are touched only through a ``World``.

Live groups run only with the owner's approval token for exactly these
frozen inputs and a passing account-free stage report (``frozen``).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from . import export, fixtures, frozen, p11, runner
from .probe import Probe, ProbeError
from .runner import Limits, MessageSummary, Refused, StopRun
from .world import Served, World, WorldError

GROUPS = frozen.GROUPS
POLL_SECONDS = 5.0
STOP_TOKENS = frozenset(
    {
        "prediction_failed",
        "unexplained_message",
        "foreign_party",
        "provider_warning",
        "isolation_failed",
        "clock_exhausted",
        "attempt_budget_exhausted",
        "unique_bytes_exhausted",
        "retry_limit",
        "convergence_timeout",
        "identifier_in_output",
        "probe_state_lost",
        "state_not_spannable",
        "operator_declined",
        "probe_failed",
        "world_error",
    }
)
_FOLDERS = ("INBOX", "Folder", "Archive", "Trash", "Drafts", "Sent", "Spam")
_ROUTE_BOOLEAN = {"B": "route_b_preserved", "C": "route_c_preserved", "A2": "route_a2_preserved"}
_SUBJECT = re.compile(rb"(?im)^Subject: (SYNTH-[A-Za-z0-9]{1,16}-\d+)")
_FROM_DOMAIN = re.compile(rb"(?im)^From:[^\r\n]*@([A-Za-z0-9.-]+)")
_ORDER = {"observed": 0, "unobservable": 1, "failed": 2}


@dataclass(frozen=True)
class Shape:
    internal: int
    gluon: int
    gluon_topmost: bool
    forged_internal: int
    forged_gluon: int


def header_shape(raw: bytes) -> Shape:
    """The identity fields of a message's top-level header block."""
    head = raw.split(b"\r\n\r\n", 1)[0].replace(b"\r\n", b"\n")
    names: list[bytes] = []
    values: list[bytes] = []
    for line in head.split(b"\n"):
        if line[:1] in (b" ", b"\t"):
            continue  # a folded continuation
        name, _, value = line.partition(b":")
        names.append(name.strip().lower())
        values.append(value.strip())
    internal = [v for n, v in zip(names, values, strict=True) if n == b"x-pm-internal-id"]
    gluon = [v for n, v in zip(names, values, strict=True) if n == b"x-pm-gluon-id"]
    return Shape(
        len(internal),
        len(gluon),
        bool(names) and names[0] == b"x-pm-gluon-id",
        sum(1 for v in internal if v.startswith(b"SYNTHFORGED-")),
        sum(1 for v in gluon if v.startswith(b"SYNTHFORGEDGLUON-")),
    )


def _field_value(raw: bytes, name: bytes) -> bytes | None:
    for line in raw.split(b"\r\n\r\n", 1)[0].replace(b"\r\n", b"\n").split(b"\n"):
        key, _, value = line.partition(b":")
        if key.strip().lower() == name:
            return value.strip()
    return None


@dataclass(frozen=True)
class Snap:
    probe_key: str
    gluon: bytes | None
    uid: int
    uidvalidity: int
    bytes_digest: bytes
    folder: str


@dataclass(frozen=True)
class RunResult:
    export: dict
    stop: tuple[str, str] | None
    states: dict[str, str]


def _domain_rule(domain: str) -> Callable[[MessageSummary], bool]:
    return lambda summary: summary.sender_domain == domain


class VirtualClock:
    """Time that only moves when something sleeps: the simulated world has
    no real waits, so a dry-run does not spend minutes pacing itself."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Driver:
    def __init__(
        self,
        world: World,
        inputs: frozen.FrozenInputs,
        *,
        run_id: str = "r1",
        limits: Limits | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        account_stage: bool = True,
        isolation_checked: bool = False,
        tls_checked: bool = False,
    ) -> None:
        if account_stage:
            inputs.require_service_frozen()  # refused before anything runs
        self.world, self.inputs, self.run_id = world, inputs, run_id
        self.limits = limits or Limits()
        self._clock, self._sleep = clock, sleep
        self.budget = runner.Budget(self.limits, clock=clock, sleep=sleep)
        self.state = runner.Run()
        for rule in inputs.service_rules:
            self.state.service_rules.add(
                str(rule.get("name", "rule")), _domain_rule(str(rule.get("sender_domain", "")))
            )
        self.state.service_rules.freeze()
        self.probe = Probe()
        self._probe0 = self.probe
        self._key = secrets.token_bytes(32)
        self._clocks = inputs.group_clocks(self.limits)
        self._allowed = {d.lower() for d in inputs.fixtures["allowed_domains"]}
        self._group = "start"
        self._group_clock: runner.GroupClock | None = None
        self._timings: dict[str, float] = {}
        self._marks: dict[str, str] = {p: "predicted" for p in inputs.predictions}
        self._contrib: dict[str, list[str]] = {}
        self._booleans: dict[str, bool] = {}
        self._counts: Counter[str] = Counter()
        self._service_seen: set[bytes] = set()
        self._ordinary: Counter[str] = Counter()
        self._recovered: Counter[str] = Counter()
        self.snapshots: list[str] = []  # the labels of snapshots taken, in order
        self._continuity: dict[str, list[tuple[str, int, int]]] = {}
        self._isolation_checked, self._tls_checked = isolation_checked, tls_checked

    # ---- plumbing --------------------------------------------------

    def _gid(self, group: str) -> str:
        return f"{self.run_id}{group}"

    def _token_of(self, raw: bytes) -> str | None:
        found = _SUBJECT.search(raw)
        return found.group(1).decode() if found else None

    def _digest(self, value: bytes | None) -> bytes | None:
        return None if value is None else hmac.new(self._key, value, hashlib.sha256).digest()

    def _checkpoint(self) -> None:
        """Run after every world operation: the clock, the probe's state,
        warnings, accounts and the restart signal."""
        if self._group_clock is not None:
            self._group_clock.check()
        if self.probe is not self._probe0:
            raise StopRun("probe_state_lost")
        if self.world.restart_needed():
            raise StopRun("state_not_spannable")
        if self.world.warnings():
            raise StopRun("provider_warning")
        if self.world.accounts() - {"A", "B", "C"}:
            raise StopRun("foreign_party")

    def _register(self, raw: bytes) -> str:
        token = self._token_of(raw)
        assert token is not None
        self.state.correlation.register(token)
        return token

    def _mark(self, prediction: str, state: str) -> None:
        """Merge a check into a prediction: failed over unobservable over
        observed; the first failed stops the run."""
        self._contrib.setdefault(prediction, []).append(state)
        worst = max(self._contrib[prediction], key=_ORDER.__getitem__)
        self._marks[prediction] = worst
        if state == "failed":
            self.state.record(prediction, "failed")  # raises StopRun

    def _send(self, route: str, raw: bytes) -> None:
        """One submission, counted, with the procedure's retry rule."""
        token = self._register(raw)
        op = self.budget.operation(token)
        op.attempt()
        while True:
            result = self.world.send(route, raw)
            self._checkpoint()
            if result == "sent":
                return
            if result == "ambiguous":
                self.budget.note_ambiguous()  # counted, never silently resent
                return
            if result == "connection_failed":
                op.retry()  # at most twice, then StopRun("retry_limit")
                continue
            if result == "refused":
                raise StopRun("provider_warning")
            raise StopRun("world_error")

    def _append(self, raw: bytes, folder: str) -> None:
        self._register(raw)
        self.budget.attempt()
        self.world.append(raw, folder)
        self._checkpoint()

    def _sync(self) -> None:
        self.budget.command()
        if self.world.sync() != "ok":
            raise StopRun("world_error")
        self._checkpoint()

    def _read(self, folder: str | None) -> list[Served]:
        """Served messages, each classified: a test message, an expected
        service message, or a stop."""
        out = self.world.served(folder)
        self.budget.pace()
        for item in out:
            token = self._token_of(item.raw)
            found = _FROM_DOMAIN.search(item.raw)
            domain = found.group(1).decode().lower() if found else None
            kind = self.state.classify(MessageSummary(token, domain))
            if kind == "test" and domain not in self._allowed:
                raise StopRun("foreign_party")
            if kind == "service":
                self._service_seen.add(hashlib.sha256(item.raw).digest()[:8])
        self._checkpoint()
        return out

    def _read_all(self) -> list[Served]:
        out: list[Served] = []
        for folder in _FOLDERS:
            out += self._read(folder)
        return out

    def _converge(
        self, tokens: set[str], folder: str | None, predictions: tuple[str, ...]
    ) -> list[Served]:
        """Sync and read until every token is served, up to the convergence
        deadline; a token that never arrives is unobservable and stops."""
        deadline = self._clock() + self.limits.convergence_seconds
        while True:
            self._sync()
            served = self._read(folder) if folder else self._read_all()
            present = {t for s in served if (t := self._token_of(s.raw))}
            if tokens <= present:
                return served
            if self._clock() >= deadline:
                for p in predictions:
                    self._contrib.setdefault(p, []).append("unobservable")
                    self._marks[p] = "unobservable"
                raise StopRun("convergence_timeout")
            self._sleep(POLL_SECONDS)
            self._checkpoint()

    def _observe(self, item: Served, label: str) -> str | None:
        """Observe one served message under ``token|label``; returns the
        probe key, or None for a message without a token."""
        token = self._token_of(item.raw)
        if token is None:
            return None
        key = f"{token}|{label}"
        seen = self.probe.observe(item.raw, item.folder, token=key)
        bucket = self._recovered if item.folder == "Recovered Messages" else self._ordinary
        if seen.reason:
            bucket[seen.reason] += 1
        self._continuity.setdefault(token, []).append((item.folder, item.uid, item.uidvalidity))
        return key

    def _snapshot(self, label: str, tokens: Sequence[str], folders: Sequence[str]) -> dict:
        """Observe each tracked token's current instance, in memory."""
        self.snapshots.append(label)
        self._sync()
        served: list[Served] = []
        for folder in folders:
            served += self._read(folder)
        snaps: dict[str, Snap] = {}
        for item in served:
            token = self._token_of(item.raw)
            if token is not None and token in tokens:
                key = self._observe(item, label)
                assert key is not None
                snaps[token] = Snap(
                    key,
                    self._digest(_field_value(item.raw, b"x-pm-gluon-id")),
                    item.uid,
                    item.uidvalidity,
                    hashlib.sha256(item.raw).digest(),
                    item.folder,
                )
        return snaps

    def _p11(self, folders: Sequence[str]) -> str:
        """P11 after a group: the retained incremental files against the
        contemporaneous served bytes, and against a fresh download (a new
        isync run into an empty Maildir, one supervised command)."""
        verdicts: list[str] = []
        for folder in folders:
            everything = self._read(folder)
            # A token that names several messages (G5's identical appends)
            # cannot pair a file with its message: those are left out.
            seen = Counter(self._token_of(s.raw) for s in everything)
            served = [
                s
                for s in everything
                if self._token_of(s.raw) is not None and seen[self._token_of(s.raw)] == 1
            ]
            if not served:
                continue  # nothing in this folder to compare
            retained = [p11.compare(s.file_bytes, s.raw).verdict for s in served if s.file_bytes]
            verdicts.append(self._all_equal(retained))
            if len(retained) != len(served):
                verdicts.append("unobservable")
            self.budget.command()
            fresh = self.world.fresh_files(folder)
            self._checkpoint()
            if fresh is None:
                verdicts.append("unobservable")
                continue
            compared = [
                p11.compare(fresh[t], s.raw).verdict
                for s in served
                if (t := self._token_of(s.raw)) is not None and t in fresh
            ]
            verdicts.append(self._all_equal(compared))
            if len(compared) != len(served):
                verdicts.append("unobservable")
        if "failed" in verdicts:
            return "failed"
        return "unobservable" if not verdicts or "unobservable" in verdicts else "observed"

    @staticmethod
    def _all_equal(verdicts: Sequence[str]) -> str:
        return "observed" if all(v == "equal" for v in verdicts) else "failed"

    # ---- groups ----------------------------------------------------

    def _plan(self, group: str) -> dict:
        return self.inputs.fixtures["groups"][group]

    def _g1(self) -> None:
        plan, run = self._plan("g1"), self._gid("g1")
        first = [
            fixtures.control_message(run, n, size=plan["size"])
            for n in range(1, plan["control"] + 1)
        ]
        for raw in first:
            self._send("B", raw)
        tokens = {self._token_of(r) for r in first}
        served = self._converge({t for t in tokens if t}, "INBOX", ("P1",))
        more = [
            fixtures.control_message(run, n, size=plan["size"])
            for n in range(plan["control"] + 1, plan["control"] + 1 + plan["incremental"])
        ]
        for raw in more:
            self._send("B", raw)
        wanted = {t for t in (self._token_of(r) for r in first + more) if t}
        served = self._converge(wanted, "INBOX", ("P1",))
        ok = True
        for item in served:
            if self._token_of(item.raw) not in wanted:
                continue
            self._observe(item, "g1")
            shape = header_shape(item.raw)
            ok &= (shape.internal, shape.gluon, shape.gluon_topmost) == (1, 1, True)
            if shape.internal > 1:
                self._counts["duplicate_internal_fields_seen"] += 1
        self._mark("P1", "observed" if ok else "failed")
        self._check_p12_ordinary()

    def _check_p12_ordinary(self) -> None:
        bad = set(self._ordinary) - {"no_acceptance_rule"}
        self._mark("P12", "failed" if bad else "observed")

    def _g2(self) -> None:
        plan, run = self._plan("g2"), self._gid("g2")
        survived_internal = survived_gluon = False
        preserved_routes: list[str] = []
        for index, route in enumerate(plan["routes"]):
            check = fixtures.forged_message(run, 900 + index, plan["kinds"][0], size=plan["size"])
            self.budget.attempt()
            result = self.world.route_preserves(route, check)
            self._checkpoint()
            if result is not None:
                self._booleans[_ROUTE_BOOLEAN[route]] = result
            if result:
                preserved_routes.append(route)
        sent: list[str] = []
        for index, route in enumerate(plan["routes"]):
            if route not in preserved_routes:
                continue  # a route that does not carry the fields tests nothing
            for i in range(plan["per_route"]):
                n = index * plan["per_route"] + i + 1
                kind = plan["kinds"][i % len(plan["kinds"])]
                raw = fixtures.forged_message(run, n, kind, size=plan["size"])
                self._send(route, raw)
                token = self._token_of(raw)
                assert token
                sent.append(token)
        if not sent:
            self._mark("P2", "unobservable")
            return
        for item in self._converge(set(sent), "INBOX", ("P2",)):
            if self._token_of(item.raw) in sent:
                self._observe(item, "g2")
                shape = header_shape(item.raw)
                survived_internal |= shape.forged_internal > 0
                survived_gluon |= shape.forged_gluon > 0
                if shape.internal > 1:
                    self._counts["duplicate_internal_fields_seen"] += 1
        self._booleans["p2_internal_survived"] = survived_internal
        self._booleans["p2_gluon_survived"] = survived_gluon
        self._mark("P2", "failed" if survived_internal or survived_gluon else "observed")

    def _g3(self) -> None:
        plan, run = self._plan("g3"), self._gid("g3")
        raws = [
            fixtures.control_message(run, n, size=plan["size"], multipart=n <= plan["multipart"])
            for n in range(1, plan["messages"] + 1)
        ]
        for raw in raws:
            self._send("B", raw)
        tokens = [t for t in (self._token_of(r) for r in raws) if t]
        self._converge(set(tokens), "INBOX", ("P3",))
        prev = self._snapshot("g3.0", tokens, ("INBOX",))
        unknown = False
        for step, op in enumerate(plan["ops"], start=1):
            for token in tokens:
                self.world.mutate(op, token)
                self.budget.pace()
                self._checkpoint()
            now = self._snapshot(f"g3.{op}", tokens, _FOLDERS)
            for token in tokens:
                if token in prev and token in now:
                    equal = self.probe.equal(prev[token].probe_key, now[token].probe_key)
                    if equal is False:
                        self._mark("P3", "failed")
                    unknown |= equal is None
                else:
                    unknown = True
            prev = now
        self._mark("P3", "unobservable" if unknown else "observed")
        self._mark("P11", self._p11(("INBOX", "Archive")))

    def _g4(self) -> None:
        plan, run = self._plan("g4"), self._gid("g4")
        replaced = kept = True
        unknown = False
        for n in range(1, plan["drafts"] + 1):
            raw = fixtures.control_message(run, n, size=plan["size"])
            self._append(raw, "Drafts")
            token = self._token_of(raw)
            assert token
            keys: list[str | None] = []
            for op in (None, "draft_edit", "draft_replace", "draft_send"):
                if op:
                    self.world.mutate(op, token)
                    self.budget.pace()
                    self._checkpoint()
                self._sync()
                items = [
                    i for f in _FOLDERS for i in self._read(f) if self._token_of(i.raw) == token
                ]
                keys.append(self._observe(items[-1], f"g4.{n}.{op}") if items else None)
                if len(keys) >= 3 and keys[-2] is not None and keys[-1] is not None:
                    equal = self.probe.equal(keys[-2], keys[-1])
                    if (op == "draft_replace" and equal is True) or (
                        op == "draft_send" and equal is False
                    ):
                        self._mark("P4", "failed")
            if None in keys:
                unknown = True
                continue
            at_replace = self.probe.equal(keys[1] or "", keys[2] or "")
            after_send = self.probe.equal(keys[2] or "", keys[3] or "")
            unknown |= at_replace is None or after_send is None
            replaced &= at_replace is False or at_replace is None
            kept &= after_send is True or after_send is None
        ok = replaced and kept
        self._mark("P4", "failed" if not ok else "unobservable" if unknown else "observed")

    def _g5(self) -> None:
        plan, run = self._plan("g5"), self._gid("g5")
        same = fixtures.control_message(run, 1, size=plan["size"])
        for _ in range(plan["identical_appends"]):
            self._append(same, "INBOX")
        token = self._token_of(same)
        self._sync()
        copies = [i for i in self._read("INBOX") if self._token_of(i.raw) == token]
        keys = [self._observe(c, f"g5.{k}") for k, c in enumerate(copies)]
        distinct = len(keys) == plan["identical_appends"] and all(
            self.probe.equal(keys[a] or "", keys[b] or "") is False
            for a in range(len(keys))
            for b in range(a + 1, len(keys))
        )
        self._mark("P5", "observed" if distinct else "failed")
        for number, case in enumerate(plan["h11"], start=100):
            raw = fixtures.forged_message(run, number, "forged", size=plan["size"])
            self._register(raw)
            self.budget.attempt()
            outcome = self.world.h11(case, raw)
            self._checkpoint()
            expected = self.inputs.h11_expect[case]
            if outcome == "no_trigger":
                self._counts["h11_unresolved"] += 1
            elif outcome == expected:
                self._counts["h11_matched"] += 1
            else:
                self._counts["h11_mismatched"] += 1
                self._mark("P5", "failed")
        self._sync()
        recovered = [self._observe(i, "g5.rec") for i in self._read("Recovered Messages")]
        self._mark("P5", "observed" if distinct else "failed")
        if not [r for r in recovered if r]:
            self._mark("P12", "unobservable")
        else:
            bad = set(self._recovered) - {"recovered_folder"}
            self._mark("P12", "failed" if bad else "observed")

    def _g6(self) -> None:
        plan, run = self._plan("g6"), self._gid("g6")
        tokens: set[str] = set()
        for index, raw in enumerate(
            fixtures.g6_messages(run, plan["messages"], plan["message_bytes"]), start=1
        ):
            self.budget.add_unique(len(raw))
            self._append(raw, "G6")
            token = self._token_of(raw)
            assert token
            tokens.add(token)
            self.budget.add_transfer(len(raw))
            for _ in range(plan["labels"]):
                self.world.mutate("label_add", token)
                self.budget.pace()
            self._checkpoint()
        self._converge(tokens, "G6", ("P9", "P10"))
        warm = self.world.header_pass(cold=False)
        ok = warm.flags_set == 0 and warm.membership_ok
        self.budget.add_transfer(warm.bytes_transferred)
        proved = False
        if self.world.can_prove_cold():
            cold = self.world.header_pass(cold=True)
            proved = cold.cold_proved
            ok &= cold.flags_set == 0 and cold.membership_ok
            self.budget.add_transfer(cold.bytes_transferred)
            if proved and cold.connector_fetches is not None:
                self._counts["header_pass_connector_fetches"] = cold.connector_fetches
        self._booleans["cold_pass_proved"] = proved
        self._mark("P9", "observed" if ok else "failed")
        race = self.world.race(plan["race_repeats"])
        self._checkpoint()
        outcomes = Counter(race.outcomes)
        self._counts["race_neither"] = outcomes["neither"]
        self._counts["race_both"] = outcomes["both"]
        self._counts["race_one"] = outcomes["old"] + outcomes["new"]
        self._mark("P10", "failed" if outcomes["neither"] or race.id_changed else "observed")

    def _g7(self) -> None:
        plan, run = self._plan("g7"), self._gid("g7")
        raws = [
            fixtures.control_message(
                run, n, size=plan["size"], multipart=n <= plan["tracked_multipart"]
            )
            for n in range(1, plan["tracked_multipart"] + plan["tracked_plain"] + 1)
        ]
        for raw in raws:
            self._send("B", raw)
        tokens = [t for t in (self._token_of(r) for r in raws) if t]
        self._converge(set(tokens), "INBOX", ("P6",))
        for step in self.inputs.g7_steps:
            # The baseline is the state the previous step left, captured
            # immediately before this step, in memory only.
            base = self._snapshot(f"g7.{step['id']}.base", tokens, ("INBOX",))
            result = self.world.operator(step["id"])
            self._checkpoint()
            if result == "declined":
                raise StopRun("operator_declined")
            if result == "unavailable":
                self._counts["steps_unobservable"] += 1
                self._mark(step["prediction"], "unobservable")
                continue
            after = self._snapshot(f"g7.{step['id']}.after", tokens, ("INBOX",))
            verdict = self._compare(step["expect"], base, after, tokens)
            if verdict == "observed":
                self._counts["steps_observed"] += 1
            elif verdict == "unobservable":
                self._counts["steps_unobservable"] += 1
            self._mark(step["prediction"], verdict)
        self._sync()
        self._mark("P11", self._p11(("INBOX",)))

    def _compare(
        self, expect: Mapping[str, str], base: dict, after: dict, tokens: Sequence[str]
    ) -> str:
        unknown = False
        for token in tokens:
            before, now = base.get(token), after.get(token)
            if before is None or now is None:
                unknown = True
                continue
            observed = {
                "proton_id": self.probe.equal(before.probe_key, now.probe_key),
                "gluon_id": None
                if before.gluon is None or now.gluon is None
                else hmac.compare_digest(before.gluon, now.gluon),
                "uid": before.uid == now.uid,
                "uidvalidity": before.uidvalidity == now.uidvalidity,
                "bytes": before.bytes_digest == now.bytes_digest,
            }
            for dimension, want in expect.items():
                if want == "any":
                    continue
                same = observed[dimension]
                if same is None:
                    unknown = True
                elif (want == "same") != same:
                    return "failed"
        return "unobservable" if unknown else "observed"

    def _g8(self) -> None:
        plan = self._plan("g8")
        scope = self.world.flag_scope()
        self._booleans["flag_scope_ok"] = scope.mailbox_local_deleted_ok and scope.shared_flags_ok
        ok = bool(self._booleans["flag_scope_ok"])
        self._mark("P3", "observed" if ok else "failed")
        if plan.get("spam_probe"):
            run = self._gid("g8")
            raws = [fixtures.control_message(run, n, size=2048) for n in (1, 2)]
            for raw in raws:
                self._send("B", raw)
            tokens = [t for t in (self._token_of(r) for r in raws) if t]
            self._converge(set(tokens), "INBOX", ("P3",))
            before = self._snapshot("g8.before", tokens, ("INBOX",))
            for token in tokens:
                self.world.mutate("spam", token)
                self._checkpoint()
            after = self._snapshot("g8.after", tokens, ("Spam",))
            self._mark("P3", self._compare({"proton_id": "same"}, before, after, tokens))
            spam = self._read("Spam")
            places = [(s.folder, s.uid, s.uidvalidity) for s in spam]
            ok &= len(places) == len(set(places))  # a UID is never handed out twice
        # Continuity over G3 to G7: within one mailbox and UIDVALIDITY a UID
        # names one message.
        owners: dict[tuple[str, int, int], str] = {}
        for token, seen in self._continuity.items():
            for place in seen:
                if owners.setdefault(place, token) != token:
                    ok = False
        self._mark("P3", "observed" if ok else "failed")

    # ---- run -------------------------------------------------------

    def _begin(self, group: str) -> None:
        self._group = group
        if not self.world.isolation_ok():
            raise StopRun("isolation_failed")
        self._isolation_checked = True
        self._group_clock = runner.GroupClock(group, self._clocks[group], self._clock)
        self._checkpoint()

    def run(self, groups: Sequence[str] = GROUPS) -> RunResult:
        if list(groups) != sorted(groups, key=GROUPS.index):
            raise runner.ConfigError("groups run in order G1 to G8")
        handlers = {name: getattr(self, f"_{name}") for name in GROUPS}
        stop: tuple[str, str] | None = None
        try:
            for name in groups:
                started = self._clock()
                self._begin(name)
                handlers[name]()
                self._timings[f"{name}_seconds"] = max(self._clock() - started, 0.0)
        except StopRun as exc:
            stop = (exc.token, self._group)
        except WorldError:
            stop = ("world_error", self._group)
        except ProbeError:
            stop = ("probe_failed", self._group)
        for name, state in self._marks.items():
            self.state.states[name] = state
        return self._finish(stop)

    def _extra_counts(self) -> dict[str, int]:
        return dict(self._counts)

    def _finish(self, stop: tuple[str, str] | None) -> RunResult:
        states = dict(self._marks)
        try:
            data = export.build_export(
                reasons=self.probe.reason_counts(),
                outcomes=self.probe.outcome_counts(),
                parser=self.probe.parser_counts(),
                counts={
                    "messages_observed": self.probe.observed,
                    "service_messages": len(self._service_seen),
                    **self.budget.counts(),
                    **self._extra_counts(),
                },
                booleans={
                    **self._booleans,
                    "isolation_passed": self._isolation_checked
                    and stop is None
                    or (stop is not None and stop[0] != "isolation_failed"),
                    "tls_pin_checked": self._tls_checked,
                    "stopped_early": stop is not None,
                    "all_predictions_held": stop is None
                    and all(s == "observed" for s in states.values()),
                },
                predictions=states,
                timings=self._timings,
                stop={"reason": stop[0], "group": stop[1]} if stop else None,
            )
        except export.ExportError:
            # Something outside the allowlist: export nothing but the stop.
            stop = ("identifier_in_output", self._group)
            data = export.build_export(
                booleans={"stopped_early": True},
                stop={"reason": stop[0], "group": stop[1]},
            )
        return RunResult(data, stop, states)


# ---- command line --------------------------------------------------


def _write_export(path: str, data: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(export.to_json(data) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args.add_argument("--frozen-dir", default=str(frozen.FROZEN_DIR))
    args.add_argument("--run-id", default="r1")
    args.add_argument("--export", help="write the allowlisted export here (mode 600)")
    args.add_argument("--dry-run", action="store_true", help="the simulated world, no accounts")
    args.add_argument("--live", action="store_true", help="the real world (guarded)")
    args.add_argument("--approval-token", help="owner's mode-600 approval file")
    args.add_argument("--stage-report", help="the account-free stage report")
    args.add_argument("--print-bundle-hash", action="store_true")
    args.add_argument("--service-hash", action="store_true")
    opts = args.parse_args(argv)
    try:
        inputs = frozen.load(opts.frozen_dir)
        if opts.print_bundle_hash:
            print(inputs.bundle_hash)
            return 0
        if opts.service_hash:
            print(frozen.service_hash(inputs.service_rules))
            return 0
        if opts.live == opts.dry_run:
            raise Refused("choose_dry_run_or_live")
        if opts.live:
            frozen.authorize_live(opts.approval_token, opts.stage_report, inputs)
            raise Refused("live_world_not_configured")  # the guest wires it (README)
        from .simulated import SimulatedWorld

        virtual = VirtualClock()
        driver = Driver(
            SimulatedWorld(),
            inputs,
            run_id=opts.run_id,
            clock=virtual,
            sleep=virtual.sleep,
            account_stage=False,
            isolation_checked=True,
            tls_checked=True,
        )
        result = driver.run()
    except Refused as refused:
        print(f"refused: {refused.token}", file=sys.stderr)
        return 2
    if opts.export:
        _write_export(opts.export, result.export)
    print(json.dumps({"stop": result.stop, "predictions": result.states}, sort_keys=True))
    return 1 if result.stop else 0


if __name__ == "__main__":
    raise SystemExit(main())
