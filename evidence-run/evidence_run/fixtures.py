"""Synthetic fixtures for the evidence run (procedure v5 sections 2 and 4).

Every message is ASCII with CRLF line endings only (P11), carries a
correlation token ("SYNTH-<run>-<n>") in its subject and body, and uses
``.example`` placeholder addresses unless the run supplies its own (#1784).
The G6 set is generated on demand and never kept.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from types import SimpleNamespace

PLAIN_MAX_BYTES = 64 * 1024
G6_COUNT = 200
G6_MESSAGE_BYTES = 256 * 1024  # 200 x 256 KiB = 52,428,800 bytes of unique payload

SENDER = "sender@sender.example"
RECIPIENT = "recipient@mail.example"

_RUN = re.compile(r"[A-Za-z0-9]{1,16}")
FORGED_KINDS = ("forged", "duplicated", "conflicting")
_ATOM = r"[A-Za-z0-9_+-]+"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
# A dot-atom local part (no leading, trailing or doubled dot) and a host of
# two to nine labels: what the header parser reads back as an address.
_ADDRESS = re.compile(rf"(?=[^@]{{1,64}}@){_ATOM}(?:\.{_ATOM})*@{_LABEL}(?:\.{_LABEL}){{1,8}}")
_LINE = 76  # filler line length, before CRLF


class FixtureError(ValueError):
    """The generator cannot build the requested message; the text is fixed."""


def check_address(address: str) -> str:
    """The address, if it is a plain ASCII ``local@host.tld`` that cannot
    alter a header; the refusal text never repeats it."""
    if not _ADDRESS.fullmatch(address):
        raise FixtureError("an address is plain ASCII local@host.domain")
    return address


def domain_of(address: str) -> str:
    return check_address(address).rsplit("@", 1)[1].lower()


def width_representatives(low: int, high: int) -> list[int]:
    """The lowest number of each decimal width in ``low..high``.

    A message's own header and part lengths depend on its number only through
    the number's decimal width, so one number per width proves every number
    in that width builds or fails alike, whatever the count.
    """
    if low < 1 or high < low:
        return []
    found, width = [], len(str(low))
    while 10 ** (width - 1) <= high:
        found.append(max(low, 10 ** (width - 1)))
        width += 1
    return found


def token(run: str, n: int) -> str:
    _check_run(run)
    return f"SYNTH-{run}-{n}"


def _check_run(run: str) -> None:
    if not _RUN.fullmatch(run):
        raise FixtureError("a run id is 1 to 16 letters or digits")


def _filler(run: str, n: int, length: int) -> str:
    """``length`` bytes of unique, CRLF-ended lines (hex of a hash chain)."""
    if length == 0:
        return ""
    if length == 1:
        raise FixtureError("a filler of one byte cannot hold a line ending")
    lines: list[str] = []
    state = hashlib.sha256(f"{run}-{n}".encode()).hexdigest()
    remaining = length
    while remaining > 0:
        take = min(_LINE + 2, remaining)
        if remaining - take == 1:  # never leave a one-byte tail
            take = remaining - 2 if remaining - 2 >= 2 else remaining
        state = hashlib.sha256(state.encode()).hexdigest()
        text = (state * 2)[: take - 2]
        lines.append(text + "\r\n")
        remaining -= take
    return "".join(lines)


def _identity_fields(run: str, n: int, kind: str) -> list[str]:
    if kind == "forged":
        internal = [f"SYNTHFORGED-{run}-{n}"]
        gluon = [f"SYNTHFORGEDGLUON-{run}-{n}"]
    elif kind == "duplicated":
        internal = [f"SYNTHFORGED-{run}-{n}"] * 2
        gluon = [f"SYNTHFORGEDGLUON-{run}-{n}"] * 2
    elif kind == "conflicting":
        internal = [f"SYNTHFORGED-{run}-{n}-a", f"SYNTHFORGED-{run}-{n}-b"]
        gluon = [f"SYNTHFORGEDGLUON-{run}-{n}-a", f"SYNTHFORGEDGLUON-{run}-{n}-b"]
    else:
        raise FixtureError(f"forged kind is one of {FORGED_KINDS}")
    return [f"X-Pm-Internal-Id: {v}" for v in internal] + [f"X-Pm-Gluon-Id: {v}" for v in gluon]


def _build(
    run: str,
    n: int,
    *,
    size: int,
    multipart: bool,
    extra: list[str],
    cap: int,
    sender: str,
    recipient: str,
) -> bytes:
    _check_run(run)
    sender, recipient = check_address(sender), check_address(recipient)
    if not 0 < size <= cap:
        raise FixtureError("size is positive and within the cap for this fixture")
    tok = token(run, n)
    boundary = f"SYNTHBOUNDARY{run}x{n}"
    head = [
        f"From: Synthetic Sender <{sender}>",
        f"To: Synthetic Recipient <{recipient}>",
        f"Subject: {tok}",
        "Date: Mon, 01 Jan 2024 12:00:00 +0000",
        f"Message-ID: <synth-{run}-{n}@{sender.rsplit('@', 1)[1]}>",
        *extra,
        "MIME-Version: 1.0",
    ]
    if multipart:
        head.append(f'Content-Type: multipart/alternative; boundary="{boundary}"')
        prefix = f"--{boundary}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nplain {tok}\r\n"
        suffix = (
            f"--{boundary}\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
            f"<p>html {tok}</p>\r\n--{boundary}--\r\n"
        )
    else:
        head += ["Content-Type: text/plain; charset=utf-8", "Content-Transfer-Encoding: 7bit"]
        prefix, suffix = f"body {tok}\r\n", ""
    fixed = "\r\n".join(head) + "\r\n\r\n" + prefix + suffix
    fill = size - len(fixed)
    if fill < 0:
        raise FixtureError("size is smaller than the message's own headers and parts")
    raw = ("\r\n".join(head) + "\r\n\r\n" + prefix + _filler(run, n, fill) + suffix).encode("ascii")
    assert len(raw) == size
    return raw


def control_message(
    run: str,
    n: int,
    *,
    size: int = 2048,
    multipart: bool = False,
    sender: str = SENDER,
    recipient: str = RECIPIENT,
) -> bytes:
    """Ordinary mail with no identity field (Bridge adds its own on delivery)."""
    return _build(
        run,
        n,
        size=size,
        multipart=multipart,
        extra=[],
        cap=PLAIN_MAX_BYTES,
        sender=sender,
        recipient=recipient,
    )


def forged_message(
    run: str,
    n: int,
    kind: str,
    *,
    size: int = 2048,
    multipart: bool = False,
    sender: str = SENDER,
    recipient: str = RECIPIENT,
) -> bytes:
    """Mail carrying forged, duplicated or conflicting ``X-Pm-*`` fields."""
    return _build(
        run,
        n,
        size=size,
        multipart=multipart,
        extra=_identity_fields(run, n, kind),
        cap=PLAIN_MAX_BYTES,
        sender=sender,
        recipient=recipient,
    )


def g6_message(
    run: str,
    n: int,
    *,
    size: int = G6_MESSAGE_BYTES,
    sender: str = SENDER,
    recipient: str = RECIPIENT,
) -> bytes:
    return _build(
        run,
        n,
        size=size,
        multipart=False,
        extra=[],
        cap=G6_MESSAGE_BYTES,
        sender=sender,
        recipient=recipient,
    )


def g6_messages(
    run: str,
    count: int = G6_COUNT,
    size: int = G6_MESSAGE_BYTES,
    *,
    sender: str = SENDER,
    recipient: str = RECIPIENT,
) -> Iterator[bytes]:
    """The G6 workload, one message at a time: nothing is kept between
    yields, so the set is never in memory or on disk as a whole."""
    for n in range(1, count + 1):
        yield g6_message(run, n, size=size, sender=sender, recipient=recipient)


def bind(sender: str, recipient: str) -> SimpleNamespace:
    """The four generators with the run's own From and To addresses.

    Each call looks the generator up by name, so a replaced module function
    is the one a run uses.
    """
    check_address(sender), check_address(recipient)
    fixed = {"sender": sender, "recipient": recipient}
    return SimpleNamespace(
        control_message=lambda *a, **k: control_message(*a, **k, **fixed),
        forged_message=lambda *a, **k: forged_message(*a, **k, **fixed),
        g6_message=lambda *a, **k: g6_message(*a, **k, **fixed),
        g6_messages=lambda *a, **k: g6_messages(*a, **k, **fixed),
    )
