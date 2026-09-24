"""Offline guard for the API's structured-output schema limits, with a safety margin.

WHAT IS KNOWN (2026-09, checked against the API documentation):
  * DOCUMENTED (structured-outputs page, "JSON Schema limitations"): additionalProperties:false on every object;
    no recursive schemas; no numeric constraints (minimum, maximum, multipleOf); no string constraints
    (minLength, maxLength); array minItems only 0 or 1; enums of strings/numbers/bools/nulls only; external $ref
    unsupported; `allOf` with `$ref` unsupported.
  * OBSERVED (the API's own 400s on live calls): (1) "too many parameters with union types (49 parameters with type
    arrays or anyOf) ... limit: 16" for the nullable design; (2) "The compiled grammar is too large" for the
    per-field-object design (15 objects, 89 properties, 5.3 KB). The current array-of-entries design (5 objects,
    29 properties, 1.9 KB) was ACCEPTED by the API on its first probe (`llm-probe --schema`).
  * NOT PUBLISHED: the documentation states no numeric limit for optional parameters, properties, depth, enum size
    or schema size. The budgets below for those are OUR conservative self-imposed ceilings (well under anything
    plausible), not API facts. `python -m app.llm.probe --schema` is the authoritative check: the API compiles the
    real schema.

The design goal is stricter than the observed limit: ZERO unions, ZERO nulls, ZERO optional parameters.
"""
import json

import pytest

from app.extraction.wire import EVIDENCED_FIELDS, wire_schema

# Observed API limit, and the margin we demand on top of it (we require none at all).
API_UNION_LIMIT = 16
UNION_BUDGET = 0
# Self-imposed budgets for unpublished limits (see module docstring). Current values are far below each.
MAX_PROPERTIES = 120
MAX_OBJECTS = 25
MAX_DEPTH = 5            # nesting of object/array SCHEMAS (properties/items levels), not dict levels
MAX_ENUM_VALUES = 16
MAX_SCHEMA_BYTES = 12_000

DOCUMENTED_UNSUPPORTED = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength",
                          "maxLength", "pattern", "$ref", "$defs", "definitions", "uniqueItems", "maxItems", "contains",
                          "if", "then", "else", "not", "patternProperties", "propertyNames"}
UNION_KEYWORDS = {"anyOf", "oneOf", "allOf"}


def walk(node, visit, depth=0):
    """Visit every schema node; `depth` counts nested object/array schemas."""
    if isinstance(node, dict):
        visit(node, depth)
        for key, value in node.items():
            if key == "properties":
                for sub in value.values():
                    walk(sub, visit, depth + 1)
            elif key == "items":
                walk(value, visit, depth + 1)
            elif key in UNION_KEYWORDS:
                for sub in value:
                    walk(sub, visit, depth + 1)


def collect():
    schema = wire_schema()
    found = {"unions": 0, "type_arrays": 0, "nulls": 0, "objects": 0, "properties": 0, "optional": 0, "max_depth": 0,
             "enum_sizes": [], "keywords": set(), "open_objects": 0, "bad_minitems": 0}

    def visit(node, depth):
        found["max_depth"] = max(found["max_depth"], depth)
        found["keywords"] |= set(node)
        found["unions"] += sum(1 for k in UNION_KEYWORDS if k in node)
        if isinstance(node.get("type"), list):
            found["type_arrays"] += 1
        if node.get("type") == "null" or "null" in node.get("enum", []) or node.get("const") is None and "const" in node:
            found["nulls"] += 1
        if "enum" in node:
            found["enum_sizes"].append(len(node["enum"]))
        if node.get("type") == "object":
            found["objects"] += 1
            found["properties"] += len(node["properties"])
            found["optional"] += len(set(node["properties"]) - set(node.get("required", [])))
            if node.get("additionalProperties") is not False:
                found["open_objects"] += 1
        if node.get("minItems", 0) > 1:
            found["bad_minitems"] += 1

    walk(schema, visit)
    return schema, found


SCHEMA, FOUND = collect()


def test_zero_union_typed_parameters():
    assert FOUND["unions"] == 0 and FOUND["type_arrays"] == 0
    assert UNION_BUDGET < API_UNION_LIMIT                                     # the budget sits below the observed limit


def test_no_null_anywhere_in_the_schema():
    assert FOUND["nulls"] == 0
    assert '"null"' not in json.dumps(SCHEMA) and "null" not in json.dumps(SCHEMA).replace("nullable", "")


def test_no_optional_parameters_every_property_is_required():
    assert FOUND["optional"] == 0


def test_every_object_is_closed():
    assert FOUND["open_objects"] == 0


def test_none_of_the_documented_unsupported_keywords_appear():
    assert FOUND["keywords"] & DOCUMENTED_UNSUPPORTED == set()
    assert FOUND["bad_minitems"] == 0


def test_no_recursion_is_possible_because_there_are_no_references():
    assert "$ref" not in FOUND["keywords"] and "$defs" not in FOUND["keywords"]


