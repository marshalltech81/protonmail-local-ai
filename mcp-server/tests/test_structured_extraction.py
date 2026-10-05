"""
Anthropic structured outputs for ``extract_from_emails`` (#808).

With ``INFERENCE_STRUCTURED_OUTPUT`` on in anthropic mode, each
per-thread call carries a strict JSON schema built from the caller's
schema, and the reply is ``{"records": [...]}`` (an empty list means
no relevant data). The conversion is one bounded pass over the
declared fields; a schema it cannot express strictly (an object field,
a nested shape) is sent without the format, as today, and the response
says so in fixed text. With the setting off, or in openai mode, the
request and the parse are today's. All data is synthetic.
"""

import asyncio
import json
import logging
import time

import pytest
from fastmcp.exceptions import ToolError
from src.lib.inference import InferenceClient
from src.tools.brief import BRIEF_JSON_SCHEMA, CHECK_JSON_SCHEMA
from src.tools.intelligence import (
    MAX_STRUCTURED_FIELDS,
    MAX_UNION_PARAMS,
    _strict_record_schema,
    _strict_records_schema,
    _union_param_count,
    register_intelligence_tools,
)

from tests.conftest import FakeEmbedClient, FakeInferenceClient
from tests.test_brief_issue import assert_strict

_QUERY = "invoice OR lunch OR meeting"
_MARKER = "SYNTHETIC_STRUCTURED_MARKER_5520"


def _nullable(*types: str) -> dict:
    return {"anyOf": [*({"type": t} for t in types), {"type": "null"}]}


_ANY_SCALAR = {
    "anyOf": [
        {"type": "string"},
        {"type": "number"},
        {"type": "integer"},
        {"type": "boolean"},
        {"type": "null"},
    ]
}
_ARRAY = {"anyOf": [{"type": "array", "items": _ANY_SCALAR}, {"type": "null"}]}
_STRING = _nullable("string")

# Caller schema -> the record's declared field schemas, or None when the
# schema cannot be expressed strictly (sent without the format).
_CATALOGUE = [
    # Shorthand.
    (
        {"vendor": "string", "amount": "number", "count": "integer", "paid": "boolean"},
        {
            "vendor": _STRING,
            "amount": _nullable("number"),
            "count": _nullable("integer"),
            "paid": _nullable("boolean"),
        },
    ),
    ({"amount": "dollar amount"}, {"amount": _STRING}),
    ({"vendor": "String"}, {"vendor": _STRING}),
    ({"names": "array"}, {"names": _ARRAY}),
    ({"amount": ["number", "null"]}, {"amount": _nullable("number")}),
    ({"id": ["string", "integer"]}, {"id": _nullable("string", "integer")}),
    # An array mixed with another type makes Anthropic's grammar too large
    # (measured 2026-10-05: eight such fields refused), so it is sent as today.
    ({"tags": ["array", "string"]}, None),
    ({"tags": ["array", "null"]}, {"tags": _ARRAY}),
    ({"nothing": "null"}, {"nothing": {"anyOf": [{"type": "null"}]}}),
    ({"address": "object"}, None),
    ({"address": {"street": "string"}}, None),
    ({"amount": {"type": "number"}}, None),
    ({"amount": ["number", "money"]}, None),
    ({"address": ["object", "null"]}, None),
    ({"x": []}, None),
    ({"x": 5}, None),
    ({"x": None}, None),
    ({}, None),
    # JSON Schema form.
    (
        {
            "type": "object",
            "properties": {"vendor": {"type": "string"}, "amount": {"type": "number"}},
            "required": ["vendor"],
        },
        {"vendor": _STRING, "amount": _nullable("number")},
    ),
    (
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a", "b"]},
        {"a": _STRING, "b": _STRING},
    ),
    ({"type": "object", "required": ["a"]}, {"a": _STRING}),
    ({"properties": {"note": {"description": "free text"}}}, {"note": _STRING}),
    ({"properties": {"kind": {"enum": ["a", "b"]}}}, {"kind": _STRING}),
    ({"properties": {"amount": {"type": "dollar amount"}}}, {"amount": _STRING}),
    ({"properties": {"v": {"type": ["string", "null"]}}}, {"v": _STRING}),
    ({"properties": {"tags": {"type": "array"}}}, {"tags": _ARRAY}),
    (
        {"properties": {"tags": {"type": "array", "items": {"type": "string"}}}},
        {"tags": _ARRAY},
    ),
    ({"properties": {"rows": {"type": "array", "items": {"type": "object"}}}}, None),
    ({"properties": {"rows": {"type": "array", "items": [{"type": "string"}]}}}, None),
    ({"properties": {"address": {"type": "object"}}}, None),
    ({"properties": {"address": {"properties": {"street": {"type": "string"}}}}}, None),
    ({"properties": {"rows": {"items": {"type": "string"}}}}, None),
    ({"properties": {"v": {"anyOf": [{"type": "string"}]}}}, None),
    ({"properties": {"v": {"oneOf": [{"type": "string"}]}}}, None),
    ({"properties": {"v": {"allOf": [{"type": "string"}]}}}, None),
    ({"properties": {"v": {"$ref": "#/$defs/x"}}}, None),
    ({"properties": {"v": "string"}}, None),
    ({"type": "object"}, None),
    ({"type": "object", "properties": {}}, None),
]


