"""HTML conversion in the extractor child (#1294).

The ``html`` attachment extractor and the parser's conversion of a
message's ``text/html`` body parts both run ``html2text`` in the
extractor child under the limits ``html.py`` passes, one child per
attachment and one per message. Most tests here run the child in this
process (``tests/conftest.py``) or stub its output; those marked
``real_extractor_child`` start the real process, and the one that needs
the address-space limit runs on Linux only (the image and CI). All HTML
is synthetic.
"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import html2text
import pytest
from src import extractors, parser
from src.extractors import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    _runner,
    extract,
    extractor_child,
    html,
    html_child,
)
from src.extractors._runner import (
    ChildOutputError,
    ToolCrashError,
    ToolExitError,
    ToolTimeoutError,
)
from src.parser import parse_email

from tests.test_extractor_child import stub_child_output

MARKER = "SYNTHETIC_HTML_MARKER"
real_child = pytest.mark.real_extractor_child
linux_only = pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS applies on Linux")


def _in_process(source: str) -> str:
    """The conversion as the indexer ran it in process before #1294
    (``extractors/html.py`` and ``parser._html_to_text`` on main), kept
    verbatim as the ground truth of the differential tests."""
    h2t = html2text.HTML2Text()
    h2t.ignore_links = True
    h2t.ignore_images = True
    h2t.body_width = 0
    return h2t.handle(source)


def _attachment_in_process(payload: bytes) -> str:
    return _in_process(payload.decode("utf-8", errors="replace"))


# Synthetic HTML shapes, as the bytes an attachment carries (or a body
# part's text encoded as UTF-8): each is a class of input the converter
# treats differently.
_SHAPES: dict[str, bytes] = {
    "plain": b"<html><body><p>Plain paragraph one.</p><p>Paragraph two.</p></body></html>",
    "empty": b"",
    "whitespace": b"  \r\n\t ",
    "text-only": b"no markup at all, just text\nand a second line",
    "nested-tables": (
        b"<table><tr><td><table><tr><td>inner a</td><td>inner b</td></tr></table></td>"
        b"<td>outer</td></tr><tr><td colspan=2>wide</td></tr></table>"
    ),
    "lists-headings": (
        b"<h1>Title</h1><h2>Sub</h2><ul><li>one</li><li>two<ol><li>a</li><li>b</li></ol></li>"
        b"</ul><blockquote>quoted<blockquote>deeper</blockquote></blockquote><hr><pre>  x\n y</pre>"
    ),
    "entities": (
        b"<p>&amp; &lt; &gt; &quot; &#39; &nbsp; &eacute; &#233; &#xE9; &#x1F600; &#0; "
        b"&#xD800; &#x110000; &bogus; &amp</p>"
    ),
    "broken-markup": (
        b"<p>unclosed <b>bold <i>italic <div>div<span>span</p></b> stray </td></tr> <p "
        b'<a href="x">link</a> <img src="y" alt="pic"> <!-- comment --> <![CDATA[cdata]]>'
    ),
    "scripts-styles": (
        b"<style>p { color: red }</style><script>var a = '<p>not text</p>';</script>"
        b"<p>visible</p><noscript>ns</noscript>"
    ),
    "unclosed-style": b"<style>unfinished { color: red }<p>text after</p>",
    "deep-nesting": b"<div>" * 3000 + b"deep text" + b"</div>" * 3000,
    "deep-blockquote": b"<blockquote>" * 300 + b"q<br>" * 20 + b"</blockquote>" * 300,
    "huge": b"<p>" + b"lorem ipsum dolor sit amet " * 80_000 + b"</p>",
    "utf8-multibyte": "<p>café 中文 \U0001f600 ​  x</p>".encode(),
    "invalid-utf8": b"<p>caf\xc3\xa9 \xff\xfe text \xe2\x82 \xed\xa0\x80 end</p>",
    "control-chars": b"<p>a\x00b\x01c\x1fd\x7f</p>",
    "crlf": b"<p>line one\r\nline two\r\n</p>\r\n<p>three</p>",
    "markdown-chars": b"<p>*star* _under_ `tick` [br]acket # hash 1. num \\ back</p>",
}


def _message(*parts: tuple[str, bytes], plain: str | None = None) -> bytes:
    """A multipart/mixed message: an optional plain part, then each
    ``(content type, body)`` part, 8-bit UTF-8."""
    head = (
        b"Message-ID: <html@example.test>\r\nFrom: sender@example.test\r\n"
        b"Date: Mon, 28 Sep 2026 12:00:00 +0000\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
    )
    body = b""
    if plain is not None:
        body += b"--b\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n" + plain.encode() + b"\r\n"
    for content_type, data in parts:
        body += (
            b"--b\r\nContent-Type: "
            + content_type.encode()
            + b"\r\nContent-Transfer-Encoding: base64\r\n\r\n"
            + base64.encodebytes(data)
        )
    return head + body + b"--b--\r\n"


def _parse(tmp_path: Path, raw: bytes, name: str = "m.eml"):
    folder = tmp_path / "INBOX" / "cur"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(raw)
    msg = parse_email(path)
    assert msg is not None
    return msg, path


def _expected_body(*sources: bytes) -> str:
    """The body the in-process parser assembled from these HTML parts:
    each converted, stripped, the non-blank ones joined by a blank line."""
    texts = [_attachment_in_process(s).strip() for s in sources]
    return "\n\n".join(t for t in texts if t)


class TestDifferentialCatalogue:
    """Old in-process conversion against the new child, on every shape:
    the text is byte-identical, through the real child process."""

    @real_child
    @pytest.mark.parametrize("shape", sorted(_SHAPES))
    def test_attachment_text_is_identical(self, shape):
        payload = _SHAPES[shape]
        assert html.extract(payload) == (_attachment_in_process(payload), "html")

    @real_child
    @pytest.mark.parametrize("shape", sorted(_SHAPES))
    def test_body_text_is_identical(self, shape, tmp_path):
        source = _SHAPES[shape]
        msg, _ = _parse(tmp_path, _message(("text/html; charset=utf-8", source)))
        assert msg.body_text == _expected_body(source)
        assert msg.parse_caps == {}
        assert msg.body_complete is True

    @real_child
    def test_every_shape_in_one_message_is_identical_in_order(self, tmp_path):
        """One child converts every HTML part of the message; their texts
        come back in document order."""
        sources = [_SHAPES[s] for s in sorted(_SHAPES)]
        before = _runner.process_launches()
        msg, _ = _parse(tmp_path, _message(*(("text/html; charset=utf-8", s) for s in sources)))
        assert _runner.process_launches() - before == 1
        assert msg.body_text == _expected_body(*sources)
        assert msg.parse_caps == {}

    @real_child
    @pytest.mark.parametrize(
        ("charset", "data"),
        [
            ("iso-8859-1", "<p>café naïve</p>".encode("latin-1")),
            ("windows-1252", "<p>“quoted” €</p>".encode("cp1252")),
            ("shift_jis", "<p>日本語</p>".encode("shift_jis")),
            ("utf-16", "<p>sixteen</p>".encode("utf-16")),
            ("x-unknown-label", b"<p>fallback \xe9</p>"),
        ],
    )
    def test_body_charsets_are_identical(self, charset, data, tmp_path):
        """The parser decodes the part with its charset; the decoded text
        crosses to the child unchanged."""
        msg, _ = _parse(tmp_path, _message((f"text/html; charset={charset}", data)))
        decoded = parser._safe_decode(data, charset)
        assert msg.body_text == _in_process(decoded).strip()

    @real_child
    def test_a_lone_surrogate_crosses_as_a_question_mark(self, tmp_path, caplog):
        """A UTF-7 part can decode to a lone surrogate, which UTF-8 cannot
        carry: it crosses as ``?``. In process the surrogate stayed in the
        body, whose UTF-8 encode (the chunker's) then failed. The
        replacement is logged with its count (review round 1); like any
        decoder replacement it does not mark the body incomplete (#1315)."""
        caplog.set_level("DEBUG")
        data = b"<p>a +2DQ- b +2DQ-</p><p>" + MARKER.encode() + b"</p>"
        decoded = parser._safe_decode(data, "utf-7")
        assert "\ud834" in decoded
        with pytest.raises(UnicodeEncodeError):
            _in_process(decoded).encode("utf-8")
        msg, _ = _parse(tmp_path, _message(("text/html; charset=utf-7", data)))
        assert msg.body_text.startswith("a ? b ?")
        assert msg.parse_caps == {}
        assert msg.body_complete is True
        lines = [
            (r.levelname, r.getMessage()) for r in caplog.records if r.name == "indexer.parser"
        ]
        assert ("WARNING", "HTML body text held 2 lone surrogates; converted as ?") in lines
        assert MARKER not in caplog.text

    def test_no_surrogate_no_line(self, tmp_path, caplog):
        caplog.set_level("DEBUG")
        _parse(tmp_path, _message(("text/html", b"<p>plain</p>")))
        assert "lone surrogates" not in caplog.text

    @pytest.mark.parametrize(
        ("text", "expected", "count"),
        [
            ("", "", 0),
            ("abc \U0001f600", "abc \U0001f600", 0),
            ("\ud800", "?", 1),
            ("a\udfffb\ud834", "a?b?", 2),
            ("\ud83d\ude00", "??", 2),
        ],
    )
    def test_lone_surrogates_are_replaced_and_counted(self, text, expected, count):
        assert html.replace_lone_surrogates(text) == (expected, count)


class TestOneChildPerMessage:
    def test_a_message_without_html_starts_no_child(self, tmp_path, monkeypatch):
        calls = stub_child_output(monkeypatch, b"T 0\n")
        msg, _ = _parse(tmp_path, _message(plain="plain only"))
        assert msg.body_text == "plain only"
        assert calls == []

    def test_all_html_parts_go_to_one_child_with_their_lengths(self, tmp_path, monkeypatch):
        seen: list[tuple[list[str], bytes]] = []
        real = _runner.run_tool

        def run_tool(argv, payload, **kwargs):
            seen.append((argv, payload))
            return real(argv, payload, **kwargs)

        monkeypatch.setattr(_runner, "run_tool", run_tool)
        sources = [b"<p>one</p>", b"<p>two \xc3\xa9</p>", b""]
        msg, _ = _parse(tmp_path, _message(*(("text/html; charset=utf-8", s) for s in sources)))
        assert msg.body_text == "one\n\ntwo é"
        [(argv, payload)] = seen
        assert argv[-4:] == ["html", "10", "13", "0"]
        assert payload == b"".join(sources)

    def test_the_child_runs_under_the_html_limits(self, tmp_path, monkeypatch):
        calls = stub_child_output(monkeypatch, b"T 5\n3:abc")
        msg, _ = _parse(tmp_path, _message(("text/html", b"<p>abc</p>")))
        assert msg.body_text == "abc"
        [call] = calls
        assert {k: v for k, v in call.items() if k != "argv"} == {
            "timeout_seconds": html.CHILD_TIMEOUT_SECONDS,
            "max_output_bytes": html._MAX_OUTPUT_BYTES,
            "max_address_space_bytes": html.CHILD_MAX_ADDRESS_SPACE_BYTES,
            "max_cpu_seconds": html.CHILD_MAX_CPU_SECONDS,
            "suffix": ".html",
        }
        # Past the CPU limit, so a CPU-bound child meets that first.
        assert html.CHILD_TIMEOUT_SECONDS > html.CHILD_MAX_CPU_SECONDS

    def test_an_attached_emails_html_converts_in_the_eml_child_only(self, monkeypatch):
        """The attached-email extractor's body walk already runs in the
        eml child under its limits: it converts there, starting no HTML
        child of its own."""
        monkeypatch.setattr(html, "run_child", None)
        payload = (
            b"Subject: s\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>inner text</p>\r\n"
        )
        text = extractor_child.run("eml", payload)
        assert text.endswith(b"Subject: s\n\ninner text")


class TestBodyDegradation:
    """A conversion the child cannot finish leaves the HTML parts without
    text, counted as the ``html_body`` parse cap, with the failure's type
    on a WARNING; the rest of the message is indexed."""

    @pytest.mark.parametrize(
        ("error", "reason"),
        [
            (ToolCrashError, "ToolCrashError"),
            (ToolExitError, "ToolExitError"),
            (ToolTimeoutError, "ToolTimeoutError"),
        ],
    )
    def test_a_runner_failure_degrades_the_body(self, error, reason, tmp_path, monkeypatch, caplog):
        def fail(*_a, **_k):
            raise error

        monkeypatch.setattr(_runner, "run_tool", fail)
        self._assert_degraded(tmp_path, caplog, reason)

    @pytest.mark.parametrize(
        ("output", "reason"),
        [
            (b"E MemoryError\n", "MemoryError"),
            (b"E RecursionError\n", "RecursionError"),
            (b"E AssertionError\n", "AssertionError"),
            (b"T 3\nabc", "ChildOutputError"),
            (b"X\n", "ChildOutputError"),
        ],
    )
    def test_a_child_error_or_broken_output_degrades_the_body(
        self, output, reason, tmp_path, monkeypatch, caplog
    ):
        stub_child_output(monkeypatch, output)
        self._assert_degraded(tmp_path, caplog, reason)

    def test_cut_output_degrades_the_body(self, tmp_path, monkeypatch, caplog):
        stub_child_output(monkeypatch, b"T 5\n3:abc", truncated=True)
        self._assert_degraded(tmp_path, caplog, "ChildOutputError")

    @staticmethod
    def _assert_degraded(tmp_path, caplog, reason):
        caplog.set_level("DEBUG")
        raw = _message(("text/html", f"<p>{MARKER}</p>".encode()), plain="kept plain text")
        msg, path = _parse(tmp_path, raw)
        assert msg.body_text == "kept plain text"
        assert msg.parse_caps == {"html_body": 1}
        assert msg.body_complete is False
        lines = [
            (r.levelname, r.getMessage()) for r in caplog.records if r.name == "indexer.parser"
        ]
        assert lines == [
            (
                "WARNING",
                f"HTML body conversion failed ({reason}); the text of 1 HTML parts is left out",
            ),
            ("WARNING", f"parser work caps dropped content from {path}: html_body=1"),
        ]
        assert MARKER not in caplog.text

    def test_a_part_the_body_would_not_keep_is_not_counted(self, tmp_path, monkeypatch, caplog):
        """An HTML alternative after a plain one with text is not a loss:
        the failure is logged, no cap is counted."""
        caplog.set_level("DEBUG")
        stub_child_output(monkeypatch, b"E MemoryError\n")
        raw = (
            b"Message-ID: <alt@example.test>\r\nFrom: sender@example.test\r\n"
            b'MIME-Version: 1.0\r\nContent-Type: multipart/alternative; boundary="a"\r\n\r\n'
            b"--a\r\nContent-Type: text/plain\r\n\r\nplain body\r\n"
            b"--a\r\nContent-Type: text/html\r\n\r\n<p>" + MARKER.encode() + b"</p>\r\n--a--\r\n"
        )
        msg, _ = _parse(tmp_path, raw)
        assert msg.body_text == "plain body"
        assert msg.parse_caps == {}
        assert msg.body_complete is True
        assert "HTML body conversion failed (MemoryError)" in caplog.text
        assert "parser work caps" not in caplog.text

    def test_an_html_only_alternative_is_counted(self, tmp_path, monkeypatch):
        stub_child_output(monkeypatch, b"E MemoryError\n")
        raw = (
            b"Message-ID: <alt@example.test>\r\nFrom: sender@example.test\r\n"
            b'MIME-Version: 1.0\r\nContent-Type: multipart/alternative; boundary="a"\r\n\r\n'
            b"--a\r\nContent-Type: text/plain\r\n\r\n \r\n"
            b"--a\r\nContent-Type: text/html\r\n\r\n<p>x</p>\r\n"
            b"--a\r\nContent-Type: text/html\r\n\r\n<p>y</p>\r\n--a--\r\n"
        )
        msg, _ = _parse(tmp_path, raw)
        assert msg.body_text == ""
        # The first HTML alternative would have been chosen; the second not.
        assert msg.parse_caps == {"html_body": 1}

    def test_the_text_budget_cuts_one_part_and_skips_the_rest(self, tmp_path, monkeypatch, caplog):
        """The budget cuts the part that crossed it; the parts after it
        are never converted (the work asserted) and have no text."""
        caplog.set_level("DEBUG")
        converted: list[str] = []
        real = html.html_to_text

        def convert(source: str) -> str:
            converted.append(source)
            return real(source)

        monkeypatch.setattr(html, "html_to_text", convert)
        monkeypatch.setattr(html, "_MAX_TEXT_CHARS", 12)
        sources = [b"<p>abcdefgh</p>", b"<p>ijklmnop</p>", b"<p>" + MARKER.encode() + b"</p>"]
        msg, path = _parse(tmp_path, _message(*(("text/html", s) for s in sources)))
        assert len(converted) == 2
        # "abcdefgh\n" is nine characters; three are left for the second.
        assert msg.body_text == "abcdefgh\n\nijk"
        assert msg.parse_caps == {"html_body": 2}
        assert msg.body_complete is False
        assert (
            "HTML body conversion stopped at 12 chars; 2 of 3 HTML parts cut or left out"
            in caplog.text
        )
        assert f"parser work caps dropped content from {path}: html_body=2" in caplog.text
        assert MARKER not in caplog.text

    def test_failure_lines_are_rate_limited(self, tmp_path, monkeypatch, caplog):
        caplog.set_level("DEBUG")
        stub_child_output(monkeypatch, b"E MemoryError\n")
        monkeypatch.setattr(extractors._LINE_BUDGET, "limit", 2)
        extractors.drain_suppressed_lines()
        for i in range(4):
            _parse(tmp_path, _message(("text/html", b"<p>x</p>"), plain="p"), f"{i}.eml")
        failed = [r for r in caplog.records if "HTML body conversion failed" in r.getMessage()]
        assert len(failed) <= 2
        assert extractors.drain_suppressed_lines() > 0

    def test_a_launch_failure_propagates_so_the_queue_retries(self, tmp_path, monkeypatch):
        """No process or scratch space left is the host's trouble, not the
        message's: the parse raises, as an ``OSError`` did before."""

        def fail(*_a, **_k):
            raise OSError(11, "Resource temporarily unavailable")

        monkeypatch.setattr(_runner, "run_tool", fail)
        folder = tmp_path / "INBOX" / "cur"
        folder.mkdir(parents=True)
        path = folder / "m.eml"
        path.write_bytes(_message(("text/html", b"<p>x</p>")))
        with pytest.raises(OSError):
            parse_email(path)


class TestLimitHit:
    @real_child
    def test_an_oversized_body_hits_the_cpu_limit_as_a_recorded_degradation(
        self, tmp_path, monkeypatch, caplog
    ):
        """A synthetic HTML body whose conversion needs far more CPU time
        than the child may use: the child is killed at its CPU limit, one
        child was started, the parse finishes within seconds of the limit
        (no stall), and the loss is recorded with fixed text and counts."""
        caplog.set_level("DEBUG")
        monkeypatch.setattr(html, "CHILD_MAX_CPU_SECONDS", 1)
        # About ten seconds of html2text, unclosed tags being its slowest
        # markup per byte (docs/architecture.md, "HTML conversion").
        source = (MARKER + " <b>x ").encode() * 600_000
        before = _runner.process_launches()
        started = time.monotonic()
        msg, path = _parse(tmp_path, _message(("text/html", source), plain="kept"))
        elapsed = time.monotonic() - started
        assert _runner.process_launches() - before == 1
        assert elapsed < 8
        assert msg.body_text == "kept"
        assert msg.parse_caps == {"html_body": 1}
        assert msg.body_complete is False
        assert "HTML body conversion failed (ToolCrashError)" in caplog.text
        assert f"parser work caps dropped content from {path}: html_body=1" in caplog.text
        assert MARKER not in caplog.text

    @real_child
    @linux_only
    def test_an_oversized_body_hits_the_address_space_limit(self, tmp_path, monkeypatch, caplog):
        """The same for memory: a body whose conversion needs more address
        space than the child may map fails in the child, reported by type."""
        caplog.set_level("DEBUG")
        monkeypatch.setattr(html, "CHILD_MAX_ADDRESS_SPACE_BYTES", 256 * 1024 * 1024)
        source = b"<hr>" * 10_000_000
        msg, _ = _parse(tmp_path, _message(("text/html", source), plain="kept"))
        assert msg.body_text == "kept"
        assert msg.parse_caps == {"html_body": 1}
        assert any(
            f"HTML body conversion failed ({reason})" in caplog.text
            for reason in ("MemoryError", "ToolExitError", "ToolCrashError")
        )


class TestAttachment:
    def test_a_child_failure_is_a_failed_row(self, monkeypatch, caplog):
        caplog.set_level("DEBUG")
        stub_child_output(monkeypatch, b"E MemoryError\n")
        result = extract(content_type="text/html", filename="a.html", payload=MARKER.encode())
        assert (result.status, result.error, result.text) == (STATUS_FAILED, "MemoryError", None)
        assert MARKER not in caplog.text

    @pytest.mark.parametrize("error", [ToolCrashError, ToolExitError, ToolTimeoutError])
    def test_a_runner_failure_is_a_failed_row(self, error, monkeypatch):
        def fail(*_a, **_k):
            raise error

        monkeypatch.setattr(_runner, "run_tool", fail)
        result = extract(content_type="text/html", filename="a.html", payload=b"<p>x</p>")
        assert (result.status, result.error) == (STATUS_FAILED, error.__name__)

    def test_the_dispatcher_stores_the_childs_text(self):
        result = extract(
            content_type="text/html", filename="a.html", payload=b"<p>caf\xc3\xa9 text</p>"
        )
        assert (result.status, result.text, result.text_complete) == (
            STATUS_SUCCESS,
            "café text",
            True,
        )


class TestFraming:
    @pytest.mark.parametrize(
        ("framed", "count", "expected"),
        [
            ("", 0, []),
            ("0:", 1, [""]),
            ("3:abc", 1, ["abc"]),
            ("3:a:c2:é\U0001f600", 2, ["a:c", "é\U0001f600"]),
            ("1:\n0:", 2, ["\n", ""]),
        ],
    )
    def test_texts_split_back(self, framed, count, expected):
        assert html.split_texts(framed, count) == expected

    @pytest.mark.parametrize(
        ("framed", "count"),
        [
            pytest.param("abc", 1, id="no-prefix"),
            pytest.param(":abc", 1, id="empty-prefix"),
            pytest.param("x:abc", 1, id="prefix-not-digits"),
            pytest.param("-1:", 1, id="negative"),
            pytest.param("٣:abc", 1, id="non-ascii-digit"),
            pytest.param("4:abc", 1, id="past-the-end"),
            pytest.param("1:a1:b", 1, id="more-texts-than-documents"),
            pytest.param("9" * 19 + ":", 1, id="prefix-too-long"),
            pytest.param("3 :abc", 1, id="space-in-prefix"),
        ],
    )
    def test_broken_framing_is_child_output_error(self, framed, count):
        with pytest.raises(ChildOutputError):
            html.split_texts(framed, count)

    @pytest.mark.parametrize(
        ("output", "documents"),
        [
            pytest.param(b"T 3\n1:a", 2, id="fewer-texts-without-the-cap"),
            pytest.param(b"C html_text_chars\nT 0\n", 1, id="cut-to-no-text"),
            pytest.param(b"T 0\n", 1, id="no-text-for-a-document"),
        ],
    )
    def test_a_text_count_that_does_not_match_is_child_output_error(
        self, output, documents, monkeypatch
    ):
        stub_child_output(monkeypatch, output)
        with pytest.raises(ChildOutputError):
            html.convert([b"x"] * documents)

    def test_a_cut_run_may_return_fewer_texts(self, monkeypatch):
        stub_child_output(monkeypatch, b"C html_text_chars\nT 3\n1:a")
        assert html.convert([b"x", b"y"]) == (["a"], True)


class TestChildArguments:
    """The child's one argument table (``html_child._document_sizes``),
    walked: count, form and the sum against the payload, each reported by
    type only."""

    @pytest.mark.parametrize(
        ("payload", "lengths"),
        [
            pytest.param(b"", ("0",), id="one-empty-document"),
            pytest.param(b"ab", ("2",), id="one-document"),
            pytest.param(b"ab", ("0", "2", "0"), id="empty-documents-around"),
            pytest.param(b"x" * 200, ("1",) * 200, id="max-documents"),
        ],
    )
    def test_valid_arguments(self, payload, lengths):
        text, caps = html_child.extract_text(payload, *lengths)
        assert caps == []
        assert len(html.split_texts(text, len(lengths))) == len(lengths)

    @pytest.mark.parametrize(
        ("payload", "lengths"),
        [
            pytest.param(b"", (), id="no-documents"),
            pytest.param(b"x" * 201, ("1",) * 201, id="over-max-documents"),
            pytest.param(b"ab", ("-2",), id="negative"),
            pytest.param(b"ab", ("+2",), id="signed"),
            pytest.param(b"ab", ("2.0",), id="not-an-integer"),
            pytest.param(b"ab", ("two",), id="not-a-number"),
            pytest.param(b"ab", ("٢",), id="non-ascii-digit"),
            pytest.param(b"ab", (" 2",), id="space"),
            pytest.param(b"ab", ("",), id="empty"),
            pytest.param(b"ab", ("1",), id="sum-below-payload"),
            pytest.param(b"ab", ("3",), id="sum-above-payload"),
            pytest.param(b"ab", ("1", "2"), id="sum-of-several-above-payload"),
        ],
    )
    def test_invalid_arguments_are_reported_by_type(self, payload, lengths):
        with pytest.raises(ValueError):
            html_child.extract_text(payload, *lengths)
        assert extractor_child.run("html", payload, lengths) == b"E ValueError\n"

    def test_the_document_limit_is_the_parsers_text_part_cap(self):
        assert html_child._MAX_DOCUMENTS == parser.MAX_BODY_TEXT_PARTS


class TestChildCost:
    @real_child
    def test_one_child_per_message_whatever_its_html_parts(self, tmp_path):
        """The cost the issue asked about: one launch per message with HTML
        (``docs/architecture.md`` has the measured time), not one per part."""
        before = _runner.process_launches()
        _parse(tmp_path, _message(*(("text/html", b"<p>p%d</p>" % i) for i in range(20))))
        assert _runner.process_launches() - before == 1


@pytest.fixture(autouse=True)
def _restore_counters():
    extractors.drain_extractor_counts()
    yield
    extractors.drain_extractor_counts()


def test_the_output_cap_holds_the_text_budget():
    """Four bytes a character plus the length prefixes of the most
    documents the child takes."""
    prefixes = html_child._MAX_DOCUMENTS * (len(str(html._MAX_TEXT_CHARS)) + 1)
    assert html._MAX_OUTPUT_BYTES >= 4 * html._MAX_TEXT_CHARS + prefixes + 64


def test_stubbed_output_shape_is_the_childs(monkeypatch):
    """What the child writes for two documents parses back through the
    runner to the same texts (the frames ``stub_child_output`` feeds)."""
    output = extractor_child.run("html", b"<p>a</p><p>b</p>", ["8", "8"])
    stub_child_output(monkeypatch, output)
    assert html.convert([b"<p>a</p>", b"<p>b</p>"]) == (["a\n", "b\n"], False)
