"""The P11 comparison (procedure v5, P11).

Isync writes a message with LF line endings and one added ``X-TUID``
field; Bridge serves CRLF and no such field (H3). The digest of the file
after its one ``X-TUID`` is removed and its LF endings turned back to CRLF
is compared with the digest of the raw served bytes, so only the file side
is changed, as H3 did, and a MIME-boundary, header or body difference
survives. Three counts are recorded first as fixed tokens; if any is off,
the method does not apply and the verdict is ``precondition_failed``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_TUID = re.compile(rb"^x-tuid:", re.IGNORECASE)


@dataclass(frozen=True)
class P11Result:
    verdict: str  # "equal" | "different" | "precondition_failed"
    tuid_in_file: int  # X-TUID header fields in the file (must be exactly 1)
    bare_in_served: int  # bare CR or LF in the served bytes (must be 0)
    tuid_in_served: int  # X-TUID header fields in the served bytes (must be 0)


def _header_lines(data: bytes) -> list[bytes]:
    """The lines of the header block, up to the first empty line."""
    return data.replace(b"\r\n", b"\n").partition(b"\n\n")[0].split(b"\n")


def _count_tuid(data: bytes) -> int:
    return sum(1 for line in _header_lines(data) if _TUID.match(line))


def _bare(data: bytes) -> int:
    bare_lf = len(re.findall(rb"(?<!\r)\n", data))
    bare_cr = len(re.findall(rb"\r(?!\n)", data))
    return bare_lf + bare_cr


def _reconstitute(file_bytes: bytes) -> bytes:
    """The file without its one X-TUID header field and with CRLF endings."""
    text = file_bytes.replace(b"\r\n", b"\n")
    head, sep, rest = text.partition(b"\n\n")
    kept = [line for line in head.split(b"\n") if not _TUID.match(line)]
    return (b"\n".join(kept) + sep + rest).replace(b"\n", b"\r\n")


def normalized_digest(file_bytes: bytes) -> str:
    return hashlib.sha256(_reconstitute(file_bytes)).hexdigest()


def compare(file_bytes: bytes, served: bytes) -> P11Result:
    tuid_file, bare, tuid_served = _count_tuid(file_bytes), _bare(served), _count_tuid(served)
    if tuid_file != 1 or bare != 0 or tuid_served != 0:
        return P11Result("precondition_failed", tuid_file, bare, tuid_served)
    equal = normalized_digest(file_bytes) == hashlib.sha256(served).hexdigest()
    return P11Result("equal" if equal else "different", tuid_file, bare, tuid_served)
