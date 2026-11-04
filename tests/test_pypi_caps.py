"""The PyPI lookup behind sdk_v2's co-installed-cap check, on fake PyPI data."""
from pathlib import Path
from textwrap import dedent

import pytest

from mcp_doctor import pypi_caps
from mcp_doctor.analyzer import analyze_repo

SERVER_V1 = """
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("x")

    @mcp.tool()
    def ping() -> str:
        \"\"\"Check that the server is alive.\"\"\"
        return "pong"
    """

# name -> {version: requires_dist}
FAKE = {
    "proxy": {"1.0.0": ["outer-mcp>=1"], "2.0.0": ["outer-mcp<3,>=2", "boto3"]},
    "outer-mcp": {"1.0.0": ["mcp>=1.9"], "2.5.0": ["outer-mcp-slim[client]==2.5.0"], "3.1.0": ["mcp>=2.1"]},
    "outer-mcp-slim": {"2.5.0": ['mcp<2.0,>=1.24; extra == "client"', "pydantic"]},
    "direct": {"1.0.0": ["mcp<2"], "1.1.0": ["mcp<2"]},
    "extra-only": {"1.0.0": ['mcp<2; extra == "mcp"']},
    "loose": {"1.0.0": ["mcp>=1.2"]},
    "boto3": {"1.0.0": []},
    "pre-capped": {"0.9.0": ["mcp>=1.2"], "1.0.0a260709": ["mcp<2,>=1.24.0"]},
    "pre-loose": {"1.0.0b1": ["mcp>=1.2"]},
}


@pytest.fixture
def online(monkeypatch):
    monkeypatch.setenv("MCP_DOCTOR_OFFLINE", "0")
    monkeypatch.setattr(pypi_caps, "_cache", {})
    calls = []

    def fake_get(url):
        calls.append(url)
        parts = url.split("/pypi/", 1)[1].strip("/").split("/")
        name = parts[0]
        if name not in FAKE:
            return None
        if parts[-1] == "json" and len(parts) == 2:
            return {"releases": {v: [{"yanked": False}] for v in FAKE[name]}}
        return {"info": {"requires_dist": FAKE[name].get(parts[1])}}

    monkeypatch.setattr(pypi_caps, "_get", fake_get)
    return calls


def test_satisfies():
    s = pypi_caps.satisfies
    assert s("2.9.0", "==2.9.0") and not s("2.9.1", "==2.9.0")
    assert s("3.4.11", ">=3.2,<3.5") and not s("3.5.0", ">=3.2,<3.5")
    assert s("2.9.9", "~=2.9") and s("2.99", "~=2.9") and not s("3.0", "~=2.9")
    assert s("1.4.9", "~=1.4.5") and not s("1.5.0", "~=1.4.5")
    assert s("1.2.3", "==1.2.*") and not s("1.3.0", "==1.2.*") and not s("1.2.0", "!=1.2.*")
    assert s("5.0", "") and not s("1.0", ">1.0")


def test_chain_through_mcp_named_packages(online):
    assert pypi_caps.caps_mcp("proxy", ">=2", set()) == (
        "proxy 2.0.0 -> outer-mcp 2.5.0 -> outer-mcp-slim 2.5.0 requires mcp<2.0,>=1.24")
    # boto3 has no "mcp" in its name, so it is never fetched.
    assert not any("/boto3/" in u for u in online)


def test_every_allowed_release_must_cap(online):
    # A resolver that takes the newest mcp first backtracks to an older release,
    # so one allowed release without the cap is enough to let mcp 2.x in.
    assert pypi_caps.caps_mcp("proxy", "", set()) is None         # proxy 1.0.0 -> outer-mcp 1.0.0 (mcp>=1.9)
    assert pypi_caps.caps_mcp("outer-mcp", "", set()) is None     # 1.0.0 and 3.1.0 don't cap
    assert pypi_caps.caps_mcp("outer-mcp", ">=2,<3", set())       # only 2.5.0: caps through slim[client]
    assert pypi_caps.caps_mcp("direct", "", set()) == "direct 1.0.0..1.1.0 requires mcp<2"


def test_budget_gives_up_as_unknown(online, monkeypatch):
    monkeypatch.setattr(pypi_caps, "_MAX_RELEASES", 1)
    assert pypi_caps.caps_mcp("direct", "", set()) is None


