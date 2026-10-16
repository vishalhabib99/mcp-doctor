"""Connects to a real, running MCP server — over stdio (a launched local
command) or Streamable HTTP (a remote URL) — calls each tool once with
realistic-looking valid arguments, and checks whether the response is a
genuine answer — not just structurally well-formed.

mcp-doctor reads a server's source and never runs it. mcp-fuzz runs it, but
only judges the *bad*-input path (does it crash?) — it explicitly declines
to judge a *successful* call's content, since a schema-only placeholder
value usually isn't realistic enough to fairly judge correctness. This
tool is the piece that was missing: still fully deterministic, still no
LLM, no API key, no per-call cost — just closer-to-realistic input plus a
handful of reliable content checks (a disguised refusal, empty content, a
declared output schema the response doesn't actually match).

Safety: same default as mcp-fuzz — only `readOnlyHint: true` tools are
called unless `--include-destructive` is passed. This tool executes real
tool calls; it inherits that risk from the target server exactly the same
way mcp-fuzz does.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from mcp_doctor.reality.checks import (
    SanityResult,
    response_text_from_content,
    check_echo_mismatch,
    check_empty_content,
    check_output_schema,
    check_refusal_in_disguise,
)
from mcp_doctor.reality.generator import generate_realistic_arguments, string_argument_values

DEFAULT_TIMEOUT_SECONDS = 15.0


@dataclass
class RealityCheckReport:
    server_command: str
    results: list[SanityResult] = field(default_factory=list)
    connect_error: str | None = None
    # Set when a tool call's failure was bad enough that the follow-up
    # reconnect itself failed — meaning the server is genuinely gone, not
    # just that one call. Distinct from connect_error, which means the
    # *initial* connection never succeeded. See _try_reconnect.
    terminated_early: str | None = None


@dataclass
class HttpTarget:
    """A remote MCP server reached over Streamable HTTP instead of a local
    launchable stdio command — mirrors mcp-fuzz's identical type. `headers`
    carries auth (a bearer token, an API key header) the same way `--env`
    carries one into a launched stdio process."""

    url: str
    headers: dict[str, str] | None = None


# Either transport the spec allows: a local command this tool launches and
# owns the lifecycle of, or a remote endpoint it only ever connects to.
ConnectionTarget = StdioServerParameters | HttpTarget


class _ServerConnection:
    def __init__(self, target: ConnectionTarget):
        self._target = target
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None
        # See mcp-fuzz's identical field: set once a reconnect-after-failure
        # attempt itself fails, meaning the server is genuinely gone — the
        # expected outcome the moment a tool kills the process behind an
        # HttpTarget, since this tool has no way to relaunch a remote
        # service it doesn't own (unlike a stdio subprocess, which usually
        # can be relaunched).
        self.unreachable = False

    async def connect(self) -> None:
        await self.close()
        stack = AsyncExitStack()
        try:
            if isinstance(self._target, HttpTarget):
                http_client = create_mcp_http_client(headers=self._target.headers)
                read, write = await stack.enter_async_context(
                    streamable_http_client(self._target.url, http_client=http_client)
                )
            else:
                read, write = await stack.enter_async_context(stdio_client(self._target))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        self.session = session
        self.unreachable = False

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:
                pass
        self._stack = None
        self.session = None


async def _try_reconnect(conn: _ServerConnection) -> None:
    """Reconnect after a failed call, tolerant of the reconnect itself
    failing — see mcp-fuzz's identical helper for the full rationale (an
    unrecoverable reconnect used to propagate straight out of
    run_reality_check, crashing the whole run instead of being reported)."""
    try:
        await conn.connect()
    except Exception:
        conn.unreachable = True


def _field(model, snake_name: str, camel_name: str):
    """Same cross-`mcp`-version compat as mcp-fuzz's identical helper —
    mcp<2.0 exposes several `types` fields under their raw camelCase wire
    name; mcp>=2.0 renamed them to snake_case with camelCase kept only as a
    validation alias, not a readable attribute."""
    if hasattr(model, snake_name):
        return getattr(model, snake_name)
    return getattr(model, camel_name)


def _is_read_only(tool: types.Tool) -> bool:
    annotations = tool.annotations
    if annotations is None:
        return False
    return _field(annotations, "read_only_hint", "readOnlyHint") is True


def _merged_env(env: dict[str, str] | None) -> dict[str, str] | None:
    """`StdioServerParameters(env=None)` doesn't inherit the operator's shell
    — the SDK's own `stdio_client` deliberately falls back to a minimal safe
    allowlist (PATH, HOME, ...), never arbitrary app-specific vars, as a real
    security default against leaking secrets into a launched server. Mirrors
    mcp-fuzz's identical helper exactly: a caller that *does* pass `env`
    almost always means "also set this one API key", not "replace PATH/HOME
    entirely" — merge onto the same safe baseline the SDK already uses when
    `env` is left unset, rather than replacing it. Found missing here via
    real dogfooding, not speculatively: mcp-reality-check had the engine-level
    `env` parameter all along but no CLI flag to ever populate it, so it
    silently couldn't test any server that needs a var to even start (e.g.
    `sooperset/mcp-atlassian`, which registers zero tools without Jira/
    Confluence config) — a real capability gap mcp-fuzz already closed for
    itself but this sibling tool never got."""
    return {**get_default_environment(), **env} if env else env


async def run_reality_check(
    command: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    url: str | None = None,
    headers: dict[str, str] | None = None,
    include_destructive: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> RealityCheckReport:
    # Exactly one transport: a local command to launch, or a remote URL to
    # connect to — mirrors mcp-fuzz's identical validation.
    if (command is None) == (url is None):
        raise ValueError("exactly one of `command` or `url` must be given")

    params: ConnectionTarget
    if url is not None:
        params = HttpTarget(url=url, headers=headers)
        server_label = url
    else:
        merged_env = _merged_env(env)
        params = StdioServerParameters(command=command, args=args or [], env=merged_env, cwd=cwd)
        server_label = " ".join([command, *(args or [])])
    report = RealityCheckReport(server_command=server_label)

    conn = _ServerConnection(params)
    try:
        await conn.connect()
    except Exception as exc:
        report.connect_error = f"{type(exc).__name__}: {exc}"
        return report

    try:
        assert conn.session is not None
        tools_result = await conn.session.list_tools()
    except Exception as exc:
        report.connect_error = f"failed to list tools: {type(exc).__name__}: {exc}"
        await conn.close()
        return report

    for tool in tools_result.tools:
        if conn.unreachable:
            report.results.append(SanityResult(
                tool_name=tool.name,
                tested=False,
                skip_reason=f"server became unreachable mid-run ({report.terminated_early})",
            ))
            continue

        if not include_destructive and not _is_read_only(tool):
            report.results.append(SanityResult(
                tool_name=tool.name,
                tested=False,
                skip_reason="not annotated readOnlyHint=true (use include_destructive to test anyway)",
            ))
            continue

        schema = _field(tool, "input_schema", "inputSchema")
        arguments = generate_realistic_arguments(schema)

        try:
            assert conn.session is not None
            result = await asyncio.wait_for(
                conn.session.call_tool(tool.name, arguments), timeout=timeout
            )
        except Exception as exc:
            # A crash/timeout/protocol error on the "realistic valid" call
            # is a real signal, but classifying *why* it failed is
            # mcp-fuzz's job, not this tool's — that's the whole reason the
            # two exist separately. Record it plainly and move on rather
            # than duplicating mcp-fuzz's crash-vs-graceful-error logic
            # here; a tool that can't even complete a plausible call has
            # nothing for the content checks below to examine anyway.
            await _try_reconnect(conn)
            if conn.unreachable and report.terminated_early is None:
                report.terminated_early = (
                    f"tool {tool.name!r} failed and the reconnect itself failed: "
                    f"{type(exc).__name__}: {exc}"
                )
            report.results.append(SanityResult(
                tool_name=tool.name,
                tested=True,
                skip_reason=f"call failed before a response was available ({type(exc).__name__}: {exc}) — see mcp-fuzz for crash/timeout classification",
            ))
            continue

        is_error = bool(_field(result, "is_error", "isError")) if isinstance(result, types.CallToolResult) else False
        response_text = response_text_from_content(result.content) if hasattr(result, "content") else ""

        sanity = SanityResult(tool_name=tool.name, tested=True, response_text=response_text)

        if not is_error:
            sanity.refusal_in_disguise = check_refusal_in_disguise(response_text)
            sanity.empty_content = check_empty_content(response_text)
            input_strings = string_argument_values(arguments)
            sanity.echo_mismatch_inputs = check_echo_mismatch(response_text, input_strings)

            output_schema = _field(tool, "output_schema", "outputSchema")
            structured = _field(result, "structured_content", "structuredContent")
            sanity.schema_violation = check_output_schema(structured, output_schema)
        else:
            # A call that the server itself flags as an error has nothing
            # to sanity-check content-wise — it's already telling the
            # truth about not succeeding, which is the opposite of a
            # disguised failure.
            sanity.skip_reason = "call returned isError=true (not a disguised failure — already honest)"

        report.results.append(sanity)

    await conn.close()
    return report
