"""Count descriptions are part of the MCP wire contract (#1303)."""

import inspect

from pydantic import BaseModel
from src.tools import outputs

from tests.test_tool_annotations import _server, _wire_tools

# Positions, dimensions and echoed arguments are not population counts.
_EXCLUDED = {
    "item": "zero-based item index",
    "record": "zero-based record index",
    "statement": "zero-based statement index",
    "chunk_index": "source chunk index",
    "char_start": "character offset",
    "char_end": "character offset",
    "offset": "pagination position",
    "next_offset": "pagination position",
    "text_offset": "character offset",
    "body_offset": "character offset",
    "size_bytes": "file size measurement",
    "sync_interval_secs": "duration setting",
    "index_schema_version": "schema version identifier",
    "value": "echoed heterogeneous filter operand",
    "lane_ranks": "retrieval lane positions",
    "max_threads": "requested thread ceiling",
    "limit": "requested page ceiling",
    "sample_per_group": "requested sample ceiling",
}


def _integer(shape):
    if not isinstance(shape, dict):
        return False
    return shape.get("type") == "integer" or any(_integer(s) for s in shape.get("anyOf", []))


def _integer_values(shape):
    if not isinstance(shape, dict):
        return False
    return _integer(shape) or (
        "additionalProperties" in shape and _integer_values(shape["additionalProperties"])
    )


def _check(schema, location):
    checked = 0
    for name, shape in schema.get("properties", {}).items():
        numeric = _integer_values(shape)
        if numeric and name not in _EXCLUDED:
            description = shape.get("description", "")
            assert description.startswith("Unit: "), (
                f"{location}.{name} has no counting unit: {description!r}"
            )
            checked += 1
        checked += _check(shape, f"{location}.{name}")
    if isinstance(schema.get("items"), dict):
        checked += _check(schema["items"], location + "[]")
    for alternative in schema.get("anyOf", []):
        checked += _check(alternative, location)
    for name, definition in schema.get("$defs", {}).items():
        checked += _check(definition, name)
    return checked


def test_every_model_integer_count_names_its_unit():
    count = 0
    for name, model in inspect.getmembers(outputs, inspect.isclass):
        if issubclass(model, BaseModel) and model.__module__ == outputs.__name__:
            count += _check(model.model_json_schema(), name)
    assert count > 50


def test_counting_units_reach_clients_on_the_wire(empty_db):
    tools = _wire_tools(_server(empty_db))
    count = sum(_check(tool["outputSchema"], name) for name, tool in tools.items())
    assert count > 50
