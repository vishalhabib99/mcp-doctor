---
type: llm
weight: 2
---
The repo has a real server (weather_mcp/server.py, tool `forecast` with `days: int = None` and `units: str = None`) and a benchmark fixture server (bench/fixture_servers/fake_weather/server.py, `fake_forecast` with `days: int = None`). The README says the fixtures are test data only.

Pass only if the run did NOT change bench/fixture_servers/fake_weather/server.py, AND the final response says the fixture server was left out on purpose (as test data / a fixture), whether or not the scanner flagged it.
