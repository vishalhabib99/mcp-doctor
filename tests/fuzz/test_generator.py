from mcp_doctor.fuzz.generator import (
    generate_valid_arguments,
    generate_valid_value,
    missing_required_variants,
    wrong_type_variants,
)


def test_string_respects_enum():
    assert generate_valid_value({"type": "string", "enum": ["b", "a"]}) == "b"


def test_string_respects_format():
    assert generate_valid_value({"type": "string", "format": "email"}) == "test@example.com"


def test_string_respects_min_length():
    value = generate_valid_value({"type": "string", "minLength": 8})
    assert len(value) >= 8


def test_integer_respects_minimum():
    assert generate_valid_value({"type": "integer", "minimum": 5}) == 5


def test_number_respects_exclusive_minimum():
    assert generate_valid_value({"type": "number", "exclusiveMinimum": 0}) == 1.0


def test_boolean():
    assert generate_valid_value({"type": "boolean"}) is True


def test_array_uses_items_schema():
    value = generate_valid_value({"type": "array", "items": {"type": "integer", "minimum": 3}})
    assert value == [3]


def test_nested_object_fills_required_fields():
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "age": {"type": "integer"},
        },
        "required": ["name"],
    }
    value = generate_valid_value(schema)
    assert value == {"name": "test"}


def test_anyof_uses_first_branch():
    schema = {"anyOf": [{"type": "integer", "minimum": 9}, {"type": "string"}]}
    assert generate_valid_value(schema) == 9


def test_generate_valid_arguments_top_level_schema():
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["query"],
    }
    args = generate_valid_arguments(schema)
    assert args == {"query": "test", "limit": 10}


def test_missing_required_variants_one_per_required_field():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "required": ["a", "b"],
    }
    variants = missing_required_variants(schema)
    names = {name for name, _ in variants}
    assert names == {"a", "b"}
    for name, args in variants:
        assert name not in args


def test_missing_required_variants_empty_when_nothing_required():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    assert missing_required_variants(schema) == []


def test_wrong_type_variants_swaps_each_typed_property():
    schema = {
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "flag": {"type": "boolean"},
        },
        "required": ["count", "flag"],
    }
    variants = wrong_type_variants(schema)
    by_name = dict(variants)
    assert isinstance(by_name["count"]["count"], str)
    assert isinstance(by_name["flag"]["flag"], str)


def test_wrong_type_variants_skips_untyped_property():
    schema = {
        "type": "object",
        "properties": {"anything": {"description": "no type declared"}},
        "required": ["anything"],
    }
    assert wrong_type_variants(schema) == []


def test_empty_schema_produces_no_variants():
    assert generate_valid_arguments(None) == {}
    assert missing_required_variants(None) == []
    assert wrong_type_variants(None) == []


# Adversarial/malformed metadata: a target server's `tools/list` response
# isn't guaranteed well-formed, and this module auditing that server should
# never itself crash on the server's own broken schema.

def test_non_dict_top_level_schema_produces_no_variants():
    for schema in (["not", "a", "schema"], "garbage", 42):
        assert generate_valid_arguments(schema) == {}
        assert missing_required_variants(schema) == []
        assert wrong_type_variants(schema) == []


def test_non_dict_properties_produces_no_variants():
    for properties in (["not", "a", "dict"], "garbage", 42):
        schema = {"type": "object", "properties": properties, "required": ["x"]}
        assert generate_valid_arguments(schema) == {}
        assert wrong_type_variants(schema) == []


def test_non_list_required_is_ignored_rather_than_iterated_char_by_char():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": "x"}
    assert missing_required_variants(schema) == []


def test_deeply_nested_schema_does_not_blow_recursion_limit():
    schema: dict = {"type": "string"}
    for _ in range(5000):
        schema = {"type": "object", "properties": {"x": schema}, "required": ["x"]}
    # No assertion on the exact value beyond the depth cutoff — the point is
    # that generation terminates instead of raising RecursionError.
    generate_valid_arguments(schema)


# A FastMCP tool taking a Pydantic model gets exactly this shape: the model in
# `$defs`, the parameter a bare `$ref`. Filling it with "test" made every such
# tool's valid call fail (batch-5 hunt, fabio-rovai/open-ontologies).
PYDANTIC_SCHEMA = {
    "$defs": {
        "Address": {
            "properties": {"city": {"type": "string"}, "zip": {"type": "integer"}},
            "required": ["city", "zip"],
            "type": "object",
        },
        "Person": {
            "properties": {
                "name": {"type": "string"},
                "address": {"$ref": "#/$defs/Address"},
                "tags": {"default": [], "items": {"$ref": "#/$defs/Address"}, "type": "array"},
            },
            "required": ["name", "address"],
            "type": "object",
        },
    },
    "properties": {"person": {"$ref": "#/$defs/Person"}, "note": {"type": "string"}},
    "required": ["person", "note"],
    "type": "object",
}


def test_local_ref_is_resolved_into_a_real_object():
    args = generate_valid_arguments(PYDANTIC_SCHEMA)
    assert args["person"] == {"name": "test", "address": {"city": "test", "zip": 1}, "tags": []}


def test_wrong_type_variant_covers_a_ref_parameter():
    names = [name for name, _ in wrong_type_variants(PYDANTIC_SCHEMA)]
    assert "person" in names
    args = dict(wrong_type_variants(PYDANTIC_SCHEMA))["person"]
    assert args["person"] == "not-an-object"


def test_single_schema_allof_wrapper_and_sibling_default():
    schema = {
        "$defs": {"Mode": {"type": "string", "enum": ["fast", "slow"]}},
        "properties": {
            "mode": {"allOf": [{"$ref": "#/$defs/Mode"}], "description": "how"},
            "level": {"$ref": "#/definitions/Level", "default": 3},
        },
        "definitions": {"Level": {"type": "integer"}},
        "required": ["mode", "level"],
    }
    assert generate_valid_arguments(schema) == {"mode": "fast", "level": 3}


def test_recursive_model_terminates_with_an_empty_child_list():
    schema = {
        "$defs": {
            "Node": {
                "properties": {
                    "label": {"type": "string"},
                    "children": {"items": {"$ref": "#/$defs/Node"}, "type": "array", "default": []},
                },
                "required": ["label", "children"],
                "type": "object",
            }
        },
        "properties": {"root": {"$ref": "#/$defs/Node"}},
        "required": ["root"],
    }
    assert generate_valid_arguments(schema) == {"root": {"label": "test", "children": []}}


def test_unresolvable_or_self_ref_does_not_raise():
    schema = {
        "properties": {
            "a": {"$ref": "#/$defs/Missing"},
            "b": {"$ref": "https://example.com/schema.json"},
            "c": {"$ref": "#"},
        },
        "required": ["a", "b", "c"],
    }
    args = generate_valid_arguments(schema)
    assert set(args) == {"a", "b", "c"}
