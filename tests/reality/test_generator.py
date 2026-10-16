from mcp_doctor.reality.generator import generate_realistic_arguments, string_argument_values


def test_generates_realistic_value_for_known_property_names():
    schema = {
        "type": "object",
        "properties": {"city": {"type": "string"}, "email": {"type": "string"}},
        "required": ["city", "email"],
    }
    args = generate_realistic_arguments(schema)
    assert args["city"] == "Paris"
    assert "@" in args["email"]


def test_generic_property_name_falls_back_to_a_readable_string():
    schema = {
        "type": "object",
        "properties": {"widget_label": {"type": "string"}},
        "required": ["widget_label"],
    }
    args = generate_realistic_arguments(schema)
    assert "widget label" in args["widget_label"]


def test_hint_matching_is_whole_word_not_substring():
    # "id" is a substring of "widget" (w-i-d-...) — a naive `in` check on
    # the raw property name would wrongly match the "id" hint here. Caught
    # this while writing the test above; fixed via whole-token matching.
    schema = {
        "type": "object",
        "properties": {
            "widget_label": {"type": "string"},
            "video_title": {"type": "string"},
            "provider": {"type": "string"},
        },
        "required": ["widget_label", "video_title", "provider"],
    }
    args = generate_realistic_arguments(schema)
    assert args["widget_label"] != "example-id-123"
    assert args["video_title"] != "example-id-123"
    assert args["provider"] != "example-id-123"


def test_id_hint_still_matches_a_real_id_property():
    schema = {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]}
    args = generate_realistic_arguments(schema)
    assert args["user_id"] == "example-id-123"


def test_timezone_hint_produces_a_real_iana_name():
    # Found via real dogfooding against mcp-server-time: no "timezone" hint
    # existed at all, so `timezone`/`source_timezone`/`target_timezone`
    # fell back to a bare "example timezone" string — not a valid IANA
    # name, so every single call errored out with "Invalid timezone" and
    # there was nothing left to sanity-check. A generic fallback failing
    # this hard on a real, common property name is exactly the kind of gap
    # this generator exists to close.
    schema = {
        "type": "object",
        "properties": {
            "timezone": {"type": "string"},
            "source_timezone": {"type": "string"},
            "target_timezone": {"type": "string"},
        },
        "required": ["timezone", "source_timezone", "target_timezone"],
    }
    import zoneinfo

    args = generate_realistic_arguments(schema)
    for value in args.values():
        zoneinfo.ZoneInfo(value)  # raises if not a real IANA timezone name


def test_time_hint_matches_common_hh_mm_convention():
    schema = {"type": "object", "properties": {"time": {"type": "string"}}, "required": ["time"]}
    args = generate_realistic_arguments(schema)
    assert args["time"] == "12:00"


def test_respects_enum_and_const():
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["fast", "slow"]},
            "version": {"const": 2},
        },
        "required": ["mode", "version"],
    }
    args = generate_realistic_arguments(schema)
    assert args["mode"] == "fast"
    assert args["version"] == 2


def test_only_required_properties_are_included():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "required": ["a"],
    }
    args = generate_realistic_arguments(schema)
    assert "a" in args
    assert "b" not in args


def test_handles_array_and_nested_object():
    schema = {
        "type": "object",
        "properties": {
            "tags": {"type": "array", "items": {"type": "string"}},
            "address": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
        "required": ["tags", "address"],
    }
    args = generate_realistic_arguments(schema)
    assert isinstance(args["tags"], list) and len(args["tags"]) == 1
    assert args["address"]["city"] == "Paris"


def test_no_schema_returns_empty_arguments():
    assert generate_realistic_arguments(None) == {}
    assert generate_realistic_arguments({}) == {}


# Adversarial/malformed metadata: a target server's `tools/list` response
# isn't guaranteed well-formed, and this module checking that server should
# never itself crash on the server's own broken schema.

def test_non_dict_top_level_schema_returns_empty_arguments():
    for schema in (["not", "a", "schema"], "garbage", 42):
        assert generate_realistic_arguments(schema) == {}


def test_non_dict_properties_returns_empty_arguments():
    for properties in (["not", "a", "dict"], "garbage", 42):
        schema = {"type": "object", "properties": properties, "required": ["x"]}
        assert generate_realistic_arguments(schema) == {}


def test_non_list_required_is_treated_as_nothing_required():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": "x"}
    assert generate_realistic_arguments(schema) == {}


def test_deeply_nested_schema_does_not_blow_recursion_limit():
    schema: dict = {"type": "string"}
    for _ in range(5000):
        schema = {"type": "object", "properties": {"x": schema}, "required": ["x"]}
    # No assertion on the exact value beyond the depth cutoff — the point is
    # that generation terminates instead of raising RecursionError.
    generate_realistic_arguments(schema)


def test_string_argument_values_flattens_and_filters_short_values():
    args = {"city": "Paris", "count": 3, "tags": ["ok", "example thing"], "nested": {"x": "Berlin"}}
    values = string_argument_values(args)
    assert "Paris" in values
    assert "Berlin" in values
    assert "ok" not in values  # too short to be a meaningful signal
    assert "example thing" not in values  # generic fallback, filtered out
