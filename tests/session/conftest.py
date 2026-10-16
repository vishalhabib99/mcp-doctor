import importlib.util

# The runtime checks need the MCP SDK, an optional extra: pip install "mcp-server-lint[runtime]".
if importlib.util.find_spec("mcp") is None:
    collect_ignore_glob = ["test_*.py"]
