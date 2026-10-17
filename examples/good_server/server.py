"""Example MCP server that passes mcp-doctor's checks."""

try:  # mcp 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

mcp = FastMCP("weather")


@mcp.tool()
def get_forecast(city: str, days: int) -> str:
    """Get a multi-day weather forecast for a city.

    Args:
        city: Name of the city to look up, e.g. "Seattle".
        days: Number of days to forecast, between 1 and 7.
    """
    try:
        if not (1 <= days <= 7):
            raise ValueError("days must be between 1 and 7")
        return f"{days}-day forecast for {city}: sunny throughout."
    except ValueError as e:
        return f"error: {e}"


if __name__ == "__main__":
    mcp.run()
