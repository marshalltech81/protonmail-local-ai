"""A prediction no message was compared for is never reported as observed."""

from copy import deepcopy
from dataclasses import replace

import pytest
from evidence_run import frozen
from test_driver import _frozen_dir, _run


@pytest.fixture
def inputs(tmp_path):
    return frozen.load(_frozen_dir(tmp_path))


@pytest.mark.parametrize(
    ("group", "edit", "predictions"),
    [
        ("g1", {"control": 0, "incremental": 0}, ["P1"]),
        ("g3", {"messages": 0}, ["P3"]),
        ("g4", {"drafts": 0}, ["P4"]),
        ("g6", {"messages": 0}, ["P9", "P10"]),
        ("g6", {"labels": 0}, ["P9"]),
        ("g6", {"race_repeats": 0}, ["P10"]),
        ("g7", {"tracked_multipart": 0, "tracked_plain": 0}, ["P3", "P6", "P7", "P8"]),
    ],
)
def test_an_empty_workload_is_not_observed(inputs, group, edit, predictions):
    plan = deepcopy(inputs.fixtures)
    plan["groups"][group].update(edit)
    _, result = _run(replace(inputs, fixtures=plan), groups=(group,))
    assert result.stop is None
    for prediction in predictions:
        assert result.states[prediction] == "unobservable"
