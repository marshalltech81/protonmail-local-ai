"""Attached-email extractor (#922).

Payloads for the end-to-end cases come from the parser itself
(``parse_email_bytes`` on a synthetic outer message), so the extractor
gets what the indexer gives it. The budget cases run the child's
extraction in this process (``_in_process_child``) so a budget can be
patched; the rest run the real child.
"""

from __future__ import annotations

import base64
import binascii
import email
import logging
from collections import Counter

import pytest
from src import extractors, parser
from src.extractors import (
    EXTRACTOR_VERSIONS,
    STATUS_FAILED,
    STATUS_SUCCESS,
    STATUS_UNSUPPORTED,
    _runner,
    eml,
    extract,
    extractor_child,
)

MARKER = "SYNTHETIC_EML_MARKER"
HDR = b"Subject: s\r\nFrom: a@example.test\r\n"


def _outer(inner: bytes, content_type: str = "message/rfc822", filename: str = "f.eml") -> bytes:
    return (
        b"Message-ID: <outer@example.test>\r\nSubject: outer\r\nFrom: o@example.test\r\n"
        b'Content-Type: multipart/mixed; boundary="OUTER"\r\n\r\n'
        b"--OUTER\r\nContent-Type: text/plain\r\n\r\nouter body\r\n"
        b"--OUTER\r\nContent-Type: " + content_type.encode() + b"\r\n"
        b"Content-Disposition: attachment; filename="
        + filename.encode()
        + b"\r\n\r\n"
        + inner
        + b"\r\n--OUTER--\r\n"
    )


def _parsed(raw: bytes) -> parser.Message:
    source = parser.SourceMetadata(
        folder="INBOX", flags=frozenset(), size=len(raw), mtime_ns=0, path="synthetic"
    )
    msg = parser.parse_email_bytes(raw, source)
    assert msg is not None
    return msg


def _extract(att: parser.Attachment):
    return extract(content_type=att.content_type, filename=att.filename, payload=att.payload)


def _multipart(*parts: bytes, headers: bytes = HDR, boundary: bytes = b"B") -> bytes:
    body = b"".join(b"--" + boundary + b"\r\n" + p + b"\r\n" for p in parts)
    return (
        headers
        + b'Content-Type: multipart/mixed; boundary="'
        + boundary
        + b'"\r\n\r\n'
        + body
        + b"--"
        + boundary
        + b"--\r\n"
    )


def _attached(inner: bytes, *, encoding: bytes | None = None) -> bytes:
    """A ``message/rfc822`` attachment part carrying ``inner``."""
    head = b"Content-Type: message/rfc822\r\nContent-Disposition: attachment\r\n"
    if encoding == b"base64":
        return head + b"Content-Transfer-Encoding: base64\r\n\r\n" + base64.encodebytes(inner)
    return head + b"\r\n" + inner


def _text(body: str, *, subject: str = "n") -> bytes:
    return f"Subject: {subject}\r\nContent-Type: text/plain\r\n\r\n{body}\r\n".encode()


_INNER = (
    b"Subject: =?utf-8?q?inner_SYNTHETIC=5FEML=5FMARKER_subject?=\r\n"
    b"From: Alice <alice@example.test>\r\nTo: bob@example.test\r\nCc: carol@example.test\r\n"
    b"Date: Mon, 5 Oct 2026 10:00:00 +0000\r\n"
    b'Content-Type: multipart/mixed; boundary="IN"\r\n\r\n'
    b"--IN\r\nContent-Type: text/plain\r\n\r\n> quoted line\r\ninner body "
    + MARKER.encode()
    + b"\r\n"
    b"--IN\r\nContent-Type: text/plain\r\nContent-Disposition: attachment; filename=n.txt\r\n\r\n"
    b"nested attachment SYNTHETIC_NESTED_FILE\r\n--IN--\r\n"
)


@pytest.fixture
def _in_process_child(monkeypatch):
    """Run the child's extraction in this process, through the real
    framed protocol and parent."""

    def run_tool(_argv, payload, *, on_output, **_kwargs):
        on_output(extractor_child.run("eml", payload))
        return _runner.ToolOutput(b"", truncated=False)

    monkeypatch.setattr(_runner, "run_tool", run_tool)


