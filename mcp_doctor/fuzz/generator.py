"""Generates test-call arguments from a tool's JSON input schema.

Three kinds of test cases, all derived purely from the schema (no LLM, no
network calls of its own):

- a "valid" call: one plausible value per property, respecting `type`,
  `enum`, `minimum`/`maximum`, `minLength`/`maxLength`, and `format` where
  present, so a well-behaved tool should accept it without complaint.
- a "missing required" call per required property: the valid call with that
  one property removed, to check the server returns a structured error
  instead of crashing or silently proceeding with an absent argument.
- a "wrong type" call per property with an unambiguous `type`: the valid
  call with that one property swapped for a value of a different JSON type,
  same reasoning.

Anything the schema doesn't pin down (no `type`, an `anyOf`/`oneOf` with
genuinely different shapes, a `$ref` that doesn't point inside the schema)
is left out of the wrong-type set rather than guessed at — a value that
might legitimately be valid isn't a useful "wrong type" test case. Local
`$ref`s (`#/$defs/...`, `#/definitions/...`) are followed: a Pydantic model
parameter in FastMCP always arrives that way, and filling it with a
placeholder string made every such tool's valid call fail.

A schema here comes straight off the wire from `tools/list` on whatever
server is being fuzzed — nothing in the MCP spec or SDK guarantees it's
well-formed. `properties`/`required` can be the wrong JSON type, or the
whole schema can be nested deep enough to blow Python's recursion limit.
Every public entry point below treats a malformed or adversarial schema as
"generate nothing useful" rather than letting the exception propagate —
this module auditing a target server should never crash on that target's
own broken metadata.
"""

from __future__ import annotations

from typing import Any

_MAX_SCHEMA_DEPTH = 50

_STRING_FORMAT_SAMPLES = {
    "date": "2026-01-01",
    "date-time": "2026-01-01T00:00:00Z",
    "email": "test@example.com",
    "uri": "https://example.com",
    "uuid": "00000000-0000-0000-0000-000000000000",
    "hostname": "example.com",
    "ipv4": "127.0.0.1",
    "ipv6": "::1",
}


def _sample_string(schema: dict[str, Any]) -> str:
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    fmt = schema.get("format")
    if fmt in _STRING_FORMAT_SAMPLES:
        return _STRING_FORMAT_SAMPLES[fmt]
    base = "test"
    min_len = schema.get("minLength")
    if isinstance(min_len, int) and min_len > len(base):
        base = base + "x" * (min_len - len(base))
    max_len = schema.get("maxLength")
    if isinstance(max_len, int) and max_len < len(base):
        base = base[:max_len] if max_len > 0 else ""
    return base


def _sample_number(schema: dict[str, Any], integer: bool) -> int | float:
    minimum = schema.get("minimum", schema.get("exclusiveMinimum"))
    maximum = schema.get("maximum", schema.get("exclusiveMaximum"))
    if isinstance(minimum, (int, float)):
        value = minimum + (1 if "exclusiveMinimum" in schema else 0)
    elif isinstance(maximum, (int, float)):
        value = maximum - (1 if "exclusiveMaximum" in schema else 0)
    else:
        value = 1
    return int(value) if integer else float(value)


def _resolve_ref(schema: dict[str, Any], root: dict[str, Any] | None, seen: frozenset[str]) -> tuple[dict[str, Any], frozenset[str]]:
    """Follows a local `$ref` (and a single-schema `allOf`, which older
    Pydantic wraps around one) against the tool's own input schema.

    Sibling keywords next to the `$ref` (`default`, `description`) are kept
    and win over the target's. A ref already being expanded on this path
    (a recursive model) or one that doesn't resolve is dropped, leaving
    whatever siblings remain, so generation always terminates."""
    for _ in range(_MAX_SCHEMA_DEPTH):
        all_of = schema.get("allOf")
        if isinstance(all_of, list) and len(all_of) == 1 and isinstance(all_of[0], dict):
            schema = {**all_of[0], **{k: v for k, v in schema.items() if k != "allOf"}}
            continue
        ref = schema.get("$ref")
        if not isinstance(ref, str):
            return schema, seen
        siblings = {k: v for k, v in schema.items() if k != "$ref"}
        target: Any = root if ref.startswith("#") else None
        if ref in seen:
            target = None
        else:
            for part in ref[1:].split("/")[1:] if ref.startswith("#/") else ([] if ref == "#" else [None]):
                if not isinstance(target, dict) or part is None:
                    target = None
                    break
                target = target.get(part.replace("~1", "/").replace("~0", "~"))
        if not isinstance(target, dict):
            return siblings, seen
        schema = {**target, **siblings}
        seen = seen | {ref}
    return schema, seen


