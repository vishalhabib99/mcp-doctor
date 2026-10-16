"""Runtime counterpart to the batch audit in `engine.py`: applies the same
deterministic content checks to one real tool call an agent actually makes,
instead of one synthetic call per tool in a one-shot report.

Where this differs from `engine.py`: that module connects to a server itself
and calls every tool once with realistic-*looking* generated arguments, to
produce a report. `guarded_call` doesn't connect to anything — it wraps a
call an agent's own `ClientSession` was already going to make, with the
agent's own real arguments, and checks the real response. Same checks
module, same no-LLM/no-API-key/zero-cost guarantee, different call site:
audit time vs. call time.

This is deliberately narrow: it answers "is this response real" (a
disguised refusal, empty content, a declared output schema the response
doesn't match), not "is this response safe" (secrets, prompt injection,
destructive actions) — that's a different, already well-served problem.
See README for the reasoning.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from mcp import ClientSession, types

from mcp_doctor.reality.checks import (
    check_echo_mismatch,
    check_empty_content,
    check_output_schema,
    check_refusal_in_disguise,
    response_text_from_content,
)
from mcp_doctor.reality.generator import string_argument_values

DEFAULT_TIMEOUT_SECONDS = 15.0


def _field(model, snake_name: str, camel_name: str):
    """Same cross-`mcp`-version compat as engine.py's identical helper."""
    if hasattr(model, snake_name):
        return getattr(model, snake_name)
    return getattr(model, camel_name)


@dataclass
class GateResult:
    tool_name: str
    # "ok" | "crash" | "timeout" — deliberately not the fuzzer's finer-
    # grained valid/missing_required/wrong_type classification: a live agent
    # sends one real call with real arguments, not a swept battery of
    # synthetic bad inputs, so there's nothing to distinguish "properly
    # rejected" from "crashed" the way mcp-fuzz's engine does for its own
    # use case. A raised/timed-out call here just means "no usable response
    # to content-check."
    outcome: str
    detail: str = ""
    is_error: bool = False
    response_text: str = ""
    refusal_in_disguise: str | None = None
    empty_content: bool = False
    schema_violation: str | None = None
    echo_mismatch_inputs: list[str] | None = None

    @property
    def flagged(self) -> bool:
        """True if this response is worth an agent treating with suspicion
        — not a schema-level failure (that's `outcome != "ok"` or
        `is_error`, both already-honest failures with nothing to disguise),
        but a *successful* response whose content doesn't hold up."""
        return bool(self.refusal_in_disguise or self.empty_content or self.schema_violation)


async def guarded_call(
    session: ClientSession,
    tool: types.Tool,
    arguments: dict,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> GateResult:
    """Calls `tool` via `session` with `arguments` — exactly what
    `session.call_tool(tool.name, arguments)` would do — and runs the same
    content checks `mcp-reality-check`'s batch audit uses against the real
    response, instead of a synthetic one.

    Use in an agent's own tool-calling loop in place of a bare
    `session.call_tool(...)`, to catch a disguised refusal, empty content,
    or a schema violation before it reaches the agent's context — rather
    than only ever finding these during a separate, one-shot audit run.
    """
    try:
        result = await asyncio.wait_for(
            session.call_tool(tool.name, arguments), timeout=timeout
        )
    except asyncio.TimeoutError:
        return GateResult(tool.name, "timeout", detail=f"no response within {timeout}s")
    except Exception as exc:
        return GateResult(tool.name, "crash", detail=f"{type(exc).__name__}: {exc}")

    is_error = bool(_field(result, "is_error", "isError")) if isinstance(result, types.CallToolResult) else False
    response_text = response_text_from_content(result.content) if hasattr(result, "content") else ""
    gate_result = GateResult(tool.name, "ok", is_error=is_error, response_text=response_text)

    if is_error:
        # Already honest about failing — nothing to content-check, same
        # reasoning as engine.py's identical branch.
        return gate_result

    gate_result.refusal_in_disguise = check_refusal_in_disguise(response_text)
    gate_result.empty_content = check_empty_content(response_text)
    gate_result.echo_mismatch_inputs = check_echo_mismatch(response_text, string_argument_values(arguments))

    output_schema = _field(tool, "output_schema", "outputSchema")
    structured = _field(result, "structured_content", "structuredContent")
    gate_result.schema_violation = check_output_schema(structured, output_schema)

    return gate_result