def test_only_plain_json_types_are_used():
    types = set()
    walk(SCHEMA, lambda n, d: types.add(n["type"]) if "type" in n else None)
    assert types <= {"object", "array", "string", "integer", "number", "boolean"}


def test_enums_hold_only_strings_and_stay_small():
    def check(node, depth):
        if "enum" in node:
            assert all(isinstance(v, str) for v in node["enum"])
            assert len(node["enum"]) <= MAX_ENUM_VALUES
    walk(SCHEMA, check)
    assert max(FOUND["enum_sizes"]) <= MAX_ENUM_VALUES and FOUND["enum_sizes"]


def test_size_budgets_with_headroom():
    size = len(json.dumps(SCHEMA, separators=(",", ":")).encode("utf-8"))
    assert FOUND["properties"] <= MAX_PROPERTIES and FOUND["objects"] <= MAX_OBJECTS
    assert FOUND["max_depth"] <= MAX_DEPTH and size <= MAX_SCHEMA_BYTES


def test_the_schema_leaves_real_headroom_under_every_budget():
    """Budgets are ceilings, not targets: we must be at most ~75% of each."""
    size = len(json.dumps(SCHEMA, separators=(",", ":")).encode("utf-8"))
    assert FOUND["properties"] <= 0.75 * MAX_PROPERTIES
    assert FOUND["objects"] <= 0.75 * MAX_OBJECTS
    assert FOUND["max_depth"] <= MAX_DEPTH - 1
    assert max(FOUND["enum_sizes"]) <= 0.9 * MAX_ENUM_VALUES
    assert size <= 0.6 * MAX_SCHEMA_BYTES


def test_the_measured_shape_is_what_the_design_says():
    """Documents the current numbers so an accidental growth shows up in review."""
    assert (FOUND["objects"], FOUND["properties"]) == (5, 29)
    assert FOUND["max_depth"] == 3          # top object -> fields / line_items array -> item object -> scalar properties
    assert sorted(FOUND["enum_sizes"]) == [3, 3, 3, 6, 11]


def test_the_top_level_is_a_closed_object_with_all_properties_required():
    assert SCHEMA["type"] == "object" and SCHEMA["additionalProperties"] is False
    assert SCHEMA["required"] == list(SCHEMA["properties"])


def test_header_fields_are_one_array_of_entries_using_found_and_placeholders():
    fields = SCHEMA["properties"]["fields"]
    assert fields["type"] == "array"
    entry = fields["items"]
    assert list(entry["properties"]) == ["name", "found", "value", "page", "source_text", "confidence", "flag"]
    assert entry["properties"]["found"] == {"type": "boolean"} and entry["properties"]["value"] == {"type": "string"}
    assert entry["properties"]["page"] == {"type": "integer"} and entry["properties"]["confidence"] == {"type": "number"}
    assert entry["properties"]["name"]["enum"] == list(EVIDENCED_FIELDS) and len(EVIDENCED_FIELDS) == 11


def test_the_evidence_block_is_defined_once_not_once_per_field():
    text = json.dumps(SCHEMA)
    assert text.count('"found": {') == 1                                       # one entry definition for all 11 fields
    assert text.count('"confidence": {') == 3                                  # entry + line item + adjustment
    assert not any(name in SCHEMA["properties"] for name in EVIDENCED_FIELDS)  # no per-field properties at the top level


def test_tri_state_flags_are_enums_not_nullable_booleans():
    props = SCHEMA["properties"]
    for node in (props["fields"]["items"]["properties"]["flag"], props["document_quality"]["properties"]["contains_reader_instructions"]):
        assert node == {"type": "string", "enum": ["yes", "no", "unknown"]}


def test_the_schema_is_much_smaller_than_the_design_the_api_rejected():
    rejected = {"objects": 15, "properties": 89, "bytes": 5308}
    size = len(json.dumps(SCHEMA, separators=(",", ":")).encode("utf-8"))
    assert FOUND["objects"] <= 0.4 * rejected["objects"] and FOUND["properties"] <= 0.4 * rejected["properties"]
    assert size <= 0.4 * rejected["bytes"]


def test_the_checker_would_catch_a_regression_to_unions():
    """Meta-test: the same measurement on a schema with a nullable field must report unions."""
    bad = {"type": "object", "properties": {"a": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                            "b": {"type": ["string", "null"]}}, "required": ["a"], "additionalProperties": False}
    seen = {"unions": 0, "type_arrays": 0, "optional": 0}

    def visit(node, depth):
        seen["unions"] += sum(1 for k in UNION_KEYWORDS if k in node)
        seen["type_arrays"] += isinstance(node.get("type"), list)
        if node.get("type") == "object":
            seen["optional"] += len(set(node["properties"]) - set(node["required"]))

    walk(bad, visit)
    assert seen["unions"] == 1 and seen["type_arrays"] == 1 and seen["optional"] == 1
