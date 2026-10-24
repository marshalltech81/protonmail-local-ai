"""Known unsafe call shapes must fail the new work gates before costly work."""

import email.header
import email.utils

import pytest

from tests.work_counts import CountedReplyPattern


@pytest.mark.parametrize("boundary", ["decode", "address"])
def test_swallowed_boundary_assertion_still_fails_the_work_check(
    header_work, monkeypatch, boundary
):
    from src import parser

    hostile = "=?utf-8?q?x " * 16000
    if boundary == "decode":
        counted = email.header.decode_header
        monkeypatch.setattr(email.header, "decode_header", lambda value: counted(hostile))
        # The production decoder catches the instrumentation's assertion.
        parser._decode_display_name("=?utf-8?q?ok?=")
        message = "whole header reached"
    else:
        counted = email.utils.parseaddr
        monkeypatch.setattr(email.utils, "parseaddr", lambda value: counted(hostile))
        # The production address parser catches it too.
        assert parser._parse_addrs("a@example.test") == []
        parser._decode_header("=?utf-8?q?ok?=")
        message = "whole address list reached"
    with pytest.raises(AssertionError, match=message):
        header_work.check(len(hostile))


def test_whole_header_decoder_regression_is_caught(header_work):
    # The pre-#218 implementation sent this entire malformed header to
    # decode_header, which repeatedly rescanned its remaining suffix.
    with pytest.raises(AssertionError, match="whole header reached"):
        email.header.decode_header("=?utf-8?q?x " * 16000)


def test_whole_group_parser_regression_is_caught(header_work):
    # The old unsplit group route handed the whole address-list to the
    # standard-library parser rather than bounded individual elements.
    with pytest.raises(AssertionError, match="whole address list reached"):
        email.utils.parseaddr("Group: " + "a@example.test," * 10000 + ";")


def test_reply_regex_without_the_line_guard_is_caught(reply_work):
    from src import quoting

    # Calling the same guarded matcher directly models removal of the
    # pre-regex length check, without letting a pathological regex hang.
    assert isinstance(quoting._REPLY_HEADER_PATTERNS[0], CountedReplyPattern)
    with pytest.raises(AssertionError, match="overlong line reached"):
        quoting._REPLY_HEADER_PATTERNS[0].match("On " + "<a@b> " * 40000)
