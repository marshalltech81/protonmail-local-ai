"""#1304: expose existing prefix normalization while preserving display case."""

import pytest
from src.lib.sqlite import _normalize_subject
from src.tools import outputs
from src.tools.outputs import ThreadSummary, thread_summary


@pytest.mark.parametrize(
    ("subject", "normalized"),
    [
        ("Re: RE: Fw:  Project   Update", "Project Update"),
        ("Re: [EXT] Project", "[EXT] Project"),
        ("[EXT] Re: Project", "[EXT] Re: Project"),
        ("Re: Re: [EXT] Project", "[EXT] Project"),
        ("Sv: 回复:  École", "École"),
        ("Tr report", "Tr report"),
        ("", ""),
    ],
)
def test_normalized_subject_preserves_original(subject, normalized, make_result):
    record = make_result("t")
    record.subject = subject
    result = thread_summary(record)
    assert result.subject == subject
    assert result.model_dump()["normalized_subject"] == normalized
    if "[" not in subject:
        assert _normalize_subject(subject) == normalized.lower()


def test_normalized_subject_is_in_served_schema():
    assert "normalized_subject" in ThreadSummary.model_json_schema()["properties"]


def test_every_subject_row_exposes_normalization():
    models = [
        model
        for model in vars(outputs).values()
        if isinstance(model, type)
        and issubclass(model, outputs._Output)
        and "subject" in model.model_fields
    ]
    assert models
    assert all("normalized_subject" in model.model_fields for model in models)


def test_nullable_sample_subject_stays_unknown():
    row = outputs.GroupSampleRow(
        claimant_id="synthetic",
        message_id="synthetic",
        thread_id="synthetic",
        subject=None,
        at=None,
        subject_clipped=False,
        subject_complete=None,
    )
    assert row.normalized_subject is None


@pytest.mark.parametrize(
    "prefix", ["Re: ", "Re:", "Fw: Re: ", "Sv: 回复: ", "Ant: ", "Fwd: " * 100]
)
def test_matching_and_display_keep_the_same_gateway_tag(prefix):
    assert _normalize_subject(prefix + "[TAG] Topic") == "[tag] topic"
    assert _normalize_subject(prefix + "[TAG] Topic", preserve_case=True) == "[TAG] Topic"


def test_tagged_reply_is_not_sent_as_changed_rerank_subject():
    from src.lib.sqlite import _changed_reply_subjects

    assert _changed_reply_subjects("[TAG] Topic", ["Re: [TAG] Topic", "Fwd: Re: [TAG] Topic"]) == []