class TestDispatch:
    @pytest.mark.parametrize(
        ("content_type", "filename"),
        [
            ("message/rfc822", "f.eml"),
            ("message/rfc822", "unnamed"),
            ("application/eml", "f"),
            ("application/octet-stream", "f.EML"),
        ],
    )
    def test_attached_emails_select_the_eml_extractor(self, content_type, filename):
        assert extractors.resolved_extractor_module(content_type, filename) == "eml"
        result = extract(content_type=content_type, filename=filename, payload=_text("x"))
        assert result.status == STATUS_SUCCESS
        assert result.extractor == f"eml@{EXTRACTOR_VERSIONS['eml']}"
        assert EXTRACTOR_VERSIONS["eml"] == 1

    @pytest.mark.parametrize(
        ("content_type", "filename", "reruns"),
        [
            ("message/rfc822", "f.eml", True),
            ("message/rfc822", "unnamed", True),
            ("application/eml", "f", True),
            ("application/octet-stream", "f.eml", True),
            ("message/delivery-status", "unnamed", False),
        ],
    )
    def test_the_startup_sweep_requeues_cached_no_extractor_rows(
        self, content_type, filename, reruns
    ):
        """No version bump: occurrences cached "no extractor" before #922
        are re-queued by the startup sweep's "no extractor" predicate,
        once, as for ``pptx`` 1."""
        from src.attachment_indexing import NO_EXTRACTOR_MODULE, reprocess_reruns_extraction

        assert (
            reprocess_reruns_extraction(
                extractors.NO_EXTRACTOR_ERROR, NO_EXTRACTOR_MODULE, content_type, filename
            )
            is reruns
        )

    def test_delivery_status_stays_unsupported(self):
        payload = b"Reporting-MTA: dns; example.test\n\nFinal-Recipient: rfc822; a@example.test\n"
        assert extractors.resolved_extractor_module("message/delivery-status", "unnamed") is None
        result = extract(
            content_type="message/delivery-status", filename="unnamed", payload=payload
        )
        assert result.status == STATUS_UNSUPPORTED
        assert result.error == extractors.NO_EXTRACTOR_ERROR

    def test_the_extraction_runs_in_the_child_under_its_limits(self, monkeypatch):
        calls = []

        def run_child(module, payload, **kwargs):
            calls.append((module, payload, kwargs))
            return _runner.ChildResult("text", [], {})

        monkeypatch.setattr(_runner, "run_child", run_child)
        assert eml.extract(b"payload") == ("text", "eml")
        [(module, payload, kwargs)] = calls
        assert (module, payload) == ("eml", b"payload")
        assert kwargs["max_address_space_bytes"] == eml.CHILD_MAX_ADDRESS_SPACE_BYTES
        assert kwargs["max_cpu_seconds"] == eml.CHILD_MAX_CPU_SECONDS
        assert kwargs["timeout_seconds"] > eml.CHILD_MAX_CPU_SECONDS
        assert kwargs["max_output_bytes"] == eml._MAX_OUTPUT_BYTES
        assert kwargs["caps"] == eml._CAP_NAMES


class TestText:
    @pytest.mark.parametrize(
        ("content_type", "filename"), [("message/rfc822", "f.eml"), ("application/eml", "f")]
    )
    def test_headers_then_body_from_the_parsers_payload(self, content_type, filename, caplog):
        msg = _parsed(_outer(_INNER, content_type, filename))
        att = next(a for a in msg.attachments if a.content_type == content_type)
        with caplog.at_level(logging.DEBUG):
            result = _extract(att)
        assert result.status == STATUS_SUCCESS
        assert result.text_complete is True
        # A leaf ``.eml`` keeps the line ends it was sent with.
        assert result.text is not None
        assert result.text.replace("\r", "") == (
            f"Subject: inner {MARKER} subject\n"
            "From: Alice <alice@example.test>\n"
            "To: bob@example.test\n"
            "Cc: carol@example.test\n"
            "Date: Mon, 5 Oct 2026 10:00:00 +0000\n"
            f"\n> quoted line\ninner body {MARKER}"
        )
        # Its own attachment is not extracted here. Inside a
        # ``message/rfc822`` part the parser records it as an occurrence of
        # its own; inside an ``.eml`` leaf it is not walked at all.
        assert "SYNTHETIC_NESTED_FILE" not in result.text
        walked = any(a.filename == "n.txt" for a in msg.attachments)
        assert walked == (content_type == "message/rfc822")
        assert MARKER not in caplog.text

    def test_nested_attached_emails_render_depth_first_with_labels(self):
        grandchild = _text("grandchild body")
        child_one = _multipart(
            _text("child one body", subject="c1").replace(b"Subject: c1\r\n", b""),
            _attached(grandchild),
            headers=b"Subject: c1\r\n",
            boundary=b"C1",
        )
        child_two = _text("child two body", subject="c2")
        inner = _multipart(
            b"Content-Type: text/plain\r\n\r\nroot body",
            _attached(child_one),
            _attached(child_two, encoding=b"base64"),
            # A nested .eml file is a leaf attachment: its own occurrence.
            b"Content-Type: application/octet-stream\r\n"
            b"Content-Disposition: attachment; filename=leaf.eml\r\n\r\n" + _text("LEAF_BODY"),
        )
        msg = _parsed(_outer(inner))
        att = msg.attachments[0]
        assert att.content_type == "message/rfc822"
        assert any(a.filename == "leaf.eml" for a in msg.attachments)
        result = _extract(att)
        assert result.status == STATUS_SUCCESS
        assert result.text_complete is True
        assert result.text == (
            "Subject: s\nFrom: a@example.test\n\nroot body"
            "\n\n[Attached message, depth 2]\nSubject: c1\n\nchild one body"
            "\n\n[Attached message, depth 3]\nSubject: n\n\ngrandchild body"
            "\n\n[Attached message, depth 2]\nSubject: c2\n\nchild two body"
        )
        assert "LEAF_BODY" not in result.text

    def test_a_raw_eml_file_is_read_as_sent(self):
        result = extract(
            content_type="application/octet-stream",
            filename="x.eml",
            payload=_multipart(b"Content-Type: text/html\r\n\r\n<p>html body</p>"),
        )
        assert result.text == "Subject: s\nFrom: a@example.test\n\nhtml body"


