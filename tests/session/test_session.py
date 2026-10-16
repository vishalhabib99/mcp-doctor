"""End-to-end tests: real subprocess, real stdio transport — GuardedSession
composes three separately-published packages, so this is entirely wiring
to verify, not new heuristic logic (each sibling package already tests its
own checks in isolation)."""

import asyncio
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mcp_doctor.session import GuardedSession

FIXTURE_SERVER = str(Path(__file__).parent / "fixtures" / "fixture_server.py")


async def _guarded_session():
    params = StdioServerParameters(command=sys.executable, args=[FIXTURE_SERVER])
    stack = AsyncExitStack()
    read, write = await stack.enter_async_context(stdio_client(params))
    session: ClientSession = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return GuardedSession(session), stack


def test_list_tools_runs_registration_checks_on_every_tool():
    async def run():
        gs, stack = await _guarded_session()
        async with stack:
            await gs.list_tools()
            assert "well_behaved" in gs.registration_results
            assert "kills_process" in gs.registration_results
            # every fixture tool has a real docstring-derived description, except the pair
            # planted with a copy-pasted description for the look-alike check
            planted = {"lookup_order", "cancel_order"}
            assert all(not r.flagged for n, r in gs.registration_results.items() if n not in planted)
            assert all(gs.registration_results[n].flagged for n in planted)

    asyncio.run(run())


def test_well_behaved_call_is_clean():
    async def run():
        gs, stack = await _guarded_session()
        async with stack:
            await gs.list_tools()
            result = await gs.call_tool("well_behaved", {"city": "Paris"})
            assert result.outcome == "ok"
            assert result.flagged is False

    asyncio.run(run())


def test_disguised_refusal_is_caught():
    async def run():
        gs, stack = await _guarded_session()
        async with stack:
            await gs.list_tools()
            result = await gs.call_tool("secretly_refuses", {"city": "Paris"})
            assert result.refusal_in_disguise is not None
            assert result.flagged is True

    asyncio.run(run())


def test_empty_content_is_caught():
    async def run():
        gs, stack = await _guarded_session()
        async with stack:
            await gs.list_tools()
            result = await gs.call_tool("returns_empty", {"city": "Paris"})
            assert result.empty_content is True
            assert result.flagged is True

    asyncio.run(run())


def test_process_crash_is_classified_as_crash():
    async def run():
        gs, stack = await _guarded_session()
        async with stack:
            await gs.list_tools()
            result = await gs.call_tool("kills_process", {"x": "anything"})
            assert result.outcome == "crash"
            assert result.flagged is False  # nothing to content-check

    asyncio.run(run())


def test_hang_is_classified_as_timeout():
    async def run():
        gs, stack = await _guarded_session()
        async with stack:
            await gs.list_tools()
            result = await gs.call_tool("hangs_forever", {"x": "anything"}, timeout=1.0)
            assert result.outcome == "timeout"

    asyncio.run(run())


def test_repeated_calls_to_the_same_tool_share_latency_history():
    # Verifies GuardedSession's LatencyGate state actually persists across
    # calls made through the same instance, not reset each time.
    async def run():
        gs, stack = await _guarded_session()
        async with stack:
            await gs.list_tools()
            for _ in range(3):
                await gs.call_tool("well_behaved", {"city": "Paris"})
            history = gs._latency_gate._durations["well_behaved"]
            assert len(history) == 3

    asyncio.run(run())


def test_only_one_real_call_is_made_per_call_tool_invocation():
    # The whole point of composing rather than calling each sibling
    # wrapper: verify a single call_tool() only ever triggers one real
    # tools/call round trip, not two or three.
    async def run():
        gs, stack = await _guarded_session()
        async with stack:
            await gs.list_tools()
            call_count = 0
            original = gs._session.call_tool

            async def counting_call_tool(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                return await original(*args, **kwargs)

            gs._session.call_tool = counting_call_tool
            await gs.call_tool("well_behaved", {"city": "Paris"})
            assert call_count == 1

    asyncio.run(run())
