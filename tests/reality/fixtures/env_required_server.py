"""A minimal MCP server that refuses to start without a specific environment
variable — mimics the real failure mode of `sooperset/mcp-atlassian` (needs
JIRA_*/CONFLUENCE_* config to register any tools at all) and
`brave/brave-search-mcp-server` (requires BRAVE_API_KEY), used to verify
`run_reality_check`'s `env` handling without depending on a real external
API key."""

from __future__ import annotations

import os
import sys

if os.environ.get("REQUIRED_TEST_KEY") != "expected-value":
    print("Error: REQUIRED_TEST_KEY is required", file=sys.stderr)
    sys.exit(1)

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("env-required-fixture")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def ping() -> str:
    """Returns pong."""
    return "pong"


if __name__ == "__main__":
    server.run(transport="stdio")