def _evidence(fields) -> dict:
    return {
        "type": "object",
        "properties": {name: {"type": "array", "items": {"type": "string"}} for name in fields},
        "required": list(fields),
        "additionalProperties": False,
    }


class TestConversion:
    @pytest.mark.parametrize(("schema", "fields"), _CATALOGUE)
    def test_catalogue(self, schema, fields):
        converted = _strict_record_schema(schema)
        if fields is None:
            assert converted is None
            return
        assert converted == {
            "type": "object",
            "properties": {**fields, "_evidence": _evidence(fields)},
            "required": [*fields, "_evidence"],
            "additionalProperties": False,
        }

    @pytest.mark.parametrize(("schema", "fields"), [c for c in _CATALOGUE if c[1] is not None])
    def test_every_converted_schema_is_strict(self, schema, fields):
        wrapper = _strict_records_schema(schema)
        assert wrapper is not None
        assert wrapper["required"] == ["records"]
        assert_strict(wrapper)

    def test_unconvertible_schema_has_no_wrapper(self):
        assert _strict_records_schema({"address": "object"}) is None

    def test_conversion_does_not_recurse_into_nested_schemas(self):
        """One pass over the declared fields: a deeply nested caller
        schema is not walked, so it can neither recurse nor cost more
        than its top level."""
        deep: dict = {"type": "string"}
        for _ in range(100_000):
            deep = {"type": "array", "items": deep}
        assert _strict_record_schema({"properties": {"rows": deep}}) is None

    def test_conversion_is_linear_in_the_field_count(self):
        """A schema far past the union limit is refused after one pass,
        quickly, and the pass did the work: each field was converted."""
        schema = {f"field_{i}": "string" for i in range(20_000)}
        start = time.perf_counter()
        record = _strict_record_schema(schema, limited=False)
        assert time.perf_counter() - start < 2.0
        assert record is not None
        assert len(record["properties"]) == 20_001
        assert len(record["properties"]["_evidence"]["properties"]) == 20_000
        assert _union_param_count(record) == 20_000
        start = time.perf_counter()
        assert _strict_record_schema(schema) is None
        assert time.perf_counter() - start < 2.0


