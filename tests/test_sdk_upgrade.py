from pathlib import Path
from textwrap import dedent

from mcp_doctor.analyzer import analyze_repo
from mcp_doctor.verdict import decide

SERVER_V1 = """
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("x")

    @mcp.tool()
    def ping() -> str:
        \"\"\"Check that the server is alive.\"\"\"
        return "pong"
    """


def write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dedent(content))
    return p


def sdk_v2(tmp_path: Path) -> list[str]:
    return [i.message for i in analyze_repo(tmp_path).repo_issues if i.check == "sdk_v2"]


def test_uncapped_mcp_with_fastmcp_import_crashes_on_fresh_install(tmp_path):
    # The 8 newest reports (2026-10-13..18, e.g. jpmorgan-payments/ai#32,
    # hballington12/mcp-oignon#1): `mcp>=1.x` resolves to 2.x, which removed
    # mcp.server.fastmcp, and the server dies with ModuleNotFoundError.
    write(tmp_path, "server.py", SERVER_V1)
    write(tmp_path, "pyproject.toml", """
        [project]
        name = "x"
        keywords = ["mcp"]
        dependencies = ["mcp[cli]>=1.6.0", "httpx"]
        """)
    [msg] = sdk_v2(tmp_path)
    assert msg.startswith("server.py:2 `from mcp.server.fastmcp import ...` was removed in mcp 2.x")
    assert "'mcp>=1.6.0'" in msg and "Pin 'mcp<2'" in msg and "MCPServer" in msg


def test_capped_or_v2_servers_are_not_flagged(tmp_path):
    write(tmp_path, "a/server.py", SERVER_V1)
    write(tmp_path, "a/pyproject.toml", '[project]\nname = "a"\ndependencies = ["mcp>=1.6,<2"]\n')
    write(tmp_path, "b/server.py", SERVER_V1)
    write(tmp_path, "b/pyproject.toml", '[tool.poetry.dependencies]\npython = "^3.10"\nmcp = {version = "^1.9", extras = ["cli"]}\n')
    write(tmp_path, "c/server.py", SERVER_V1)
    write(tmp_path, "c/requirements.txt", "mcp==1.9.4\n")
    write(tmp_path, "d/server.py", SERVER_V1.replace("from mcp.server.fastmcp import FastMCP", "from mcp.server.mcpserver import MCPServer as FastMCP"))
    write(tmp_path, "d/requirements.txt", "mcp>=2\n")
    assert sdk_v2(tmp_path) == []


def test_each_package_is_judged_by_its_nearest_manifest(tmp_path):
    write(tmp_path, "pyproject.toml", '[project]\nname = "root"\ndependencies = ["mcp<2"]\n')
    write(tmp_path, "servers/git/server.py", SERVER_V1)
    write(tmp_path, "servers/git/requirements.txt", "mcp\n")
    write(tmp_path, "servers/time/server.py", SERVER_V1)
    [msg] = sdk_v2(tmp_path)
    assert msg.startswith("servers/git/server.py:2") and "servers/git/requirements.txt asks for 'mcp'" in msg


def test_setup_py_reads_install_requires_not_keywords(tmp_path):
    write(tmp_path, "server.py", SERVER_V1)
    write(tmp_path, "setup.py", """
        from setuptools import setup
        setup(name="x", keywords=["mcp"], install_requires=["mcp>=1.2.0"])
        """)
    assert len(sdk_v2(tmp_path)) == 1
    write(tmp_path, "setup.py", 'from setuptools import setup\nsetup(name="x", keywords=["mcp"], install_requires=["fastmcp"])\n')
    assert sdk_v2(tmp_path) == []  # mcp not declared: which version installs is unknown


def test_other_removed_imports_and_guarded_shims(tmp_path):
    write(tmp_path, "client.py", """
        from mcp.client.streamable_http import streamablehttp_client
        from mcp.shared.exceptions import McpError
        """)
    write(tmp_path, "requirements.txt", "mcp>=1.10\n")
    [msg] = sdk_v2(tmp_path)
    assert "`from mcp.client.streamable_http import streamablehttp_client`" in msg
    # A try/except ImportError shim runs on both versions.
    write(tmp_path, "client.py", """
        try:
            from mcp.server.mcpserver import MCPServer
        except ImportError:
            from mcp.server.fastmcp import FastMCP as MCPServer
        """)
    assert sdk_v2(tmp_path) == []