class TestBudgets:
    """Each budget is shared by every message of one payload, and a cut
    is reported (WARNING, counted) and marks the text incomplete."""

    def _cut(self, payload: bytes, caplog) -> extractors.ExtractionResult:
        extractors.drain_extractor_counts()
        with caplog.at_level(logging.WARNING):
            result = extract(content_type="message/rfc822", filename="f.eml", payload=payload)
        assert result.status == STATUS_SUCCESS
        assert result.text_complete is False
        assert extractors.drain_extractor_counts()["extractor_caps"] >= 1
        assert MARKER not in caplog.text
        return result

    def test_parts_are_shared_across_nested_messages(self, monkeypatch, caplog, _in_process_child):
        walked = []
        real = parser._part_filename
        monkeypatch.setattr(parser, "_part_filename", lambda p, *a: walked.append(1) or real(p, *a))
        monkeypatch.setattr(eml, "_MAX_PARTS", 6)
        nested = _multipart(
            *(b"Content-Type: text/plain\r\n\r\n" + MARKER.encode(),) * 3, boundary=b"N"
        )
        payload = _multipart(*(_attached(nested),) * 4)
        result = self._cut(payload, caplog)
        assert len(walked) == 6
        assert "extractor cap eml_parts:" in caplog.text
        assert result.text is not None

    def test_text_parts_are_shared_across_nested_messages(
        self, monkeypatch, caplog, _in_process_child
    ):
        monkeypatch.setattr(eml, "_MAX_TEXT_PARTS", 2)
        decoded = []
        real = parser._safe_decode
        monkeypatch.setattr(
            parser, "_safe_decode", lambda b, c, *a: decoded.append(1) or real(b, c, *a)
        )
        payload = _multipart(
            b"Content-Type: text/plain\r\n\r\none",
            _attached(_text("two")),
            _attached(_text("three " + MARKER)),
        )
        result = self._cut(payload, caplog)
        assert len(decoded) == 2
        assert result.text is not None and "three" not in result.text
        assert "extractor cap eml_text_parts:" in caplog.text

    def test_nesting_past_the_depth_cap_is_cut(self, monkeypatch, caplog, _in_process_child):
        monkeypatch.setattr(eml, "_MAX_DEPTH", 3)
        inner = _text("depth four " + MARKER)
        for level in range(3):
            inner = _multipart(_attached(inner), boundary=b"L%d" % level)
        result = self._cut(inner, caplog)
        assert result.text is not None
        assert "[Attached message, depth 3]" in result.text
        assert "depth 4" not in result.text
        assert "extractor cap eml_nested_messages:" in caplog.text

    def test_transfer_decoded_bytes_are_shared(self, monkeypatch, caplog, _in_process_child):
        one = _attached(_text("first"), encoding=b"base64")
        monkeypatch.setattr(eml, "_MAX_DECODED_BYTES", len(base64.encodebytes(_text("first"))) + 20)
        payload = _multipart(one, _attached(_text("second " + MARKER), encoding=b"base64"))
        result = self._cut(payload, caplog)
        assert result.text is not None and "first" in result.text and "second" not in result.text
        assert "extractor cap eml_nested_messages:" in caplog.text

    def test_an_undecodable_nested_email_is_a_cut(self, caplog, _in_process_child):
        bad = (
            b"Content-Type: message/rfc822\r\nContent-Disposition: attachment\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n" + b"!!!not base64" + MARKER.encode()
        )
        result = self._cut(_multipart(b"Content-Type: text/plain\r\n\r\nroot", bad), caplog)
        assert result.text == "Subject: s\nFrom: a@example.test\n\nroot"
        assert "extractor cap eml_nested_messages:" in caplog.text

    def test_a_long_header_is_cut_before_decoding(self, caplog, _in_process_child):
        payload = b"Subject: " + b"s" * 50_000 + MARKER.encode() + b"\r\n\r\nbody\r\n"
        result = self._cut(payload, caplog)
        assert result.text == "Subject: " + "s" * eml._MAX_HEADER_CHARS + "\n\nbody"
        assert "extractor cap eml_header_chars:" in caplog.text

    def test_text_is_cut_at_its_budget(self, monkeypatch, caplog, _in_process_child):
        monkeypatch.setattr(eml, "_MAX_TEXT_CHARS", 30)
        result = self._cut(_text("x" * 100 + MARKER), caplog)
        assert result.text is not None and len(result.text) <= 30
        assert "extractor cap eml_text_chars:" in caplog.text

    def test_only_the_first_of_each_header_is_read(self):
        payload = b"Subject: first\r\nSubject: second\r\n\r\nbody\r\n"
        result = extract(content_type="message/rfc822", filename="f.eml", payload=payload)
        assert result.text == "Subject: first\n\nbody"
        assert result.text_complete is True


