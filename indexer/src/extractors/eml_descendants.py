"""Attached-email occurrence expansion, in the limited eml child (#1310).

One ledger covers the outer walk and every child of one message. Root work
is observed without changing the legacy walk; only additional work is
admitted against the remaining allowance. Failed children end expansion,
since their unreported consumption cannot safely be credited back.
"""

from __future__ import annotations

import email
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import (
    CHILD_DEGRADATION_KEYS,
    _runner,
    apply_child_degradation,
    eml,
    has_container_prefix,
    note_eml_degraded,
    resolved_extractor_module,
    warn_rate_limited,
)

if TYPE_CHECKING:
    from ..parser import Attachment

log = logging.getLogger("indexer.parser")

# The existing parser byte/structure ceilings, shared across all expansions.
# Launch and wall ceilings also bound many tiny sibling files, not just bytes.
LIMITS = {
    "parse_bytes": 64_000_000,
    "decode_bytes": 64_000_000,
    "parts": 10_000,
    "headers": 10_000,
    "header_bytes": 1_000_000,
    "occurrences": 10_000,
    "output_bytes": 64_000_000,
}
MAX_CHILDREN = 64
MAX_DEPTH = 20
_CAP = "eml_expansion"


@dataclass
class ExpansionBudget:
    left: dict[str, int] = field(default_factory=lambda: dict(LIMITS))

    def observe(self, key: str, amount: int) -> None:
        self.left[key] = max(0, self.left[key] - amount)

    def take(self, key: str, amount: int) -> bool:
        if amount > self.left[key]:
            self.left[key] = 0
            return False
        self.left[key] -= amount
        return True

    def visit(
        self, part: email.message.Message, *, enforce: bool, assess_structure: bool = True
    ) -> bool:
        charge = self.take if enforce else self._observe
        if not charge("parts", 1):
            return False
        for name, value in part.raw_items():
            if not charge("headers", 1) or not charge("header_bytes", len(name) + len(value)):
                return False
        if (
            enforce
            and assess_structure
            and (
                any(isinstance(d, email.errors.MessageDefect) for d in part.defects)
                or (part.get_content_maintype() == "multipart" and not part.is_multipart())
            )
        ):
            # Known positives may survive a structural loss, but absence cannot.
            self.incomplete = True
        return True

    def _observe(self, key: str, amount: int) -> bool:
        self.observe(key, amount)
        return True

    degraded: Counter[str] = field(default_factory=Counter)
    incomplete: bool = False
    max_depth: int = MAX_DEPTH

    def admit_parse(self, payload: bytes, depth: int) -> bool:
        return depth <= MAX_DEPTH and self.take("parse_bytes", len(payload))

    def admit_payload(self, part: email.message.Message) -> bool:
        # Charge the transport before decoding; native attached messages may
        # reparse that transport, so reserve it in both dimensions. The
        # serialization guard still applies independently inside the walker.
        value = part.get_payload()
        if isinstance(value, str):
            return self.take("decode_bytes", len(value))
        return True

    def keep(self, attachment: Attachment) -> bool:
        if (
            len(attachment.filename) > _runner.MAX_ATTACHMENT_NAME
            or len(attachment.content_type) > _runner.MAX_ATTACHMENT_TYPE
        ):
            return False
        try:
            wire = _runner.attachment_frame(_wire(attachment))
        except _runner.ChildOutputError:
            return False
        return self.take("occurrences", 1) and self.take("output_bytes", len(wire))


def _wire(attachment: Attachment) -> _runner.ChildAttachment:
    return _runner.ChildAttachment(
        attachment.filename,
        attachment.content_type,
        attachment.payload,
        attachment.content_hash,
        attachment.payload_complete,
        attachment.eml_depth,
    )


