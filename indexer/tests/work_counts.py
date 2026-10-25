"""Test-only counters at the expensive parser and regex boundaries."""

import email.header
import email.utils
from dataclasses import dataclass, field

from src import parser


class CountedPattern:
    def __init__(self, pattern, lengths):
        self._pattern = pattern
        self.lengths = lengths

    def __getattr__(self, name):
        return getattr(self._pattern, name)

    def search(self, text, *args):
        self.lengths.append(len(text))
        return self._pattern.search(text, *args)

    def sub(self, replacement, text, *args):
        self.lengths.append(len(text))
        return self._pattern.sub(replacement, text, *args)

    def finditer(self, text, *args):
        self.lengths.append(len(text))
        return self._pattern.finditer(text, *args)


@dataclass
class HeaderWork:
    decoded: list[int] = field(default_factory=list)
    addresses: list[int] = field(default_factory=list)
    regex: list[int] = field(default_factory=list)

    def install(self, monkeypatch):
        decode = email.header.decode_header
        parse = email.utils.parseaddr

        def counted_decode(value):
            # Header objects already carry decoded chunks. A string must
            # be a bounded encoded-word, never the whole hostile header.
            if isinstance(value, str):
                self.decoded.append(len(value))
                assert len(value) <= parser._MAX_ENCODED_WORD_CHARS, (
                    "whole header reached decode_header"
                )
            return decode(value)

        def counted_parse(value, *args, **kwargs):
            self.addresses.append(len(value))
            assert len(value) <= parser._MAX_ADDRESS_ELEMENT_CHARS, (
                "whole address list reached parseaddr"
            )
            return parse(value, *args, **kwargs)

        monkeypatch.setattr(email.header, "decode_header", counted_decode)
        monkeypatch.setattr(email.utils, "parseaddr", counted_parse)
        for name in ("_ENCODED_WORD_RE", "_ENCODED_WORD_RUN_RE"):
            monkeypatch.setattr(parser, name, CountedPattern(getattr(parser, name), self.regex))

    def check(self, input_chars):
        # Production deliberately catches Exception around these calls.
        # Recheck recorded sizes here so a swallowed assertion cannot pass.
        assert all(n <= parser._MAX_ENCODED_WORD_CHARS for n in self.decoded), (
            "whole header reached decode_header"
        )
        assert all(n <= parser._MAX_ADDRESS_ELEMENT_CHARS for n in self.addresses), (
            "whole address list reached parseaddr"
        )
        assert self.regex, "the guarded scan was not exercised"
        assert sum(self.regex) <= 12 * input_chars
        assert sum(self.addresses) <= 12 * input_chars
        assert sum(self.decoded) <= 12 * input_chars
        assert len(self.addresses) <= 4 * parser.MAX_MESSAGE_ADDRESSES


class CountedReplyPattern:
    def __init__(self, pattern, lengths):
        self._pattern = pattern
        self.lengths = lengths

    def match(self, text):
        from src import quoting

        self.lengths.append(len(text))
        assert len(text) <= quoting._MAX_REPLY_HEADER_CHARS, "overlong line reached reply regex"
        return self._pattern.match(text)