class TestChildFailures:
    def test_nesting_past_the_recursion_limit_is_a_failed_row(self, caplog):
        """The standard library's parse recurses per level (about 1,000
        levels, measured): in the child that is a per-payload failure."""
        inner = b"Subject: " + MARKER.encode() + b"\r\n\r\nleaf\r\n"
        for _ in range(3_000):
            inner = b"Content-Type: message/rfc822\r\n\r\n" + inner
        with caplog.at_level(logging.WARNING):
            result = extract(
                content_type="application/octet-stream", filename="x.eml", payload=inner
            )
        assert (result.status, result.error) == (STATUS_FAILED, "RecursionError")
        assert result.text_complete is False
        assert "extractor eml failed (dispatch_via=extension): RecursionError" in caplog.text
        assert MARKER not in caplog.text

    @pytest.mark.parametrize("error", [MemoryError, RecursionError])
    def test_resource_errors_in_the_child_are_failed_rows(
        self, error, monkeypatch, _in_process_child
    ):
        def boom(_payload):
            raise error

        monkeypatch.setattr(eml, "extract_text", boom)
        result = extract(content_type="message/rfc822", filename="f.eml", payload=_text("x"))
        assert (result.status, result.error) == (STATUS_FAILED, error.__name__)


class TestBodyOnlyWalk:
    """``parser.BodyWalk``: attachments are neither materialized nor
    walked into. The default walk is unchanged (the parser pin)."""

    def test_attachments_are_not_materialized_or_walked_into(self, monkeypatch):
        def refuse(*_args, **_kwargs):
            raise AssertionError("attachment payload materialized")

        monkeypatch.setattr(parser, "_attachment_payload", refuse)
        bundle = _multipart(
            *(b"Content-Type: text/plain\r\n\r\n" + MARKER.encode(),) * 50,
            headers=b"Content-Disposition: attachment; filename=b.bin\r\n",
            boundary=b"Z",
        )
        root = email.message_from_bytes(_multipart(b"Content-Type: text/plain\r\n\r\nroot", bundle))
        walk = parser.BodyWalk()
        body, attachments = parser._extract_body_and_attachments(root, walk=walk)
        assert (body, attachments, walk.nested) == ("root", [], [])
        # The root, its text part and the bundle; none of the bundle's 50.
        assert walk.parts_left == parser.MAX_WALKED_PARTS - 3
        assert walk.text_parts_left == parser.MAX_BODY_TEXT_PARTS - 1


class TestReviewRound1:
    """Codex round 1 on #1311."""

    def test_a_lossy_base64_nested_email_is_rendered_and_cut(
        self, monkeypatch, caplog, _in_process_child
    ):
        """A nested email whose base64 drops a quantum still decodes and
        is rendered, but the text is not whole: an ``eml_nested_messages``
        cap, so the result is incomplete."""
        checked = []
        real = parser._base64_transport_lost
        monkeypatch.setattr(
            parser, "_base64_transport_lost", lambda data: checked.append(1) or real(data)
        )
        encoded = base64.encodebytes(_text("intact words " + MARKER * 3))
        lossy = encoded[:40] + b"!!!!" + encoded[44:]
        part = (
            b"Content-Type: message/rfc822\r\nContent-Disposition: attachment\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n" + lossy
        )
        with caplog.at_level(logging.WARNING):
            result = extract(
                content_type="message/rfc822",
                filename="f.eml",
                payload=_multipart(b"Content-Type: text/plain\r\n\r\nroot", part),
            )
        assert checked == [1]
        assert result.status == STATUS_SUCCESS
        assert result.text_complete is False
        assert result.text is not None and "[Attached message, depth 2]" in result.text
        assert "extractor cap eml_nested_messages:" in caplog.text
        assert MARKER not in caplog.text

    def test_an_intact_base64_nested_email_stays_complete(self, _in_process_child):
        part = _attached(_text("intact"), encoding=b"base64")
        result = extract(content_type="message/rfc822", filename="f.eml", payload=_multipart(part))
        assert result.text_complete is True
        assert result.text is not None and "intact" in result.text

    @staticmethod
    def _degraded_headers() -> bytes:
        # A raw 8-bit Subject that is not UTF-8, an encoded-word in an
        # unknown charset, and a raw 8-bit To holding an encoded-word.
        return (
            b"Subject: caf\xe9 " + MARKER.encode() + b"\r\n"
            b"From: =?x-unknown-synthetic?q?" + MARKER.encode() + b"?= <a@example.test>\r\n"
            b"To: caf\xc3\xa9 =?utf-8?q?" + MARKER.encode() + b"?= <b@example.test>\r\n"
            b"\r\nbody\r\n"
        )

    def test_header_fallbacks_are_counted_in_the_child(self):
        """In the child the decoders' lines go nowhere: each fallback is
        counted into ``eml_headers_degraded`` instead."""
        from src.extractors import child_degradation, eml, reset_attempt

        extractors.drain_counters()
        reset_attempt()
        text, caps = eml.extract_text(self._degraded_headers())
        assert caps == []
        assert child_degradation() == {"eml_headers_degraded": 3}
        assert "body" in text

    def test_the_parent_logs_the_count_and_keeps_the_text_complete(self, caplog):
        """Through the real child: one rate-limited parent line with the
        fixed key and count; replacement is not loss (#1315)."""
        extractors.drain_extractor_counts()
        with caplog.at_level(logging.WARNING):
            result = extract(
                content_type="message/rfc822", filename="f.eml", payload=self._degraded_headers()
            )
        assert result.status == STATUS_SUCCESS
        assert result.text_complete is True
        lines = [r for r in caplog.records if "degraded in the child" in r.getMessage()]
        assert [(r.levelname, r.getMessage()) for r in lines] == [
            ("WARNING", "extractor eml degraded in the child: eml_headers_degraded=3")
        ]
        assert extractors.drain_extractor_counts()["eml_headers_degraded"] == 3
        assert MARKER not in caplog.text

    def test_other_callers_count_nothing(self):
        """The counter is optional: the parser's own callers pass none."""
        value = parser.email.header.make_header([(b"caf\xe9", "unknown-8bit")])
        assert parser._decode_text_header(value) == "caf�"


