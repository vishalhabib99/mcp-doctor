"""Generates one plausible set of arguments per tool from its JSON schema.

Deliberately more realistic than a bare placeholder ("test"): a property
named `city` gets a real city name, `email` gets a real-shaped email, `date`
gets today's date, etc. This matters here in a way it doesn't for mcp-fuzz
(mcp-doctor's sibling, which fuzzes with intentionally *bad* input) — this
tool calls a tool once with its *best-effort valid* input and then checks
whether the response is a genuine, on-topic answer. A response can't
plausibly reference "test" back, but it very well might reference "Paris".
Still schema-only and heuristic, not LLM-generated — no guarantee a given
server actually has a matching real "Paris", just a more realistic prior
than a bare placeholder.

A schema here comes straight off the wire from `tools/list` on whatever
server is being checked — nothing guarantees it's well-formed. See
mcp-fuzz's generator.py for the sibling tool's version of this same
hardening (found via the same real class of malformed metadata: non-dict
`properties`/schema, and schemas nested deep enough to blow Python's
recursion limit) — this module needed the identical fix.
"""

from __future__ import annotations

import datetime
import re
from typing import Any

_MAX_SCHEMA_DEPTH = 50

_NAME_HINTS: list[tuple[tuple[str, ...], Any]] = [
    (("email",), "jane.doe@example.com"),
    (("url", "uri", "link", "endpoint", "website"), "https://example.com"),
    (("date",), datetime.date.today().isoformat()),
    (("time",), "12:00"),
    (("timezone", "tz"), "America/New_York"),
    (("city",), "Paris"),
    (("country",), "France"),
    (("name",), "Jane Doe"),
    (("phone",), "+1-555-0100"),
    (("path", "file", "filename", "filepath"), "/tmp/example.txt"),
    (("query", "search", "keyword", "keywords", "q"), "example search query"),
    (("id",), "example-id-123"),
    (("token", "key", "secret", "password"), "example-token-123"),
    (("title",), "Example Title"),
    (("description",), "An example description"),
    (("language", "lang"), "en"),
    (("currency",), "USD"),
]


def _tokens(prop_name: str) -> set[str]:
    """Splits snake_case/kebab-case/camelCase into lowercase whole-word
    tokens. Whole-word, not substring, matching matters here: a naive
    substring check on a hint like "id" would also match inside "widget",
    "video", "provide" — real property names, not IDs at all."""
    with_underscores = re.sub(r"(?<!^)(?=[A-Z])", "_", prop_name)
    return {t.lower() for t in re.split(r"[_\-\s]+", with_underscores) if t}


def _realistic_string(prop_name: str) -> str:
    tokens = _tokens(prop_name)
    for hints, value in _NAME_HINTS:
        if tokens & set(hints):
            return value
    return f"example {prop_name.replace('_', ' ')}".strip()


def _value_for_schema(prop_name: str, schema: dict, _depth: int = 0) -> Any:
    if not isinstance(schema, dict):
        # Malformed metadata (e.g. a property's schema fragment isn't an
        # object at all) — nothing to infer from it, same fallback as no
        # usable type info below.
        return _realistic_string(prop_name)
    if _depth > _MAX_SCHEMA_DEPTH:
        # Guards against a schema nested deep enough to blow Python's
        # recursion limit — no real tool schema goes anywhere near this,
        # only a malformed or adversarial one would.
        return _realistic_string(prop_name)
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]

    json_type = schema.get("type")
    if isinstance(json_type, list):
        json_type = next((t for t in json_type if t != "null"), json_type[0] if json_type else None)

    if json_type == "string":
        fmt = schema.get("format")
        if fmt == "date":
            return datetime.date.today().isoformat()
        if fmt == "date-time":
            return datetime.datetime.now().isoformat()
        if fmt in ("uri", "url"):
            return "https://example.com"
        if fmt == "email":
            return "jane.doe@example.com"
        value = _realistic_string(prop_name)
        min_len = schema.get("minLength")
        if isinstance(min_len, int) and len(value) < min_len:
            value = value + ("x" * (min_len - len(value)))
        return value
    if json_type == "integer":
        minimum = schema.get("minimum")
        return int(minimum) if isinstance(minimum, (int, float)) else 1
    if json_type == "number":
        minimum = schema.get("minimum")
        return float(minimum) if isinstance(minimum, (int, float)) else 1.0
    if json_type == "boolean":
        return True
    if json_type == "array":
        item_schema = schema.get("items", {"type": "string"})
        return [_value_for_schema(prop_name, item_schema, _depth + 1)]
    if json_type == "object":
        return _object_value(schema, _depth + 1)
    # No usable type info (bare {}, ambiguous anyOf/oneOf, ...): fall back to
    # a realistic string rather than guessing at a structure we can't infer.
    return _realistic_string(prop_name)


def _object_value(schema: dict, _depth: int = 0) -> dict:
    if _depth > _MAX_SCHEMA_DEPTH:
        return {}
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        # A server returning `properties` as something other than an
        # object is malformed metadata, not a real schema — nothing to
        # generate from it, same as no properties at all.
        properties = {}
    required = schema.get("required", list(properties.keys()))
    if not isinstance(required, list):
        # Malformed `required` (e.g. a bare string): `name in required`
        # would silently do substring matching instead of raising, which
        # is worse than a crash — treat it the same as "nothing required".
        required = []
    return {
        name: _value_for_schema(name, prop_schema, _depth + 1)
        for name, prop_schema in properties.items()
        if name in required
    }


def generate_realistic_arguments(input_schema: dict | None) -> dict:
    """One best-effort realistic value per required (and any strongly-typed
    optional) property — mirrors mcp-fuzz's `generate_valid_arguments` shape
    but biases string values toward plausible content instead of a bare
    placeholder, since the whole point here is judging whether the response
    is a genuine answer."""
    if not isinstance(input_schema, dict):
        return {}
    return _object_value(input_schema)


def string_argument_values(arguments: dict) -> list[str]:
    """Every string leaf value used in a call's arguments, for the echo/
    relevance check — flattened out of nested lists/objects too. Filters out
    very short or generic-looking values, which are too likely to appear in
    any response by coincidence to be a meaningful signal either way."""
    values: list[str] = []

    def _walk(v: Any) -> None:
        if isinstance(v, str):
            if len(v) >= 4 and not v.startswith("example "):
                values.append(v)
        elif isinstance(v, list):
            for item in v:
                _walk(item)
        elif isinstance(v, dict):
            for item in v.values():
                _walk(item)

    _walk(arguments)
    return values