def generate_valid_value(
    schema: dict[str, Any], _depth: int = 0, _root: dict[str, Any] | None = None, _seen: frozenset[str] = frozenset(),
) -> Any:
    """One plausible value for a single property's schema fragment.

    `_depth` guards against a schema nested deeper than any real tool would
    need — a malformed or adversarial `object`/`array` schema that nests
    thousands of levels deep would otherwise blow Python's recursion limit
    before this ever gets to run a single test call."""
    if _depth > _MAX_SCHEMA_DEPTH:
        return "test"
    schema, _seen = _resolve_ref(schema, _root, _seen)
    if "const" in schema:
        return schema["const"]
    if "default" in schema:
        return schema["default"]
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    for combinator in ("anyOf", "oneOf"):
        if combinator in schema and schema[combinator]:
            return generate_valid_value(schema[combinator][0], _depth + 1, _root, _seen)
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = next((t for t in schema_type if t != "null"), schema_type[0])
    if schema_type == "string":
        return _sample_string(schema)
    if schema_type == "integer":
        return _sample_number(schema, integer=True)
    if schema_type == "number":
        return _sample_number(schema, integer=False)
    if schema_type == "boolean":
        return True
    if schema_type == "array":
        items_schema = schema.get("items", {})
        if (
            isinstance(items_schema, dict)
            and items_schema.get("$ref") in _seen
            and not schema.get("minItems")
        ):
            return []  # a recursive model's child list: empty is valid and ends the recursion
        min_items = schema.get("minItems", 1) or 1
        sample = (
            generate_valid_value(items_schema, _depth + 1, _root, _seen)
            if isinstance(items_schema, dict)
            else "test"
        )
        return [sample for _ in range(max(min_items, 1))]
    if schema_type == "object" or (schema_type is None and isinstance(schema.get("properties"), dict)):
        return generate_valid_object(schema, _depth + 1, _root, _seen)
    if schema_type == "null":
        return None
    # No usable type information — a generic placeholder is better than
    # omitting the property outright, which would itself look like a
    # missing-required-field test rather than a valid call.
    return "test"


def generate_valid_object(
    schema: dict[str, Any], _depth: int = 0, _root: dict[str, Any] | None = None, _seen: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """A plausible object for an `{"type": "object", "properties": {...}}`
    schema — every `required` property filled in, plus any optional
    property that itself has an `enum`/`const`/`default` (cheap to include,
    makes the "valid" call more representative)."""
    if _depth > _MAX_SCHEMA_DEPTH:
        return {}
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        # A server returning `properties` as something other than an
        # object is malformed metadata, not a real schema — nothing to
        # generate from it, same as no properties at all.
        properties = {}
    required_raw = schema.get("required", [])
    required = set(required_raw) if isinstance(required_raw, list) else set()
    result: dict[str, Any] = {}
    for name, prop_schema in properties.items():
        if not isinstance(prop_schema, dict):
            continue
        if name in required or any(k in prop_schema for k in ("enum", "const", "default")):
            result[name] = generate_valid_value(prop_schema, _depth + 1, _root, _seen)
    return result


def generate_valid_arguments(input_schema: dict[str, Any] | None) -> dict[str, Any]:
    """The full argument dict for a tool call that should succeed."""
    if not isinstance(input_schema, dict):
        return {}
    return generate_valid_object(input_schema, _root=input_schema)


def missing_required_variants(input_schema: dict[str, Any] | None) -> list[tuple[str, dict[str, Any]]]:
    """(property_name, arguments) for each required property, omitted one
    at a time from an otherwise-valid call."""
    if not isinstance(input_schema, dict):
        return []
    required = input_schema.get("required", [])
    if not isinstance(required, list) or not required:
        return []
    base = generate_valid_arguments(input_schema)
    variants = []
    for name in required:
        args = dict(base)
        args.pop(name, None)
        variants.append((name, args))
    return variants


_WRONG_TYPE_SAMPLES: dict[str, Any] = {
    "string": 12345,
    "integer": "not-a-number",
    "number": "not-a-number",
    "boolean": "not-a-boolean",
    "array": "not-an-array",
    "object": "not-an-object",
}


def _wrong_type_value(schema: dict[str, Any]) -> Any | None:
    schema_type = schema.get("type")
    if isinstance(schema_type, list) or schema_type is None:
        return None
    return _WRONG_TYPE_SAMPLES.get(schema_type)


def wrong_type_variants(input_schema: dict[str, Any] | None) -> list[tuple[str, dict[str, Any]]]:
    """(property_name, arguments) for each property whose schema has an
    unambiguous single `type`, with that one property swapped to a value of
    a different JSON type in an otherwise-valid call."""
    if not isinstance(input_schema, dict):
        return []
    properties = input_schema.get("properties", {})
    if not isinstance(properties, dict) or not properties:
        return []
    base = generate_valid_arguments(input_schema)
    variants = []
    for name, prop_schema in properties.items():
        if not isinstance(prop_schema, dict):
            continue
        wrong = _wrong_type_value(_resolve_ref(prop_schema, input_schema, frozenset())[0])
        if wrong is None:
            continue
        args = dict(base)
        args[name] = wrong
        variants.append((name, args))
    return variants