class TestReviewRound2:
    """Codex round 2 on #1311, as the owner decided."""

    def _extract(self, payload: bytes, caplog) -> extractors.ExtractionResult:
        extractors.drain_extractor_counts()
        with caplog.at_level(logging.WARNING):
            result = extract(content_type="message/rfc822", filename="f.eml", payload=payload)
        assert result.status == STATUS_SUCCESS
        assert MARKER not in caplog.text
        return result

    def test_a_lossy_base64_body_part_is_a_cut(self, caplog):
        """Finding 2: the body-only walk reads ``_decode_lost_bytes``."""
        encoded = base64.encodebytes(b"intact words " + MARKER.encode() * 4)
        payload = (
            HDR
            + b"Content-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n"
            + encoded[:16]
            + b"!!!!"
            + encoded[20:]
        )
        result = self._extract(payload, caplog)
        assert result.text_complete is False
        assert "extractor cap eml_body_decode:" in caplog.text

    def test_an_intact_base64_body_part_stays_complete(self, caplog):
        payload = (
            HDR
            + b"Content-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n"
            + base64.encodebytes(b"intact words")
        )
        result = self._extract(payload, caplog)
        assert result.text_complete is True
        assert result.text == "Subject: s\nFrom: a@example.test\n\nintact words"

    def test_the_default_walk_ignores_body_decode_loss(self):
        """Walk-only: the top-level body path is unchanged (#1315)."""
        encoded = base64.encodebytes(b"intact words")
        msg = email.message_from_bytes(
            b"Content-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n"
            + encoded[:4]
            + b"!!!!"
            + encoded[8:]
        )
        caps: Counter[str] = Counter()
        parser._extract_body_and_attachments(msg, caps=caps)
        assert caps == Counter()

    def test_an_inline_attached_email_is_a_nested_section(self, caplog):
        """Finding 5: a ``message/rfc822`` part with no filename and no
        disposition keeps its headers and depth label."""
        inline = b"Content-Type: message/rfc822\r\n\r\n" + _text("inline body", subject="inner")
        result = self._extract(_multipart(b"Content-Type: text/plain\r\n\r\nroot", inline), caplog)
        assert result.text == (
            "Subject: s\nFrom: a@example.test\n\nroot"
            "\n\n[Attached message, depth 2]\nSubject: inner\n\ninline body"
        )
        assert result.text_complete is True

    def test_the_default_walk_still_folds_an_inline_attached_email(self):
        msg = email.message_from_bytes(
            _multipart(
                b"Content-Type: text/plain\r\n\r\nroot",
                b"Content-Type: message/rfc822\r\n\r\n" + _text("inline body"),
            )
        )
        body, attachments = parser._extract_body_and_attachments(msg)
        assert (body, attachments) == ("root\n\ninline body", [])

    def test_a_quoted_printable_nested_email_is_a_cut(self, caplog):
        """Finding 1: quoted-printable loss records nothing to detect, so
        every such nested email is counted lossy (until #1288)."""
        part = (
            b"Content-Type: message/rfc822\r\nContent-Disposition: attachment\r\n"
            b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
            b"Subject: n\r\n\r\nvisible =\rtail " + MARKER.encode()
        )
        result = self._extract(_multipart(part), caplog)
        assert result.text_complete is False
        assert result.text is not None and "[Attached message, depth 2]" in result.text
        assert "extractor cap eml_nested_messages:" in caplog.text

    @staticmethod
    def _with_named_part(disposition: bytes) -> bytes:
        return _multipart(
            b"Content-Type: text/plain\r\n\r\nroot",
            b"Content-Type: text/plain\r\nContent-Disposition: attachment; "
            + disposition
            + b"\r\n\r\n"
            + MARKER.encode(),
        )

    def test_a_filename_fallback_is_counted_and_changes_no_text(self, caplog):
        """Finding 6: counted into ``eml_filenames_degraded``; the part is
        an attachment either way, so the text is the same."""
        clean = self._extract(self._with_named_part(b'filename="n.txt"'), caplog)
        caplog.clear()
        result = self._extract(self._with_named_part(b"filename*=idna''n.txt"), caplog)
        assert result.text == clean.text == "Subject: s\nFrom: a@example.test\n\nroot"
        assert result.text_complete is True
        assert extractors.drain_extractor_counts()["eml_filenames_degraded"] == 1
        lines = [
            r.getMessage() for r in caplog.records if "degraded in the child" in r.getMessage()
        ]
        assert lines == ["extractor eml degraded in the child: eml_filenames_degraded=1"]

    def test_a_body_charset_fallback_is_counted_and_keeps_the_text_complete(self, caplog):
        """Finding 3: visible as ``eml_charsets_degraded``; replacement is
        not loss (#1315)."""
        payload = (
            HDR
            + b"Content-Type: text/plain; charset=x-unknown-synthetic\r\n\r\ncaf\xe9 "
            + MARKER.encode()
        )
        result = self._extract(payload, caplog)
        assert result.text_complete is True
        assert extractors.drain_extractor_counts()["eml_charsets_degraded"] == 1
        lines = [
            r.getMessage() for r in caplog.records if "degraded in the child" in r.getMessage()
        ]
        assert lines == ["extractor eml degraded in the child: eml_charsets_degraded=1"]

    def test_invalid_bytes_under_a_known_charset_are_counted(self):
        walk = parser.BodyWalk()
        msg = email.message_from_bytes(b"Content-Type: text/plain; charset=utf-8\r\n\r\ncaf\xe9")
        body, _ = parser._extract_body_and_attachments(msg, walk=walk)
        assert body == "caf�"
        assert walk.degraded == Counter({parser.CHARSET_DEGRADED: 1})