def expand_payload(payload: bytes, *options: str) -> tuple[bytes, list[str], dict[str, int]]:
    """Child entry point: reuse the MIME classifier, with no body conversion."""
    from .. import parser

    if len(options) != len(LIMITS) + 1:
        raise ValueError("invalid expansion allowances")
    depth, *amounts = map(int, options)
    if not 1 <= depth <= MAX_DEPTH or any(
        not 0 <= n <= LIMITS[k] for k, n in zip(LIMITS, amounts, strict=True)
    ):
        raise ValueError("invalid expansion allowances")
    work = ExpansionBudget(dict(zip(LIMITS, amounts, strict=True)))
    before = dict(work.left)
    if not work.admit_parse(payload, depth):
        return b"", [_CAP], {key: 0 for key in LIMITS}
    caps: Counter[str] = Counter()
    root = email.message_from_bytes(payload)
    _, attachments = parser._extract_body_and_attachments(
        root,
        caps=caps,
        raw=payload,
        expansion_work=work,
        expand_leaves=True,
        eml_depth=depth,
    )
    note_eml_degraded(
        headers=work.degraded[parser.HEADER_DEGRADED],
        filenames=work.degraded[parser.FILENAME_DEGRADED],
        charsets=0,
    )
    cut = work.incomplete or any(caps[k] for k in parser.ATTACHMENT_LOSS_CAPS)
    return (
        b"".join(_runner.attachment_frame(_wire(a)) for a in attachments),
        [_CAP] if cut else [],
        {key: before[key] - work.left[key] for key in LIMITS},
    )


def expand_attachments(
    attachments: list[Attachment], work: ExpansionBudget
) -> tuple[list[Attachment], bool]:
    from ..parser import Attachment

    expanded: list[Attachment] = []
    incomplete = False
    deadline = time.monotonic() + eml.CHILD_TIMEOUT_SECONDS
    launches = 0
    stopped = False
    for attachment in attachments:
        expanded.append(attachment)
        if (
            resolved_extractor_module(attachment.content_type, attachment.filename) != "eml"
            or not attachment.eml_leaf
            or has_container_prefix(attachment.payload)
        ):
            continue
        if (
            stopped
            or launches >= MAX_CHILDREN
            or time.monotonic() >= deadline
            or attachment.eml_depth >= MAX_DEPTH
            or len(attachment.payload) > work.left["parse_bytes"]
            or any(work.left[k] == 0 for k in ("parts", "headers", "header_bytes", "decode_bytes"))
            or work.left["occurrences"] == 0
            or work.left["output_bytes"] < 4096
        ):
            incomplete = True
            continue
        if not attachment.payload_complete:
            incomplete = True
        before = dict(work.left)
        launches += 1
        try:
            result = _runner.run_child(
                "eml-expand",
                attachment.payload,
                max_address_space_bytes=eml.CHILD_MAX_ADDRESS_SPACE_BYTES,
                max_cpu_seconds=eml.CHILD_MAX_CPU_SECONDS,
                timeout_seconds=max(0.001, deadline - time.monotonic()),
                max_output_bytes=work.left["output_bytes"] + 4096,
                caps=frozenset({_CAP}),
                counts=frozenset(LIMITS) | CHILD_DEGRADATION_KEYS,
                on_report=lambda report: apply_child_degradation(
                    log,
                    "eml-expand",
                    {k: n for k, n in report.counts.items() if k in CHILD_DEGRADATION_KEYS},
                ),
                options=[str(attachment.eml_depth + 1), *(str(n) for n in work.left.values())],
                attachment_limit=work.left["occurrences"],
            )
            if (
                not set(LIMITS) <= set(result.counts)
                or result.text
                or result.recovered
                or any(not 0 <= result.counts[k] <= before[k] for k in LIMITS)
                or result.counts["parse_bytes"] < len(attachment.payload)
                or result.counts["occurrences"] != len(result.attachments)
                or result.counts["output_bytes"]
                != sum(len(_runner.attachment_frame(a)) for a in result.attachments)
                or any(not attachment.eml_depth < a.depth <= MAX_DEPTH for a in result.attachments)
            ):
                raise _runner.ChildOutputError
        except (
            _runner.ChildError,
            _runner.ChildOutputError,
            _runner.ToolTimeoutError,
            _runner.ToolExitError,
            _runner.ToolCrashError,
            _runner.ToolNotFoundError,
            OSError,
        ) as exc:
            reason = exc.type_name if isinstance(exc, _runner.ChildError) else type(exc).__name__
            warn_rate_limited(
                log, "attached email expansion failed (%s); manifest incomplete", reason
            )
            incomplete = stopped = True
            continue
        for key in LIMITS:
            work.observe(key, result.counts[key])
        incomplete |= bool(result.caps)
        expanded.extend(
            Attachment(
                filename=a.filename,
                content_type=a.content_type,
                size=len(a.payload),
                payload=a.payload,
                content_hash=a.content_hash,
                payload_complete=a.payload_complete,
                eml_depth=a.depth,
            )
            for a in result.attachments
        )
    return expanded, incomplete