class TestProviderLimits:
    """Anthropic refuses a structured-output schema with more than 16
    union-typed (anyOf or type-list) parameters, and one whose compiled
    grammar is too large; the second limit is unpublished and depends on
    the schema's shape (measured 2026-10-05: 15 scalar fields accepted, 16
    refused; 10 scalar plus 3 array fields refused). Every mix of up to 8
    fields was accepted on Sonnet 5.5, Sonnet 4.6 and Opus 5.5, except
    type lists mixing an array with another type, so more fields, or such
    a list, is sent without the format, as before #808. Evidence label
    lists are plain arrays and add no union."""

    def test_limits(self):
        assert MAX_UNION_PARAMS == 16
        assert MAX_STRUCTURED_FIELDS == 8

    @pytest.mark.parametrize(
        ("schema", "unions"),
        [
            ({f"f{i}": "string" for i in range(8)}, 8),
            ({f"f{i}": "array" for i in range(8)}, 16),
            ({**{f"s{i}": "string" for i in range(4)}, **{f"a{i}": "array" for i in range(4)}}, 12),
            ({f"m{i}": ["string", "number", "integer", "boolean"] for i in range(8)}, 8),
            ({"properties": {f"f{i}": {"type": "number"} for i in range(8)}}, 8),
            ({"properties": {"a": {"type": "string"}}, "required": [f"r{i}" for i in range(7)]}, 8),
        ],
    )
    def test_at_the_limit_is_structured(self, schema, unions):
        wrapper = _strict_records_schema(schema)
        assert wrapper is not None
        assert _union_param_count(wrapper) == unions <= MAX_UNION_PARAMS

    @pytest.mark.parametrize(
        "schema",
        [
            {f"f{i}": "string" for i in range(9)},
            {f"f{i}": "array" for i in range(9)},
            {"properties": {f"f{i}": {"type": "number"} for i in range(9)}},
            {"properties": {"a": {"type": "string"}}, "required": [f"r{i}" for i in range(8)]},
            {"tags": ["array", "number"]},
        ],
    )
    def test_over_the_limit_is_sent_as_today(self, schema):
        assert _strict_records_schema(schema) is None

    def test_union_cap_applies_without_the_field_cap(self):
        """The union count is its own check: 9 array fields are 18 unions."""
        record = _strict_record_schema({f"f{i}": "array" for i in range(9)}, limited=False)
        assert record is not None
        assert _union_param_count(record) == 18 > MAX_UNION_PARAMS

    def test_evidence_lists_add_no_unions(self):
        record = _strict_record_schema({"vendor": "string", "amount": "number"})
        assert record is not None
        assert _union_param_count(record["properties"]["_evidence"]) == 0

    @pytest.mark.parametrize("schema", [BRIEF_JSON_SCHEMA, CHECK_JSON_SCHEMA])
    def test_fixed_schemas_are_within_the_limit(self, schema):
        assert _union_param_count(schema) <= MAX_UNION_PARAMS

    def test_counting_is_linear(self):
        """The count walks the server-built schema once."""
        deep: dict = {"type": "string"}
        for _ in range(50_000):
            deep = {"anyOf": [deep, {"type": "null"}]}
        start = time.perf_counter()
        assert _union_param_count(deep) == 50_000
        assert time.perf_counter() - start < 2.0

    def test_over_the_limit_extraction_is_sent_as_today_and_says_so(self, seeded_db):
        llm = FakeInferenceClient(complete_responses=["null"] * 3, structured_output=True)
        out = _run(seeded_db, llm, {f"f{i}": "string" for i in range(9)})
        assert llm.json_schemas == [None] * 3
        for _system, user in llm.complete_calls:
            assert user.endswith(_LEGACY_ASK)
        notice = out.structured_content["notice"]
        assert notice is not None
        assert "INFERENCE_STRUCTURED_OUTPUT" in notice


def _run(seeded_db, llm, schema):
    from tests.conftest import FakeMCPServer

    server = FakeMCPServer()
    register_intelligence_tools(server, seeded_db, FakeEmbedClient(), llm)
    return asyncio.run(server.tools["extract_from_emails"](query=_QUERY, schema=schema))


def _texts(out) -> str:
    return "\n".join(item.text for item in out.content)


_SHORTHAND = {"vendor": "string"}
_STRUCTURED_ASK = '{"records": [...]}'
_LEGACY_ASK = "or null if no relevant data found."