class TestReviewRound3:
    """Codex round 3 on #1311: guards decided from the declared
    Content-Transfer-Encoding alone, as the owner decided for round 2."""

    def _extract(self, payload: bytes, caplog) -> extractors.ExtractionResult:
        extractors.drain_extractor_counts()
        with caplog.at_level(logging.WARNING):
            result = extract(content_type="message/rfc822", filename="f.eml", payload=payload)
        assert result.status == STATUS_SUCCESS
        assert MARKER not in caplog.text
        return result

    def test_a_quoted_printable_body_part_is_a_cut(self, caplog):
        """Finding 1: a quoted-printable body loss records nothing to
        detect, so the part counts as lossy (until #1288)."""
        payload = (
            HDR + b"Content-Type: text/plain\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\n"
            b"visible =\rtail " + MARKER.encode()
        )
        result = self._extract(payload, caplog)
        assert result.text_complete is False
        assert "extractor cap eml_body_decode:" in caplog.text

    def test_the_default_walk_ignores_a_quoted_printable_body(self):
        msg = email.message_from_bytes(
            b"Content-Type: text/plain\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\nok"
        )
        caps: Counter[str] = Counter()
        assert parser._extract_body_and_attachments(msg, caps=caps)[0] == "ok"
        assert caps == Counter()

    @pytest.mark.parametrize("encoding", [b"x-uuencode", b"uuencode", b"uue", b"x-uue", b"x-other"])
    def test_a_nested_email_in_an_unhandled_encoding_is_labelled_and_cut(self, encoding, caplog):
        """Finding 2: no decoder; the encoded transport text is never
        indexed, the depth label shows the skip, and the text is
        incomplete."""
        inner = b"Subject: n\r\n\r\nnested " + MARKER.encode() + b"\r\n"
        uu = b"begin 644 x\n" + binascii.b2a_uu(inner) + b"`\nend\n"
        part = (
            b"Content-Type: message/rfc822\r\nContent-Disposition: attachment\r\n"
            b"Content-Transfer-Encoding: " + encoding + b"\r\n\r\n" + uu
        )
        result = self._extract(_multipart(b"Content-Type: text/plain\r\n\r\nroot", part), caplog)
        assert result.text == (
            "Subject: s\nFrom: a@example.test\n\nroot\n\n[Attached message, depth 2]"
        )
        assert result.text_complete is False
        assert "extractor cap eml_nested_messages:" in caplog.text

    @pytest.mark.parametrize("encoding", [b"7bit", b"8bit", b"binary", b"7BIT"])
    def test_identity_encodings_are_rendered(self, encoding, caplog):
        part = (
            b"Content-Type: message/rfc822\r\nContent-Disposition: attachment\r\n"
            b"Content-Transfer-Encoding: " + encoding + b"\r\n\r\n" + _text("plain nested")
        )
        result = self._extract(_multipart(part), caplog)
        assert result.text_complete is True
        assert result.text is not None and "plain nested" in result.text