def test_names_still_exported_in_v2_are_not_flagged(tmp_path):
    # JSONRPCMessage and the Client*/Server* message unions are still exported
    # by mcp 2.0.0-2.3.0 (databrickslabs/mcp's base.py was a false positive).
    write(tmp_path, "base.py", """
        from mcp.types import JSONRPCMessage, ClientRequest, ServerResult
        from mcp import ClientNotification, ServerRequest
        """)
    write(tmp_path, "pyproject.toml", '[project]\nname = "x"\ndependencies = ["mcp>=1.9.2"]\n')
    assert sdk_v2(tmp_path) == []
    write(tmp_path, "base.py", "from mcp.types import Content\n")
    assert len(sdk_v2(tmp_path)) == 1


def test_v2_only_requirement_is_not_called_unbounded(tmp_path):
    # Tencent/AI-Infra-Guard: 'mcp>=2.0.0,<3.0.0' with a fastmcp import. Still a
    # crash, but the requirement has an upper bound, so don't say it lacks one.
    write(tmp_path, "server.py", SERVER_V1)
    write(tmp_path, "pyproject.toml", '[project]\nname = "x"\ndependencies = ["mcp>=2.0.0,<3.0.0"]\n')
    [msg] = sdk_v2(tmp_path)
    assert "asks for 'mcp>=2.0.0,<3.0.0', so a fresh install" in msg and "no upper bound" not in msg
    write(tmp_path, "pyproject.toml", '[project]\nname = "x"\ndependencies = ["mcp>1.5"]\n')
    [msg] = sdk_v2(tmp_path)
    assert "no upper bound" in msg


def test_version_branch_shim_is_not_flagged(tmp_path):
    # strands-agents/harness-sdk: the v2 branch returns early, the v1 import follows it.
    write(tmp_path, "compat.py", """
        MCP_V2 = True

        def transport(url):
            if MCP_V2:
                from mcp.client.streamable_http import streamable_http_client
                return streamable_http_client(url=url)
            from mcp.client.streamable_http import streamablehttp_client
            return streamablehttp_client(url=url)
        """)
    write(tmp_path, "pyproject.toml", '[project]\nname = "x"\ndependencies = ["mcp>=1.23.0,<2.4"]\n')
    assert sdk_v2(tmp_path) == []
    # An unrelated early return doesn't make the v1 import safe.
    write(tmp_path, "compat.py", """
        def transport(url):
            if not url:
                return None
            from mcp.client.streamable_http import streamablehttp_client
            return streamablehttp_client(url=url)
        """)
    [msg] = sdk_v2(tmp_path)
    assert "asks for 'mcp>=1.23.0,<2.4', which allows mcp 2.x" in msg and "no upper bound" not in msg


def test_lockfile_pinning_v1_is_named(tmp_path):
    # derisk-ai/OpenDerisk, worldbank/data360-mcp: the lock keeps `uv sync` on 1.x, pip still breaks.
    write(tmp_path, "server.py", SERVER_V1)
    write(tmp_path, "pyproject.toml", '[project]\nname = "x"\ndependencies = ["mcp>=1.1.3"]\n')
    write(tmp_path, "uv.lock", 'version = 1\n\n[[package]]\nname = "mcp"\nversion = "1.8.1"\n')
    [msg] = sdk_v2(tmp_path)
    assert "(uv.lock pins mcp 1.8.1, so `uv sync` is unaffected; pip and uvx installs are not)" in msg
    write(tmp_path, "uv.lock", 'version = 1\n\n[[package]]\nname = "mcp"\nversion = "2.3.0"\n')
    [msg] = sdk_v2(tmp_path)
    assert "uv.lock pins" not in msg

def test_test_files_are_not_flagged(tmp_path):
    write(tmp_path, "tests/test_server.py", SERVER_V1)
    write(tmp_path, "requirements.txt", "mcp\n")
    assert sdk_v2(tmp_path) == []


def test_not_scored_but_verdict_asks_for_a_fix(tmp_path):
    write(tmp_path, "server.py", SERVER_V1)
    write(tmp_path, "requirements.txt", "mcp>=1.6\n")
    flagged = analyze_repo(tmp_path)
    write(tmp_path, "requirements.txt", "mcp>=1.6,<2\n")
    capped = analyze_repo(tmp_path)
    assert (flagged.percent, flagged.security_percent) == (capped.percent, capped.security_percent)
    doctor = {"repo_issues": [
        {"check": i.check, "severity": i.severity, "category": i.category} for i in flagged.repo_issues if i.check == "sdk_v2"
    ], "tools": []}
    d = decide(doctor, None, None)
    assert any("mcp 2.x" in f for f in d.fixes)
    assert not any("spec/documentation error" in f for f in d.fixes)


