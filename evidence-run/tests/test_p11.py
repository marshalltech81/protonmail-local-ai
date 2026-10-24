"""The P11 comparison method and its local test (#1759, procedure v5 P11)."""

import hashlib

import pytest
from evidence_run import p11

BOUNDARY = "SYNTHBOUNDARY1759"
SERVED = (
    "From: Synthetic One <one@mail.example>\r\n"
    "To: Synthetic Two <two@mail.example>\r\n"
    "Subject: P11 fixture\r\n"
    "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
    "Message-ID: <p11@mail.example>\r\n"
    "MIME-Version: 1.0\r\n"
    f'Content-Type: multipart/alternative; boundary="{BOUNDARY}"\r\n'
    "\r\n"
    f"--{BOUNDARY}\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "\r\n"
    "plain part\r\n"
    f"--{BOUNDARY}\r\n"
    "Content-Type: text/html; charset=utf-8\r\n"
    "\r\n"
    "<p>html part</p>\r\n"
    f"--{BOUNDARY}--\r\n"
).encode()


def as_isync_wrote_it(served: bytes, *, tuid: str = "X-TUID: abcdEFGH0123") -> bytes:
    """The documented difference (H3): LF line endings and one X-TUID
    field, which isync adds to the header block."""
    return (tuid + "\n").encode() + served.replace(b"\r\n", b"\n")


def test_the_documented_difference_alone_is_equal():
    result = p11.compare(as_isync_wrote_it(SERVED), SERVED)
    assert result == p11.P11Result("equal", 1, 0, 0)


def test_a_mime_boundary_only_change_is_a_difference():
    changed = as_isync_wrote_it(SERVED).replace(BOUNDARY.encode(), b"SYNTHBOUNDARY1759X")
    assert p11.compare(changed, SERVED).verdict == "different"


def test_a_one_header_change_is_a_difference():
    changed = as_isync_wrote_it(SERVED).replace(b"Subject: P11 fixture", b"Subject: P11 fixturf")
    assert p11.compare(changed, SERVED).verdict == "different"


def test_a_body_change_is_a_difference():
    changed = as_isync_wrote_it(SERVED).replace(b"plain part", b"plain pare")
    assert p11.compare(changed, SERVED).verdict == "different"


def test_a_second_tuid_in_the_file_fails_the_precondition():
    twice = b"X-TUID: zzzzzzzzzzzz\n" + as_isync_wrote_it(SERVED)
    result = p11.compare(twice, SERVED)
    assert result.verdict == "precondition_failed"
    assert result.tuid_in_file == 2


def test_no_tuid_in_the_file_fails_the_precondition():
    result = p11.compare(SERVED.replace(b"\r\n", b"\n"), SERVED)
    assert (result.verdict, result.tuid_in_file) == ("precondition_failed", 0)


def test_a_tuid_in_the_served_bytes_fails_the_precondition():
    served = b"X-TUID: served00000\r\n" + SERVED
    result = p11.compare(as_isync_wrote_it(SERVED), served)
    assert (result.verdict, result.tuid_in_served) == ("precondition_failed", 1)


@pytest.mark.parametrize("served", [SERVED.replace(b"\r\n", b"\n", 1), SERVED + b"\rtail"])
def test_bare_line_endings_in_the_served_bytes_fail_the_precondition(served):
    result = p11.compare(as_isync_wrote_it(SERVED), served)
    assert result.verdict == "precondition_failed"
    assert result.bare_in_served >= 1


def test_a_tuid_like_text_in_the_body_is_not_a_header_field():
    body_mention = SERVED.replace(b"plain part", b"X-TUID: in the body")
    result = p11.compare(as_isync_wrote_it(body_mention), body_mention)
    assert result == p11.P11Result("equal", 1, 0, 0)


def test_the_digest_is_over_the_reconstituted_file_and_the_raw_served_bytes():
    assert p11.normalized_digest(as_isync_wrote_it(SERVED)) == hashlib.sha256(SERVED).hexdigest()
