import json

from mcp_doctor.reality.checks import (
    check_echo_mismatch,
    check_empty_content,
    check_output_schema,
    check_refusal_in_disguise,
)


def test_refusal_in_disguise_catches_common_phrasings():
    assert check_refusal_in_disguise("I don't have access to real-time data.") is not None
    assert check_refusal_in_disguise("I'm sorry, but I cannot do that.") is not None
    assert check_refusal_in_disguise("As an AI, I have no way to browse the web.") is not None
    assert check_refusal_in_disguise("Unfortunately, I cannot process this request.") is not None


def test_refusal_in_disguise_does_not_flag_a_genuine_answer():
    assert check_refusal_in_disguise("The weather in Paris is sunny and 72F.") is None
    # "sorry" alone, in a non-refusal context, shouldn't trip the check —
    # only specific refusal phrasings should.
    assert check_refusal_in_disguise("Sorry for the delay, here are your results: 3 items found.") is None


def test_empty_content():
    assert check_empty_content("") is True
    assert check_empty_content("   \n  ") is True
    assert check_empty_content("actual content") is False


def test_echo_mismatch_returns_none_when_nothing_to_check():
    assert check_echo_mismatch("some response", []) is None


def test_echo_mismatch_finds_present_and_missing_values():
    response = "The weather in Paris is sunny."
    assert check_echo_mismatch(response, ["Paris"]) == []
    assert check_echo_mismatch(response, ["Paris", "Berlin"]) == ["Berlin"]
    assert check_echo_mismatch(response, ["Berlin"]) == ["Berlin"]


def test_echo_mismatch_is_case_insensitive():
    assert check_echo_mismatch("The weather in PARIS is sunny.", ["Paris"]) == []


def test_output_schema_skips_when_either_side_missing():
    assert check_output_schema(None, {"type": "object"}) is None
    assert check_output_schema({"x": 1}, None) is None


def test_output_schema_catches_a_real_violation():
    schema = {"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]}
    assert check_output_schema({"count": "not a number"}, schema) is not None
    assert check_output_schema({}, schema) is not None


def test_output_schema_passes_a_matching_response():
    schema = {"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]}
    assert check_output_schema({"count": 3}, schema) is None


def test_refusal_must_be_the_answer_not_a_quote_deep_in_a_document():
    # Found by reading real READMEs through server-filesystem: a document that *mentions* a
    # refusal phrase was flagged as a disguised refusal. Over ~/code (12,445 text files) the
    # old check flagged 38 files; with the 200-char window and tightened phrases, 0.
    readme = "# Project\n\n" + "Plain documentation. " * 300 + 'refusals look like "I don\'t have access to..."'
    assert check_refusal_in_disguise(readme) is None
    assert check_refusal_in_disguise('{"result": "I\'m sorry, but I can\'t provide that."}') is not None
    long_refusal = "I'm sorry, but I can't provide that. " + "Here is why. " * 100
    assert check_refusal_in_disguise(long_refusal) is not None


def test_tightened_phrases_skip_ordinary_prose():
    assert check_refusal_in_disguise("Structured record of work as an AI Product Manager.") is None
    assert check_refusal_in_disguise("An agent has no access to server source, only metadata.") is None
    assert check_refusal_in_disguise("As an AI language model, I cannot browse the internet.") is not None
    assert check_refusal_in_disguise("I have no access to the user's calendar.") is not None


def test_json_error_payload_reported_as_success_is_flagged():
    # bug #137: astro-airflow-mcp answered every call with Airflow down like this, isError false
    text = json.dumps({
        "error": "Failed to detect Airflow version at http://127.0.0.1:9. Connection refused",
        "error_type": "RuntimeError",
        "hint": "The Airflow API call failed",
        "retryable": False,
        "dag_id": "x",
    }, indent=2)
    assert check_refusal_in_disguise(text).startswith('error payload: "Failed to detect Airflow')
    assert check_refusal_in_disguise('{"error": {"message": "Unauthorized", "code": 401}}')
    assert check_refusal_in_disguise('{"success": false, "error": "not found"}')


def test_json_error_field_in_real_data_is_not_flagged():
    assert check_refusal_in_disguise('{"error": null, "rows": 3}') is None
    assert check_refusal_in_disguise('{"error": "", "result": "ok"}') is None
    # a record that reports a failure is still a real answer
    assert check_refusal_in_disguise('{"run_id": "r1", "state": "failed", "error": "OOM"}') is None
    assert check_refusal_in_disguise('{"status": "error", "error": "timeout"}') is None
    # partial results next to an error
    assert check_refusal_in_disguise('{"results": [{"id": 1}], "error": "page 2 failed"}') is None
    assert check_refusal_in_disguise('[{"error": "x"}]') is None
    assert check_refusal_in_disguise('error: not json') is None