def test_pyproject_without_a_toml_parser(tmp_path, monkeypatch):
    # Python 3.9/3.10 without tomli fall back to a regex that must still skip `keywords`.
    monkeypatch.setitem(__import__("sys").modules, "tomllib", None)
    monkeypatch.setitem(__import__("sys").modules, "tomli", None)
    write(tmp_path, "server.py", SERVER_V1)
    write(tmp_path, "pyproject.toml", '[project]\nname = "x"\nkeywords = ["mcp"]\ndependencies = ["mcp<2"]\n')
    assert sdk_v2(tmp_path) == []
    write(tmp_path, "pyproject.toml", '[project]\nname = "x"\nkeywords = [\n  "mcp",\n]\ndependencies = ["mcp>=1.6"]\n')
    assert len(sdk_v2(tmp_path)) == 1
    write(tmp_path, "pyproject.toml", '[tool.poetry.dependencies]\nmcp = "^1.9"\n')
    assert sdk_v2(tmp_path) == []


def test_lowlevel_server_decorators_fail_at_start(tmp_path):
    # BeehiveInnovations/pal-mcp-server: `server = Server(...)` then
    # `@server.list_tools()`; 2.x takes handlers as `Server(on_list_tools=...)`
    # and the decorator raises AttributeError when the module loads.
    write(tmp_path, "server.py", """
        from mcp.server import Server as LowServer

        class App:
            def __init__(self):
                self.server = LowServer("x")

                @self.server.call_tool()
                async def call(name, args):
                    return []

        server: LowServer = LowServer("y")

        @server.list_tools()
        async def list_tools():
            return []
        """)
    write(tmp_path, "requirements.txt", "mcp>=1.0.0\n")
    [msg] = sdk_v2(tmp_path)
    assert msg.startswith("server.py:8 `@self.server.call_tool()` was removed in mcp 2.x")
    assert "AttributeError" in msg and "Server(on_list_tools=...)" in msg
    # maven-decoder-mcp checks for the decorators and installs its own on 2.x.
    write(tmp_path, "server.py", """
        from mcp.server import Server
        server = Server("x")
        if not hasattr(server, "list_tools"):
            server.list_tools = lambda: (lambda f: f)

        @server.list_tools()
        async def list_tools():
            return []
        """)
    assert sdk_v2(tmp_path) == []
    # The same decorator name on something that isn't the SDK's Server is fine.
    write(tmp_path, "server.py", """
        from myframework import Server
        server = Server("x")

        @server.list_tools()
        async def list_tools():
            return []
        """)
    assert sdk_v2(tmp_path) == []


def test_a_co_installed_package_that_caps_mcp_below_2(tmp_path):
    # google/adk-recipes genmedia-for-commerce: `mcp[cli]>=1.27.0` next to
    # `google-adk>=1.31.0,<2.0.0`, and every google-adk 1.x since 1.20 requires
    # mcp<2, so a fresh install resolves mcp 1.30.0. aws-samples/
    # sample-genai-on-eks-starter-kit: a bare `mcp` next to a bare
    # `langchain-mcp-adapters`, whose newest release (0.3.2) requires mcp<2.0.0.
    write(tmp_path, "adk/server.py", SERVER_V1)
    write(tmp_path, "adk/pyproject.toml", """
        [project]
        name = "adk"
        dependencies = ["google-adk>=1.31.0,<2.0.0", "mcp[cli]>=1.27.0"]
        """)
    write(tmp_path, "lc/server.py", SERVER_V1.replace('"x"', '"lc"'))
    write(tmp_path, "lc/requirements.txt", "langchain-mcp-adapters\nlanggraph\nmcp\n")
    assert sdk_v2(tmp_path) == []


def test_a_co_installed_package_that_still_allows_mcp_2(tmp_path):
    # google-adk 2.x allows mcp<3, strands-agents allows mcp<2.2 (a fresh
    # install gets 2.1.1), and langchain-mcp-adapters before 0.3.2 had no cap.
    # (Distinct server names: identical files are scanned once.)
    for name, reqs in (
        ("adk", "google-adk>=1.31.0\nmcp>=1.27.0\n"),
        ("strands", "strands-agents[otel]>=1.4.0\nmcp\n"),
        ("lc", "langchain-mcp-adapters<0.3\nmcp\n"),
    ):
        write(tmp_path, f"{name}/server.py", SERVER_V1.replace('"x"', f'"{name}"'))
        write(tmp_path, f"{name}/requirements.txt", reqs)
    assert len(sdk_v2(tmp_path)) == 3
