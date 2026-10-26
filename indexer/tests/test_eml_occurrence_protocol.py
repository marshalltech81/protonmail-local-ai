"""The new binary frames are opt-in, bounded and validated before storage."""

import hashlib
import json

import pytest
from src.extractors import _runner


def _attachment(payload=b"synthetic\nA 1 1\n\x00\xff"):
    return _runner.ChildAttachment(
        "synthetic.txt",
        "application/octet-stream",
        payload,
        hashlib.sha256(payload).hexdigest(),
        True,
        1,
    )


@pytest.mark.parametrize("chunk", [1, 2, 7, 1024, 65536])
def test_binary_frames_round_trip_at_every_fragment_boundary(chunk):
    attachment = _attachment()
    wire = b"N work 2\n" + _runner.attachment_frame(attachment) * 2 + b"T 3\nend"
    frames = _runner._Frames(frozenset(), frozenset({"work"}), None, attachment_limit=2)
    for offset in range(0, len(wire), chunk):
        frames.feed(wire[offset : offset + chunk])
    result = frames.result()
    assert result.attachments == [attachment, attachment]
    assert result.text == "end" and result.counts == {"work": 2}


@pytest.mark.parametrize("limit", [0, 1])
def test_binary_frames_require_opt_in_and_obey_occurrence_limit(limit):
    frames = _runner._Frames(frozenset(), frozenset(), None, attachment_limit=limit)
    with pytest.raises(_runner.ChildOutputError):
        frames.feed(_runner.attachment_frame(_attachment()) * 2 + b"T 0\n")


@pytest.mark.parametrize(
    "change",
    [
        {"filename": 123},
        {"filename": "\ud800"},
        {"filename": "x" * 8193},
        {"content_type": "x" * 257},
        {"content_hash": "0" * 64},
        {"payload_complete": 1},
        {"depth": True},
        {"depth": 21},
        {"extra": "not allowed"},
    ],
)
def test_invalid_metadata_never_becomes_an_occurrence(change):
    a = _attachment(b"bytes")
    metadata = dict(
        filename=a.filename,
        content_type=a.content_type,
        content_hash=a.content_hash,
        payload_complete=a.payload_complete,
        depth=a.depth,
    )
    metadata.update(change)
    body = json.dumps(metadata).encode()
    frames = _runner._Frames(frozenset(), frozenset(), None, attachment_limit=1)
    with pytest.raises(_runner.ChildOutputError):
        frames.feed(f"A {len(body)} 5\n".encode() + body + b"bytesT 0\n")


@pytest.mark.parametrize(
    "wire",
    [
        b"A 65537 1\n",
        b"A 0 0\n",
        b"A -1 1\n",
        b"A 1 no\n",
        b"A 1 1\n{aT 0\n",
        b"A 4 1\nnullaT 0\n",
        b"T 0\nA 1 1\n",
        b"A 1 1\n{",
        b"A 1 1\n",
    ],
)
def test_malformed_or_incomplete_frames_fail_closed(wire):
    frames = _runner._Frames(frozenset(), frozenset(), None, attachment_limit=1)
    with pytest.raises(_runner.ChildOutputError):
        frames.feed(wire)
        frames.result()


@pytest.mark.serial
def test_large_binary_payload_is_streamed_with_linear_buffer_writes():
    import time

    class CountedBytes(bytearray):
        written = 0

        def extend(self, data):
            self.written += len(data)
            return super().extend(data)

    payload = b"Synthetic binary bytes\n" * 400_000
    attachment = _attachment(payload)
    wire = _runner.attachment_frame(attachment) + b"T 0\n"
    frames = _runner._Frames(frozenset(), frozenset(), None, attachment_limit=1)
    counted = CountedBytes()
    frames._attachment_data = counted
    start = time.monotonic()
    calls = 0
    for offset in range(0, len(wire), _runner._READ_CHUNK):
        frames.feed(wire[offset : offset + _runner._READ_CHUNK])
        assert len(frames._line) <= _runner._MAX_FRAME_LINE
        calls += 1
    assert frames.result().attachments == [attachment]
    assert len(payload) <= counted.written <= len(payload) + _runner.MAX_ATTACHMENT_METADATA
    assert calls == (len(wire) + _runner._READ_CHUNK - 1) // _runner._READ_CHUNK
    assert time.monotonic() - start < 10