class TestReviewRound5:
    """Codex round 5 on #1311, as the owner decided."""

    @pytest.mark.parametrize("encoding", [b"x-uuencode", b"uuencode", b"uue", b"x-uue", b"x-other"])
    def test_a_body_part_in_an_unhandled_encoding_is_a_cut(self, encoding, caplog):
        """Finding 1: a malformed envelope comes back as transport text,
        so the part's text is kept but the result is incomplete."""
        payload = (
            HDR
            + b"Content-Type: text/plain\r\nContent-Transfer-Encoding: "
            + encoding
            + b"\r\n\r\nbegin not-an-envelope "
            + MARKER.encode()
        )
        extractors.drain_extractor_counts()
        with caplog.at_level(logging.WARNING):
            result = extract(content_type="message/rfc822", filename="f.eml", payload=payload)
        assert result.status == STATUS_SUCCESS
        assert result.text_complete is False
        assert result.text is not None and "not-an-envelope" in result.text
        assert "extractor cap eml_body_decode:" in caplog.text
        assert MARKER not in caplog.text

    def test_a_codec_that_rejects_replace_decodes_as_the_default_path(self):
        """Finding 3: ``idna`` decodes strictly but rejects
        ``errors="replace"``; the walk returns the default path's text
        (its UTF-8 fallback) and counts the fallback."""
        degraded: Counter[str] = Counter()
        assert parser._safe_decode(b"xn--caf-dma", "idna", degraded) == parser._safe_decode(
            b"xn--caf-dma", "idna"
        )
        assert parser._safe_decode(b"xn--caf-dma", "idna") == "xn--caf-dma"
        assert degraded == Counter({parser.CHARSET_DEGRADED: 1})

    def test_a_replacement_is_counted(self):
        degraded: Counter[str] = Counter()
        assert parser._safe_decode(b"caf\xe9", "utf-8", degraded) == "caf�"
        assert degraded == Counter({parser.CHARSET_DEGRADED: 1})

    def test_a_clean_decode_is_not_counted(self):
        degraded: Counter[str] = Counter()
        assert parser._safe_decode("café".encode("latin-1"), "latin-1", degraded) == "café"
        assert degraded == Counter()

    def test_the_walk_body_matches_the_default_body(self):
        raw = b"Content-Type: text/plain; charset=idna\r\n\r\nxn--caf-dma"
        default, _ = parser._extract_body_and_attachments(email.message_from_bytes(raw))
        walked, _ = parser._extract_body_and_attachments(
            email.message_from_bytes(raw), walk=parser.BodyWalk()
        )
        assert walked == default == "xn--caf-dma"


# Review round 6 on #1311: declared multipart containers the standard
# library could not decompose, at the root, in a sub-part and in a nested
# email. (payload, cut, text kept). A cut is exactly a part whose declared
# maintype is multipart that did not parse into parts: its text is lost.
_SUB = (
    b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
    b"--B\r\nContent-Type: text/plain\r\n\r\nKEPT\r\n--B\r\n"
)
_STRUCTURE_SHAPES = {
    "root_start_boundary_absent": (
        b"Content-Type: multipart/mixed; boundary=NEVER\r\n\r\n" + MARKER.encode(),
        True,
        "",
    ),
    "root_no_boundary_parameter": (
        b"Content-Type: multipart/mixed\r\n\r\n" + MARKER.encode(),
        True,
        "",
    ),
    "root_close_boundary_absent": (
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nKEPT TEXT\r\n",
        False,
        "KEPT TEXT",
    ),
    "root_separator_missing": (
        b'Content-Type: multipart/mixed; boundary="B"\r\nnot a header line\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nKEPT TEXT\r\n--B--\r\n",
        False,
        "KEPT TEXT",
    ),
    "sub_start_boundary_absent": (
        _SUB
        + b"Content-Type: multipart/alternative; boundary=NEVER\r\n\r\n"
        + MARKER.encode()
        + b"\r\n--B--\r\n",
        True,
        "KEPT",
    ),
    "sub_alternative_no_boundary_parameter": (
        _SUB + b"Content-Type: multipart/alternative\r\n\r\n" + MARKER.encode() + b"\r\n--B--\r\n",
        True,
        "KEPT",
    ),
    "sub_close_boundary_absent": (
        _SUB + b'Content-Type: multipart/alternative; boundary="C"\r\n\r\n'
        b"--C\r\nContent-Type: text/plain\r\n\r\nKEPT TEXT\r\n--B--\r\n",
        False,
        "KEPT\n\nKEPT TEXT",
    ),
    "sub_separator_missing": (
        _SUB + b'Content-Type: multipart/alternative; boundary="C"\r\nnot a header line\r\n'
        b"--C\r\nContent-Type: text/plain\r\n\r\nKEPT TEXT\r\n--C--\r\n--B--\r\n",
        False,
        "KEPT\n\nKEPT TEXT",
    ),
    "nested_email_no_boundary_parameter": (
        _SUB + b"Content-Type: message/rfc822\r\nContent-Disposition: attachment\r\n\r\n"
        b"Subject: n\r\nContent-Type: multipart/mixed\r\n\r\n" + MARKER.encode() + b"\r\n--B--\r\n",
        True,
        "KEPT\n\n[Attached message, depth 2]\nSubject: n",
    ),
}


