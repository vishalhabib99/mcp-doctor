import pytest


@pytest.fixture(autouse=True)
def _no_pypi_lookups(monkeypatch):
    # The sdk_v2 check can ask PyPI whether a co-installed package caps mcp.
    # Tests stay offline and deterministic; tests of that lookup opt back in.
    monkeypatch.setenv("MCP_DOCTOR_OFFLINE", "1")
