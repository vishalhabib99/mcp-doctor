"""Example MCP server that fails most of mcp-doctor's checks — used in tests/README as the 'before' case."""

try:  # mcp 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
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
