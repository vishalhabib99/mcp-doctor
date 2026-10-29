# weather-mcp, one bug (example)

The same server as `good_server`, plus an optional `units` parameter typed
`str = None`. It grades an A, and it still breaks agents that send the null
default the schema advertises: `mcp-doctor verdict` blocks it, and
`mcp-doctor --fix` types it `str | None`.

## Tools

- `get_forecast(city, days, units)` — get a multi-day weather forecast for a city.