class TestStructuredExtraction:
    def test_each_call_carries_the_records_schema(self, seeded_db):
        llm = FakeInferenceClient(
            complete_responses=[
                '{"records": [{"vendor": "Acme", "_evidence": {"vendor": ["E1"]}}]}',
                '{"records": []}',
                '{"records": [{"vendor": "Beta", "_evidence": {"vendor": null}}, '
                '{"vendor": "Gamma", "_evidence": {"vendor": null}}]}',
            ],
            structured_output=True,
        )
        out = _run(seeded_db, llm, _SHORTHAND)
        assert llm.json_schemas == [_strict_records_schema(_SHORTHAND)] * 3
        for _system, user in llm.complete_calls:
            assert _STRUCTURED_ASK in user
            assert _LEGACY_ASK not in user
        records = out.structured_content["records"]
        assert [r["vendor"] for r in records] == ["Acme", "Beta", "Gamma"]
        assert out.structured_content["notice"] is None
        assert "could not be extracted" not in _texts(out)

    def test_empty_records_everywhere_is_no_data(self, seeded_db):
        llm = FakeInferenceClient(
            complete_responses=['{"records": []}'] * 3, structured_output=True
        )
        out = _run(seeded_db, llm, _SHORTHAND)
        assert "No structured data matching the schema found in 3 threads." in _texts(out)
        assert "could not be extracted" not in _texts(out)

    @pytest.mark.parametrize(
        "reply",
        [
            "not json",
            "null",
            "[]",
            '[{"vendor": "Acme"}]',
            '{"vendor": "Acme"}',
            '{"records": null}',
            '{"records": "Acme"}',
        ],
    )
    def test_reply_that_is_not_the_wrapper_is_a_failure(self, seeded_db, reply):
        llm = FakeInferenceClient(complete_responses=[reply] * 3, structured_output=True)
        out = _run(seeded_db, llm, _SHORTHAND)
        assert "3 of 3 threads could not be extracted" in _texts(out)

    def test_non_object_records_are_a_failure(self, seeded_db):
        llm = FakeInferenceClient(
            complete_responses=['{"records": [{"vendor": "Acme"}, "Beta"]}', '{"records": []}']
            + ['{"records": []}'],
            structured_output=True,
        )
        out = _run(seeded_db, llm, _SHORTHAND)
        assert [r["vendor"] for r in out.structured_content["records"]] == ["Acme"]
        assert "1 of 3 threads could not be extracted" in _texts(out)

    def test_null_for_an_unrequired_property_stands_for_the_omitted_field(self, seeded_db):
        """Every field is required and nullable in the strict schema; a
        null in a property the caller did not require is the omitted
        field, so it is dropped before the unchanged conformance check.
        A null in a required property still fails it, as today."""
        schema = {
            "type": "object",
            "properties": {"vendor": {"type": "string"}, "amount": {"type": "number"}},
            "required": ["vendor"],
        }
        llm = FakeInferenceClient(
            complete_responses=[
                '{"records": [{"vendor": "Acme", "amount": null, "_evidence": {}}]}',
                '{"records": [{"vendor": null, "amount": 3, "_evidence": {}}]}',
                '{"records": []}',
            ],
            structured_output=True,
        )
        out = _run(seeded_db, llm, schema)
        [record] = out.structured_content["records"]
        assert record["vendor"] == "Acme"
        assert "amount" not in record
        assert "1 of 3 threads could not be extracted" in _texts(out)
        assert "did not match the schema" in _texts(out)

    def test_shorthand_nulls_are_kept_as_today(self, seeded_db):
        llm = FakeInferenceClient(
            complete_responses=[
                '{"records": [{"vendor": "Acme", "note": null, "_evidence": {}}]}',
                '{"records": []}',
                '{"records": []}',
            ],
            structured_output=True,
        )
        out = _run(seeded_db, llm, {"vendor": "string", "note": "string"})
        [record] = out.structured_content["records"]
        assert record["note"] is None

    def test_unconvertible_schema_is_sent_as_today_and_says_so(self, seeded_db):
        llm = FakeInferenceClient(
            complete_responses=['```json\n{"address": {"street": "Main"}}\n```', "null", "null"],
            structured_output=True,
        )
        out = _run(seeded_db, llm, {"address": "object"})
        assert llm.json_schemas == [None] * 3
        for _system, user in llm.complete_calls:
            assert user.endswith(_LEGACY_ASK)
            assert _STRUCTURED_ASK not in user
        assert out.structured_content["records"][0]["address"] == {"street": "Main"}
        notice = out.structured_content["notice"]
        assert notice is not None
        assert "INFERENCE_STRUCTURED_OUTPUT" in notice
        assert notice in _texts(out)

    def test_unconvertible_schema_note_on_no_data(self, seeded_db):
        llm = FakeInferenceClient(complete_responses=["null"] * 3, structured_output=True)
        out = _run(seeded_db, llm, {"address": "object"})
        text = _texts(out)
        assert "No structured data matching the schema found in 3 threads." in text
        assert "INFERENCE_STRUCTURED_OUTPUT" in text

    def test_setting_off_keeps_todays_request_and_parse(self, seeded_db):
        llm = FakeInferenceClient(
            complete_responses=['```json\n{"vendor": "Acme"}\n```', "null", "[]"]
        )
        out = _run(seeded_db, llm, _SHORTHAND)
        assert llm.json_schemas == [None] * 3
        for _system, user in llm.complete_calls:
            assert user.endswith(_LEGACY_ASK)
        assert [r["vendor"] for r in out.structured_content["records"]] == ["Acme"]
        assert out.structured_content["notice"] is None

    def test_reserved_names_are_rejected_before_any_call(self, seeded_db):
        llm = FakeInferenceClient(structured_output=True)
        with pytest.raises(ToolError, match="reserved"):
            _run(seeded_db, llm, {"vendor": "string", "_evidence": "object"})
        assert llm.complete_calls == []


