"""Heuristic checks run against one successful tool response.

Deliberately not LLM-judged (see README for why) — every check here is a
plain, deterministic rule, in the same spirit as mcp-doctor and mcp-fuzz:
no network calls beyond the one to the target server, no API key, fully
reproducible. That means real recall limits (a refusal phrased in a way the
pattern list doesn't cover won't be caught) in exchange for zero cost and
zero flakiness — a deliberate tradeoff, not an oversight.

Checks are split the same way mcp-fuzz splits crashes from "valid_call_
errored": REFUSAL_IN_DISGUISE and EMPTY_CONTENT are confident enough to
score. ECHO_MISMATCH is a weaker signal (a real, on-topic answer may
legitimately not repeat the input back verbatim) and is reported
separately, flagged as worth a manual look rather than folded into the
score — the same "don't overclaim a heuristic" discipline used throughout
this family of tools.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from mcp_doctor.guard import schema_error

# Phrases specific enough that a genuine, on-topic answer is unlikely to
# contain them incidentally. Deliberately conservative (full phrases, not
# single words like "sorry" or "can't", which show up in legitimate content
# too) — a missed refusal is better than a false accusation here.
_REFUSAL_PATTERNS = [
    r"\bi (?:can(?:not|'t)|am unable to|'m unable to|do not have access|don't have access|"
    r"am not able to|'m not able to|don't have the ability|do not have the ability)\b",
    # "As an AI," / "As an AI language model," - not "as an AI Product Manager".
    r"\bas an ai(?: (?:language )?model| assistant)?,",
    r"\bi'm sorry, but\b",
    r"\bunfortunately,? i (?:cannot|can't|am unable|don't|do not)\b",
    # "I have no access to" - not prose like "an agent has no access to server source".
    r"\bi(?: have|'ve| had) no access to\b",
    r"\bnot authorized to\b",
    r"\bunable to (?:process|complete|fulfill) this request\b",
    r"\bi don't have (?:the )?(?:permission|access|ability) to\b",
]
_REFUSAL_RE = re.compile("|".join(_REFUSAL_PATTERNS), re.IGNORECASE)


@dataclass
class SanityResult:
    tool_name: str
    tested: bool
    skip_reason: str | None = None
    refusal_in_disguise: str | None = None  # matched phrase, if any
    empty_content: bool = False
    schema_violation: str | None = None
    echo_mismatch_inputs: list[str] | None = None  # None = check not applicable
    response_text: str = ""


def response_text_from_content(content_blocks: list) -> str:
    texts = []
    for block in content_blocks:
        text = getattr(block, "text", None)
        if text:
            texts.append(text)
    return "\n".join(texts)


# A disguised refusal IS the answer, so it shows up at the start of the response. A phrase deep
# inside a long response is a document quoting one (a README describing refusals, a transcript),
# and flagging it turned every file-reading tool into a false positive. 200 chars leaves room for
# a short preamble or a JSON wrapper like {"result": "I'm sorry, but ..."}.
REFUSAL_WINDOW_CHARS = 200


def check_refusal_in_disguise(response_text: str) -> str | None:
    match = _REFUSAL_RE.search(response_text.lstrip())
    if match and match.start() < REFUSAL_WINDOW_CHARS:
        return match.group(0)
    return _error_payload(response_text)


# A JSON record of a job, run or check can carry its own "error" field and still be a
# real answer ("this run failed because ..."), so a status/state key means a record.
_RECORD_KEYS = {"status", "state"}


def _error_payload(response_text: str) -> str | None:
    """A "successful" response whose whole body is a JSON error object, like
    {"error": "Connection refused", "hint": "..."}: the tool failed and said so in
    the text, but isError is false, so a client checking the flag counts a success."""
    text = response_text.strip()
    if not text.startswith("{"):
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    if not isinstance(obj, dict) or _RECORD_KEYS & obj.keys():
        return None
    error = obj.get("error")
    if isinstance(error, dict):
        error = error.get("message") or error.get("detail")
    if not isinstance(error, str) or not error.strip():
        return None
    # Real data next to the error (a list of results, a nested object) means a
    # partial answer, not a failure; only flag when everything else is short metadata.
    if any(isinstance(v, (list, dict)) and v for k, v in obj.items() if k != "error"):
        return None
    return f'error payload: "{error.strip()[:60]}"'


def check_empty_content(response_text: str) -> bool:
    return len(response_text.strip()) == 0


def check_echo_mismatch(response_text: str, input_strings: list[str]) -> list[str] | None:
    """Returns the list of meaningful input strings that never appear
    anywhere in the response — an empty list means every one was found, a
    non-empty list is the "worth investigating" flag. None means there was
    nothing meaningful to check (no realistic string inputs to look for)."""
    if not input_strings:
        return None
    lowered_response = response_text.lower()
    missing = [s for s in input_strings if s.lower() not in lowered_response]
    return missing


def check_output_schema(structured_content: dict | None, output_schema: dict | None) -> str | None:
    """Validates a tool's declared `outputSchema` against its actual
    structured response, when both exist. The one fully hard, non-heuristic
    check here — either the response matches the schema the server itself
    published, or it doesn't."""
    if output_schema is None or structured_content is None:
        return None
    # mcp-doctor's own validator (shared with guard) instead of the jsonschema
    # package: it covers the keywords outputs use in practice and accepts
    # anything it doesn't understand rather than guess.
    return schema_error(structured_content, output_schema)
