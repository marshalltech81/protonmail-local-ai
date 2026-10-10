"""Exact running sums of a thread's chunk vectors (#1356).

A thread's vector is the mean of its chunk vectors. Instead of reading
every chunk vector of the thread to recompute that mean, the database
keeps, per thread, the exact sum ``S`` of the stored chunk vectors and
their count (``thread_vector_sums``), and each write that adds, replaces
or removes chunks applies ``S += new - old``.

Exactness: each component is summed as an integer in units of 2**-149,
the smallest float32 subnormal. Every finite float32 value is an integer
multiple of that unit, so the stored float32 bytes (``serialize_float32``)
convert to integers with no rounding and integer addition never drifts:
the running sum always equals a full recompute, whatever the order of
the writes.

The thread vector is derived from ``S`` and the count by one rule
(``thread_vector``), used by every writer of a chunk-mean thread vector.

Encoding (``ENCODING_VERSION`` 1) of ``S``: an all-zero sum is the empty
blob; otherwise, per component in order, ``varint(k)``, ``varint(n)`` and
``n`` bytes of ``m`` as signed big-endian two's complement, where the
component equals ``m * 2**k`` with ``m`` odd (``k = n = 0`` for a zero
component). ``varint`` is unsigned LEB128. The encoding of a sum is
unique, so two sums are equal exactly when their blobs are.
"""

from __future__ import annotations

import struct

from .chunker import l2_normalize

ENCODING_VERSION = 1

# One unit of the sums: 2**-149. Multiplying a float32 value (exact in a
# float64) by this power of two is exact, and the product is an integer.
_UNITS_PER_ONE = 2.0**149
_UNIT_SHIFT = 149


def vector_units(blob: bytes) -> list[int]:
    """The components of a stored float32 vector as integers in units of
    2**-149. Exact. Raises ``ValueError`` on a non-finite component."""
    values = struct.unpack(f"{len(blob) // 4}f", blob)
    try:
        return [int(x * _UNITS_PER_ONE) for x in values]
    except OverflowError, ValueError:
        # ``int`` of inf raises OverflowError, of NaN ValueError.
        raise ValueError("vector has non-finite components") from None


def add_units(total: list[int], units: list[int], sign: int = 1) -> list[int]:
    """``total + sign * units``, component-wise."""
    if len(total) != len(units):
        raise ValueError("all vectors must have the same dimension")
    if sign >= 0:
        return [a + b for a, b in zip(total, units, strict=True)]
    return [a - b for a, b in zip(total, units, strict=True)]


def _put_varint(out: bytearray, value: int) -> None:
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)


def encode(total: list[int]) -> bytes:
    """Encode a sum (see the module docstring)."""
    if not any(total):
        return b""
    out = bytearray()
    for value in total:
        if not value:
            out += b"\x00\x00"
            continue
        k = (value & -value).bit_length() - 1
        m = value >> k
        n = (m.bit_length() + 8) // 8
        _put_varint(out, k)
        _put_varint(out, n)
        out += m.to_bytes(n, "big", signed=True)
    return bytes(out)


def decode(blob: bytes, dim: int) -> list[int]:
    """Decode a sum of ``dim`` components. Raises ``ValueError`` on a
    malformed blob."""
    if not blob:
        return [0] * dim
    total: list[int] = []
    i = 0
    end = len(blob)
    try:
        while i < end:
            k = 0
            shift = 0
            while True:
                byte = blob[i]
                i += 1
                k |= (byte & 0x7F) << shift
                shift += 7
                if byte < 0x80:
                    break
            n = 0
            shift = 0
            while True:
                byte = blob[i]
                i += 1
                n |= (byte & 0x7F) << shift
                shift += 7
                if byte < 0x80:
                    break
            if i + n > end:
                raise ValueError("truncated vector sum")
            total.append(int.from_bytes(blob[i : i + n], "big", signed=True) << k)
            i += n
    except IndexError:
        raise ValueError("truncated vector sum") from None
    if len(total) != dim:
        raise ValueError("vector sum has the wrong dimension")
    return total


def mean(total: list[int], count: int) -> list[float]:
    """The mean of ``count`` chunk vectors summing to ``total``: each
    component ``S_i / (count * 2**149)`` is the exact quotient rounded
    once to the nearest float64, ties to even (Python's ``int / int`` is
    correctly rounded). ``count`` must be positive."""
    if count <= 0:
        raise ValueError("cannot mean an empty vector list")
    divisor = count << _UNIT_SHIFT
    return [value / divisor for value in total]


def thread_vector(total: list[int], count: int) -> list[float]:
    """The thread vector for a sum of ``count`` chunk vectors.

    The rule: ``mean`` (the exact quotient rounded once to float64, ties
    to even), then L2-normalized by ``chunker.l2_normalize`` in float64,
    then stored as float32 by ``serialize_float32`` (round to nearest,
    ties to even). ``count`` must be positive: a thread without chunk
    vectors keeps its chunkless fallback.
    """
    return l2_normalize(mean(total, count))