class TestReviewRound6:
    @pytest.mark.parametrize("shape", sorted(_STRUCTURE_SHAPES))
    def test_a_container_that_did_not_decompose_is_a_cut(self, shape, caplog):
        payload, cut, kept = _STRUCTURE_SHAPES[shape]
        extractors.drain_extractor_counts()
        with caplog.at_level(logging.WARNING):
            result = extract(
                content_type="message/rfc822", filename="f.eml", payload=b"Subject: s\r\n" + payload
            )
        assert result.status == STATUS_SUCCESS
        assert result.text == "Subject: s" + (f"\n\n{kept}" if kept else "")
        assert result.text_complete is not cut
        assert ("extractor cap eml_body_structure:" in caplog.text) is cut
        assert MARKER not in caplog.text

    def test_the_default_walk_counts_nothing(self):
        msg = email.message_from_bytes(b"Content-Type: multipart/mixed\r\n\r\ntext")
        caps: Counter[str] = Counter()
        parser._extract_body_and_attachments(msg, caps=caps)
        assert caps == Counter()

    def test_the_walk_counts_each_undecomposed_container(self):
        payload, _, _ = _STRUCTURE_SHAPES["sub_alternative_no_boundary_parameter"]
        walk = parser.BodyWalk()
        parser._extract_body_and_attachments(email.message_from_bytes(payload), walk=walk)
        assert walk.structure_lost_parts == 1


# Review round 8 on #1311.
def _alternative(plain: bytes, html: bytes) -> bytes:
    """An attached email whose body is a ``multipart/alternative`` of
    the two given parts (headers included)."""
    return (
        HDR + b'Content-Type: multipart/alternative; boundary="A"\r\n\r\n'
        b"--A\r\n" + plain + b"\r\n--A\r\n" + html + b"\r\n--A--\r\n"
    )


_PLAIN = b"Content-Type: text/plain\r\n\r\nplain words"
_HTML = b"Content-Type: text/html\r\n\r\n<p>html words</p>"
_B64_HTML = base64.encodebytes(b"<p>html words " + MARKER.encode() * 4 + b"</p>")
_LOSSY_HTML = (
    b"Content-Type: text/html\r\nContent-Transfer-Encoding: base64\r\n\r\n"
    + _B64_HTML[:16]
    + b"!!!!"
    + _B64_HTML[20:]
)
_B64_PLAIN = base64.encodebytes(b"plain words " + MARKER.encode() * 4)
_LOSSY_PLAIN = (
    b"Content-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n"
    + _B64_PLAIN[:16]
    + b"!!!!"
    + _B64_PLAIN[20:]
)


class TestReviewRound8:
    def _extract(self, payload: bytes, caplog) -> extractors.ExtractionResult:
        extractors.drain_extractor_counts()
        with caplog.at_level(logging.WARNING):
            result = extract(content_type="message/rfc822", filename="f.eml", payload=payload)
        assert result.status == STATUS_SUCCESS
        assert MARKER not in caplog.text
        return result

    @pytest.mark.parametrize(
        "html",
        [
            _LOSSY_HTML,
            b"Content-Type: text/html\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\n"
            b"<p>html =\rwords</p>",
            b"Content-Type: text/html; charset=x-unknown-synthetic\r\n\r\n<p>caf\xe9</p>",
        ],
        ids=["lossy_base64", "quoted_printable", "charset_fallback"],
    )
    def test_loss_in_an_unselected_alternative_is_not_counted(self, caplog, html):
        """The body keeps only the clean plain part, so a loss or fallback
        in the HTML rendering it set aside changes nothing indexed."""
        result = self._extract(_alternative(_PLAIN, html), caplog)
        assert result.text == "Subject: s\nFrom: a@example.test\n\nplain words"
        assert result.text_complete is True
        assert "eml_body_decode" not in caplog.text
        assert extractors.drain_extractor_counts()["eml_charsets_degraded"] == 0

    def test_loss_in_the_selected_alternative_is_counted(self, caplog):
        result = self._extract(_alternative(_LOSSY_PLAIN, _HTML), caplog)
        assert result.text_complete is False
        assert "extractor cap eml_body_decode:" in caplog.text

    def test_a_lossy_part_left_blank_still_counts_if_it_would_be_kept(self, caplog):
        """A plain part whose lossy decode left no text is set aside for
        the HTML, but the sender's plain text would have been kept."""
        blank = b"Content-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n!!!!"
        result = self._extract(_alternative(blank, _HTML), caplog)
        assert result.text == "Subject: s\nFrom: a@example.test\n\nhtml words"
        assert result.text_complete is False
        assert "extractor cap eml_body_decode:" in caplog.text

    def test_a_selected_charset_fallback_is_still_counted(self, caplog):
        plain = b"Content-Type: text/plain; charset=x-unknown-synthetic\r\n\r\ncaf\xe9"
        self._extract(_alternative(plain, _HTML), caplog)
        assert extractors.drain_extractor_counts()["eml_charsets_degraded"] == 1

    def test_a_dropped_first_transport_line_cuts_the_nested_email(self, caplog, _in_process_child):
        """Security finding: a base64 transport whose first line starts
        with whitespace is read by the standard library as a header
        continuation with no header before it and dropped; the rest
        decodes cleanly, so the nested email is cut."""
        encoded = base64.encodebytes(_text("intact words " + MARKER * 3))
        assert encoded.count(b"\n") > 1
        part = (
            b"Content-Type: message/rfc822\r\nContent-Disposition: attachment\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n " + encoded
        )
        result = self._extract(_multipart(b"Content-Type: text/plain\r\n\r\nroot", part), caplog)
        assert result.text_complete is False
        assert "extractor cap eml_nested_messages:" in caplog.text
