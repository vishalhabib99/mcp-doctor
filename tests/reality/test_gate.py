"""End-to-end tests for the live call-time gate: real subprocess, real
stdio transport, real tool calls with agent-supplied (not generated)
arguments — same discipline as test_engine.py, applied to the per-call
wrapper instead of the batch audit."""

import asyncio
import sys
from contextlib import AsyncExitStack
from pathlib import Path

import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mcp_doctor.reality.gate import guarded_call

FIXTURE_SERVER = str(Path(__file__).parent / "fixtures" / "fixture_server.py")
TIMEOUT = 3.0


async def _call(tool_name: str, arguments: dict, timeout: float = TIMEOUT):
    params = StdioServerParameters(command=sys.executable, args=[FIXTURE_SERVER])
    async with AsyncExitStack() as stack:
        read, write = await stack.enter_async_context(stdio_client(params))
        session: ClientSession = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        tools = {t.name: t for t in (await session.list_tools()).tools}
        return await guarded_call(session, tools[tool_name], arguments, timeout=timeout)


def test_well_behaved_tool_is_not_flagged():
    result = asyncio.run(_call("well_behaved", {"city": "Paris"}))
    assert result.outcome == "ok"
    assert result.is_error is False
    assert result.refusal_in_disguise is None
    assert result.empty_content is False
    assert result.echo_mismatch_inputs == []
    assert result.flagged is False


def test_disguised_refusal_is_caught():
    result = asyncio.run(_call("secretly_refuses", {"city": "Paris"}))
    assert result.outcome == "ok"
    assert result.refusal_in_disguise is not None
    assert result.flagged is True


def test_empty_content_on_success_is_caught():
    result = asyncio.run(_call("returns_empty", {"city": "Paris"}))
    assert result.outcome == "ok"
    assert result.empty_content is True
    assert result.flagged is True


def test_honest_failure_is_not_flagged():
    result = asyncio.run(_call("genuinely_fails", {"city": "Paris"}))
    assert result.outcome == "ok"
    assert result.is_error is True
    # An honest, SDK-caught failure has nothing content-checked — same
    # reasoning as engine.py's identical branch.
    assert result.refusal_in_disguise is None
    assert result.empty_content is False
    assert result.flagged is False


def test_tool_with_no_string_args_has_no_echo_check():
    result = asyncio.run(_call("no_string_args", {"count": 3}))
    assert result.echo_mismatch_inputs is None


def test_process_crash_is_classified_as_crash_not_a_hang_or_silent_ok():
    result = asyncio.run(_call("kills_process", {"x": "anything"}))
    assert result.outcome == "crash"
    assert result.flagged is False  # nothing to content-check, no response exists


def test_hang_is_classified_as_timeout():
    result = asyncio.run(_call("hangs_forever", {"x": "anything"}, timeout=1.0))
    assert result.outcome == "timeout"
    assert "1.0s" in result.detail
