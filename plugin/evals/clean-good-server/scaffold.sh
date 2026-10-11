#!/usr/bin/env bash
# Seeds the workspace with examples/good_server (inlined so the case doesn't depend on where the scaffold runs).
set -euo pipefail
cat > './LICENSE' <<'MCPD_EOF'
MIT License

Copyright (c) 2026

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files, to deal in the Software
without restriction, including without limitation the rights to use, copy,
modify, merge, publish, distribute, sublicense, and/or sell copies of the
Software.
MCPD_EOF
cat > './README.md' <<'MCPD_EOF'
# weather-mcp (example)

An example MCP server exposing one tool: `get_forecast`.

## Tools

- `get_forecast(city, days)` — get a multi-day weather forecast for a city.
MCPD_EOF
cat > './requirements.txt' <<'MCPD_EOF'
mcp>=1.0.0
MCPD_EOF
cat > './server.py' <<'MCPD_EOF'
"""Example MCP server that passes mcp-doctor's checks."""

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
MCPD_EOF
mkdir -p './tests'
cat > './tests/test_server.py' <<'MCPD_EOF'
from server import get_forecast


def test_get_forecast_valid_days():
    assert "3-day forecast" in get_forecast("Seattle", 3)


def test_get_forecast_invalid_days_returns_error_string():
    assert "error" in get_forecast("Seattle", 30)
MCPD_EOF
