"""Example MCP server that grades an A and still breaks agents: one parameter
defaults to None but is typed `str`, so the schema advertises a null default the
server then rejects. `mcp-doctor verdict` blocks it; `mcp-doctor --fix` fixes it."""

try:  # mcp 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

mcp = FastMCP("weather")


@mcp.tool()
def get_forecast(city: str, days: int, units: str = None) -> str:
    """Get a multi-day weather forecast for a city.

    Args:
        city: Name of the city to look up, e.g. "Seattle".
        days: Number of days to forecast, between 1 and 7.
        units: "metric" or "imperial"; leave it out for the city's local units.
    """
    try:
        if not (1 <= days <= 7):
            raise ValueError("days must be between 1 and 7")
        return f"{days}-day forecast for {city}: sunny throughout ({units or 'local units'})."
    except ValueError as e:
        return f"error: {e}"


if __name__ == "__main__":
    mcp.run()