class TestRejectedStructuredRequest:
    def test_provider_400_names_the_setting_and_leaks_nothing(self, seeded_db, caplog):
        """End to end through the real anthropic backend: a 400 on a
        call that carried the format fails the tool with fixed text
        naming INFERENCE_STRUCTURED_OUTPUT=false, after one call, and
        neither the error nor the log quotes the provider's body."""
        import anthropic
        import httpx2

        client = InferenceClient.create(
            mode="anthropic",
            base_url="http://h.invalid",
            model="synthetic-model",
            api_key="sk-test",  # pragma: allowlist secret
        )
        request = httpx2.Request("POST", "http://h.invalid/v1/messages")
        response = httpx2.Response(400, request=request, json={"error": _MARKER})
        calls: list[dict] = []

        async def reject(**kwargs):
            calls.append(kwargs)
            raise anthropic.BadRequestError(
                f"rejected {_MARKER}", response=response, body={"error": _MARKER}
            )

        client._backend.client.messages.create = reject  # type: ignore[assignment]
        caplog.set_level(logging.DEBUG)
        with pytest.raises(ToolError) as err:
            _run(seeded_db, client, _SHORTHAND)
        assert "INFERENCE_STRUCTURED_OUTPUT=false" in str(err.value)
        assert _MARKER not in str(err.value)
        assert _MARKER not in caplog.text
        assert len(calls) == 1
        assert calls[0]["output_config"]["format"]["schema"] == _strict_records_schema(_SHORTHAND)


def test_records_wrapper_shape():
    wrapper = _strict_records_schema(_SHORTHAND)
    assert wrapper == {
        "type": "object",
        "properties": {"records": {"type": "array", "items": _strict_record_schema(_SHORTHAND)}},
        "required": ["records"],
        "additionalProperties": False,
    }
    json.dumps(wrapper)  # serializable as sent