def test_extras_and_loose_requirements(online):
    assert pypi_caps.caps_mcp("extra-only", "", set()) is None
    assert pypi_caps.caps_mcp("extra-only", "", {"mcp"})
    assert pypi_caps.caps_mcp("loose", "", set()) is None
    assert pypi_caps.caps_mcp("not-on-pypi", "", set()) is None


def test_offline_or_failing_lookup_keeps_the_finding(monkeypatch):
    monkeypatch.setenv("MCP_DOCTOR_OFFLINE", "1")
    assert pypi_caps.first_capping([("direct", "", set())]) is None
    monkeypatch.setenv("MCP_DOCTOR_OFFLINE", "0")
    monkeypatch.setattr(pypi_caps, "_cache", {})
    monkeypatch.setattr(pypi_caps, "_get", lambda url: None)
    assert pypi_caps.first_capping([("direct", "", set())]) is None


def _write(tmp_path: Path, name: str, content: str) -> None:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dedent(content))


def test_sdk_v2_uses_the_lookup_when_online(tmp_path, online):
    for name, reqs in (("capped", "direct\nmcp>=1.9\n"), ("open", "loose\nhttpx\nmcp>=1.9\n")):
        _write(tmp_path, f"{name}/server.py", SERVER_V1.replace('"x"', f'"{name}"'))
        _write(tmp_path, f"{name}/requirements.txt", reqs)
    flagged = [i.message for i in analyze_repo(tmp_path).repo_issues if i.check == "sdk_v2"]
    assert len(flagged) == 1 and "open/server.py" in flagged[0]


def test_only_the_newest_release_capping_is_a_warning(tmp_path, online):
    # "outer-mcp" unbounded: 3.1.0 (the newest) needs mcp>=2.1, so not even partial.
    # "proxy" unbounded: 2.0.0 (the newest) caps through outer-mcp 2.5.0, but 1.0.0
    # doesn't, so pip/uv pick the capped one today and a backtracking resolver might not.
    assert pypi_caps.first_capping([("proxy", "", set())]) == (
        "proxy 2.0.0 -> outer-mcp 2.5.0 -> outer-mcp-slim 2.5.0 requires mcp<2.0,>=1.24", False)
    assert pypi_caps.first_capping([("outer-mcp", "", set())]) is None
    assert pypi_caps.first_capping([("proxy", "", set()), ("direct", "", set())])[1] is True
    for name, reqs in (("above", "proxy\nmcp>=1.9\n"), ("below", "mcp>=1.9\nproxy\n")):
        _write(tmp_path, f"{name}/server.py", SERVER_V1.replace('"x"', f'"{name}"'))
        _write(tmp_path, f"{name}/requirements.txt", reqs)
    found = {i.message.split("/")[0]: i for i in analyze_repo(tmp_path).repo_issues if i.check == "sdk_v2"}
    assert found["above"].severity == "warning" and "The newest proxy requires mcp<2" in found["above"].message
    assert found["below"].severity == "error"  # with mcp first, uv takes mcp 2.x


def test_an_exact_pin_to_a_prerelease_is_looked_up(tmp_path, online):
    # microsoft/frontiertour27-BRK240: agent-framework-foundry-hosting==1.0.0a260709
    # requires mcp<2 and uv resolves mcp 1.30.0, but only final releases were read,
    # so the lookup came back "unknown". pip and uv both take an explicitly pinned
    # prerelease, and it's the only release this requirement allows.
    assert pypi_caps.caps_mcp("pre-capped", "==1.0.0a260709", set()) == (
        "pre-capped 1.0.0a260709 requires mcp<2,>=1.24.0"
    )
    assert pypi_caps.caps_mcp("pre-loose", "==1.0.0b1", set()) is None
    # Only an exact pin: a range still reads final releases, as before.
    assert pypi_caps.caps_mcp("pre-capped", ">=0.9", set()) is None
    assert pypi_caps.caps_mcp("pre-capped", "==2.0.0a1", set()) is None


def test_a_manifest_pinning_a_capped_prerelease_is_not_flagged(tmp_path, online):
    (tmp_path / "server.py").write_text(dedent(SERVER_V1))
    (tmp_path / "requirements.txt").write_text("pre-capped==1.0.0a260709\nmcp>=1.0.0")
    assert not [i for i in analyze_repo(tmp_path).repo_issues if i.check == "sdk_v2"]
    (tmp_path / "requirements.txt").write_text("pre-loose==1.0.0b1\nmcp>=1.0.0")
    pypi_caps._cache.clear()
    assert [i for i in analyze_repo(tmp_path).repo_issues if i.check == "sdk_v2"]
