"""Synthetic fixtures for the evidence run (procedure v5 sections 2 and 4).

Every message is ASCII with CRLF line endings only (P11), carries a
correlation token ("SYNTH-<run>-<n>") in its subject and body, and uses
``.example`` addresses. The G6 set is generated on demand and never kept.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator

PLAIN_MAX_BYTES = 64 * 1024
G6_COUNT = 200
G6_MESSAGE_BYTES = 256 * 1024  # 200 x 256 KiB = 52,428,800 bytes of unique payload

_RUN = re.compile(r"[A-Za-z0-9]{1,16}")
FORGED_KINDS = ("forged", "duplicated", "conflicting")
_LINE = 76  # filler line length, before CRLF


def token(run: str, n: int) -> str:
    _check_run(run)
    return f"SYNTH-{run}-{n}"


def _check_run(run: str) -> None:
    if not _RUN.fullmatch(run):
        raise ValueError("a run id is 1 to 16 letters or digits")


def _filler(run: str, n: int, length: int) -> str:
    """``length`` bytes of unique, CRLF-ended lines (hex of a hash chain)."""
    if length == 0:
        return ""
    if length == 1:
        raise ValueError("a filler of one byte cannot hold a line ending")
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
        raise ValueError(f"forged kind is one of {FORGED_KINDS}")
    return [f"X-Pm-Internal-Id: {v}" for v in internal] + [f"X-Pm-Gluon-Id: {v}" for v in gluon]


def _build(run: str, n: int, *, size: int, multipart: bool, extra: list[str], cap: int) -> bytes:
    _check_run(run)
    if not 0 < size <= cap:
        raise ValueError("size is positive and within the cap for this fixture")
    tok = token(run, n)
    boundary = f"SYNTHBOUNDARY{run}x{n}"
    head = [
        "From: Synthetic Sender <sender@sender.example>",
        "To: Synthetic Recipient <recipient@mail.example>",
        f"Subject: {tok}",
        "Date: Mon, 01 Jan 2024 12:00:00 +0000",
        f"Message-ID: <synth-{run}-{n}@sender.example>",
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
        raise ValueError("size is smaller than the message's own headers and parts")
    raw = ("\r\n".join(head) + "\r\n\r\n" + prefix + _filler(run, n, fill) + suffix).encode("ascii")
    assert len(raw) == size
    return raw


def control_message(run: str, n: int, *, size: int = 2048, multipart: bool = False) -> bytes:
    """Ordinary mail with no identity field (Bridge adds its own on delivery)."""
    return _build(run, n, size=size, multipart=multipart, extra=[], cap=PLAIN_MAX_BYTES)


def forged_message(
    run: str, n: int, kind: str, *, size: int = 2048, multipart: bool = False
) -> bytes:
    """Mail carrying forged, duplicated or conflicting ``X-Pm-*`` fields."""
    return _build(
        run,
        n,
        size=size,
        multipart=multipart,
        extra=_identity_fields(run, n, kind),
        cap=PLAIN_MAX_BYTES,
    )


def g6_messages(run: str, count: int = G6_COUNT, size: int = G6_MESSAGE_BYTES) -> Iterator[bytes]:
    """The G6 workload, one message at a time: nothing is kept between
    yields, so the set is never in memory or on disk as a whole."""
    for n in range(1, count + 1):
        yield _build(run, n, size=size, multipart=False, extra=[], cap=G6_MESSAGE_BYTES)
