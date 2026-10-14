#!/usr/bin/env bash
# A cloned third-party server with the none-default bug in its real server
# AND in a benchmark fixture server; the skill must fix only the real one.
set -euo pipefail
mkdir -p weather_mcp tests bench/fixture_servers/fake_weather
cat > README.md <<'MCPD_EOF'
# weather-mcp

MCP server for weather forecasts (`weather_mcp/server.py`).

`bench/fixture_servers/` holds synthetic servers used only as test data for our
tool-selection benchmark. They are never shipped or run as real servers.
MCPD_EOF
cat > weather_mcp/__init__.py <<'MCPD_EOF'
MCPD_EOF
cat > weather_mcp/server.py <<'MCPD_EOF'
"""Weather MCP server."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("weather")


@mcp.tool()
def forecast(city: str, days: int = None, units: str = None) -> str:
    """Get the forecast for a city.

    Args:
        city: City name, e.g. "Paris".
        days: Number of days, 1-7. Defaults to 3.
        units: "metric" or "imperial". Defaults to metric.
    """
    return f"{city}: sunny for {days or 3} days ({units or 'metric'})"


if __name__ == "__main__":
    mcp.run()
MCPD_EOF
cat > bench/fixture_servers/fake_weather/server.py <<'MCPD_EOF'
"""Synthetic fixture server for the tool-selection benchmark. Test data only."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-weather")


@mcp.tool()
def fake_forecast(city: str, days: int = None) -> str:
    """Pretend forecast.

    Args:
        city: City.
        days: Days.
    """
    return "fixture"
MCPD_EOF
cat > tests/test_server.py <<'MCPD_EOF'
from weather_mcp.server import forecast


def test_forecast_defaults():
    assert forecast("Paris") == "Paris: sunny for 3 days (metric)"
MCPD_EOF
cat > pyproject.toml <<'MCPD_EOF'
[project]
name = "weather-mcp"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = ["mcp>=1.20,<2"]
MCPD_EOF
git init -q && git add -A && git -c user.name=maintainer -c user.email=m@example.com commit -qm "initial"
