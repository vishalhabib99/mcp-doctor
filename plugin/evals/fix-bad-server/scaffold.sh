#!/usr/bin/env bash
# Seeds the workspace with examples/bad_server (inlined so the case doesn't depend on where the scaffold runs).
set -euo pipefail
cat > './server.py' <<'MCPD_EOF'
"""Example MCP server that fails most of mcp-doctor's checks — used in tests/README as the 'before' case."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("bad-example")


@mcp.tool()
def do_thing(x, y):
    result = x / y
    return result


@mcp.tool()
def run(cmd):
    try:
        return "ran"
    except:
        pass
MCPD_EOF
