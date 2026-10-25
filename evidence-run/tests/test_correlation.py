"""Correlation uses top-level parsed evidence, never quoted body fields."""

import pytest
from evidence_run.pieces import _token
from evidence_run.simulated import SimulatedWorld


@pytest.mark.parametrize("reader", [_token, SimulatedWorld._token])
@pytest.mark.parametrize(
    "raw",
    [
        b"From: a@sender.example\r\nSubject: unrelated\r\n\r\nSubject: SYNTH-r1g1-1\r\n",
        b"From: a@sender.example\r\nSubject: SYNTH-r1g1-1\r\nSubject: unrelated\r\n\r\n",
        b"From: a@sender.example\r\nFrom: b@other.example\r\nSubject: SYNTH-r1g1-1\r\n\r\n",
        b"Subject: SYNTH-r1g1-1\r\n\r\nFrom: a@sender.example\r\n",
    ],
)
def test_body_or_ambiguous_headers_cannot_supply_correlation(reader, raw):
    assert reader(raw) is None


def test_read_uses_the_same_top_level_classification(tmp_path):
    from evidence_run import frozen
    from test_driver import _frozen_dir

    inputs = frozen.load(_frozen_dir(tmp_path))
    from evidence_run.simulated import SimulatedWorld
    from evidence_run.world import Served

    class BodySubject(SimulatedWorld):
        def served(self, folder=None):
            items = super().served(folder)
            items.append(
                Served(
                    "INBOX",
                    9999,
                    1,
                    frozenset(),
                    b"From: a@sender.example\r\nSubject: unrelated\r\n\r\nSubject: SYNTH-r1g1-1\r\n",
                    None,
                )
            )
            return items

    from test_driver import _run

    _, result = _run(inputs, world=BodySubject(), groups=("g1",))
    assert result.stop == ("unexplained_message", "g1")


def test_correlation_caps_precede_header_parsing(monkeypatch):
    from evidence_run import correlation

    parsed = []
    original = correlation.BytesHeaderParser.parsebytes

    def parse(self, raw, *args, **kwargs):
        parsed.append(len(raw))
        return original(self, raw, *args, **kwargs)

    monkeypatch.setattr(correlation.BytesHeaderParser, "parsebytes", parse)
    head = b"From: a@sender.example\r\nSubject: SYNTH-r1g1-1\r\n\r\n"
    assert correlation.summary(head + b"x" * 2_000_000).token == "SYNTH-r1g1-1"
    assert len(parsed) == 1 and parsed[0] < len(head)
    parsed.clear()
    assert correlation.summary(b"X-Large: " + b"x" * 100_000 + b"\r\n\r\n").token is None
    assert correlation.summary(b"X: x\r\n" * 300 + head).token is None
    assert parsed == []


@pytest.mark.parametrize("ending", [b"\r\n", b"\n"])
def test_valid_correlation_and_service_sender_share_parsed_fields(ending):
    from evidence_run import correlation

    for subject, token in [(b"SYNTH-r1g1-1", "SYNTH-r1g1-1"), (b"Welcome", None)]:
        observed = correlation.summary(
            ending.join(
                [b"From: Synthetic <a@sender.example>", b"Subject: " + subject, b"", b"body"]
            )
        )
        assert observed.token == token and observed.sender_domain == "sender.example"


@pytest.mark.parametrize("ending", [b"\r\n", b"\n"])
def test_identity_shape_and_gluon_reads_cannot_use_body_headers(ending):
    from evidence_run import driver

    raw = ending.join(
        [
            b"From: a@sender.example",
            b"Subject: SYNTH-r1g1-1",
            b"",
            b"X-Pm-Internal-Id: SYNTHFORGED-1",
            b"X-Pm-Gluon-Id: SYNTHFORGEDGLUON-1",
        ]
    )
    assert driver.header_shape(raw) == driver.Shape(0, 0, False, 0, 0)
    assert driver._field_value(raw, b"x-pm-gluon-id") is None


def test_identity_fields_are_unfolded_before_comparisons():
    from evidence_run import driver

    raw = (
        b"X-Pm-Gluon-Id:\r\n SYNTHFORGEDGLUON-1\r\nX-Pm-Internal-Id:\r\n SYNTHFORGED-1\r\n\r\nbody"
    )
    shape = driver.header_shape(raw)
    assert (shape.forged_internal, shape.forged_gluon) == (1, 1)
    assert driver._field_value(raw, b"x-pm-gluon-id") == b"SYNTHFORGEDGLUON-1"


def test_deeply_nested_from_comments_are_unknown_evidence():
    from evidence_run import correlation

    nested = "(" * 490 + "x" + ")" * 490
    assert len(f"{nested} <a@b>") < correlation.MAX_CORRELATION_FIELD_CHARS
    raw = f"Subject: SYNTH-r1g1-1\r\nFrom: {nested} <a@b>\r\n\r\n".encode()
    assert correlation.summary(raw) == correlation.MessageSummary(None, None)
