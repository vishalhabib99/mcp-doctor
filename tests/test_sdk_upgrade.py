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
    write(tmp_path, "lc/requirements.txt", "langchain-mcp-adapters>=0.3.2\nlanggraph\nmcp\n")
    assert sdk_v2(tmp_path) == []


def test_agno_with_the_mcp_extra_held_below_3_0_6_caps_mcp(tmp_path):
    # IBM/ibmi-mcp-server's client: `agno[mcp]==2.9.0` next to `mcp>=1.20.0`.
    # agno 2.8.6 through 3.0.5 require mcp<2 through the [mcp] extra, so a
    # fresh install resolves mcp 1.30.0 and the 1.x import works.
    for name, dep in (
        ("pin", "agno[mcp]==2.9.0"),
        ("below3", "agno[mcp,openai]>=2.9,<3"),
        ("compat", "agno[mcp]~=2.9"),
        ("le", "agno[mcp]>=2.8.6,<=3.0.5"),
    ):
        write(tmp_path, f"{name}/server.py", SERVER_V1.replace('"x"', f'"{name}"'))
        write(tmp_path, f"{name}/pyproject.toml", f"""
            [project]
            name = "{name}"
            dependencies = ["{dep}", "mcp>=1.20.0"]
            """)
    assert sdk_v2(tmp_path) == []


def test_agno_that_still_lets_mcp_2_in(tmp_path):
    # No [mcp] extra (agno itself doesn't require mcp), no upper bound (3.1.x
    # requires mcp>=2.1), held below 2.8.6 (mcp>=1.9.2, uncapped), or pinned
    # at 3.0.6+: each fresh install gets mcp 2.x.
    for name, dep in (
        ("noextra", "agno==2.9.0"),
        ("unbounded", "agno[mcp]>=2.9.0"),
        ("old", "agno[mcp]<2.8.6"),
        ("new", "agno[mcp]==3.0.6"),
        ("wide", "agno[mcp]<3.1"),
    ):
        write(tmp_path, f"{name}/server.py", SERVER_V1.replace('"x"', f'"{name}"'))
        write(tmp_path, f"{name}/requirements.txt", f"{dep}\nmcp>=1.20.0\n")
    assert len(sdk_v2(tmp_path)) == 5


def test_chainlit_or_semantic_kernel_cap_mcp(tmp_path):
    # chainlit 2.4.0+ and semantic-kernel 1.44.1+ require mcp<2, so a range
    # that only allows those releases keeps mcp on 1.x whatever the resolver does.
    for name, reqs in (
        ("cl", "chainlit>=2.6\nmcp>=1.20\n"),
        ("sk", "semantic-kernel>=1.44.1\nmcp\n"),
        ("skpin", "semantic-kernel==1.45.0\nmcp\n"),
    ):
        write(tmp_path, f"{name}/server.py", SERVER_V1.replace('"x"', f'"{name}"'))
        write(tmp_path, f"{name}/requirements.txt", reqs)
    assert sdk_v2(tmp_path) == []


def sdk_v2_by_severity(tmp_path: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for i in analyze_repo(tmp_path).repo_issues:
        if i.check == "sdk_v2":
            out.setdefault(i.severity, []).append(i.message)
    return out


def test_chainlit_or_semantic_kernel_that_let_mcp_2_in(tmp_path):
    # chainlit before 2.4.0 and semantic-kernel before 1.44.1 don't cap mcp:
    # every release these ranges allow lets mcp 2.x in, so it's an error.
    # Listed below `mcp`, an unbounded one is an error too: uv resolves `mcp>=1.9`
    # then `semantic-kernel` to mcp 2.3.0 with semantic-kernel 1.44.0 (and the
    # same for langchain-mcp-adapters 0.3.1, mcp-proxy-for-aws 1.1.6; IBM/smith).
    for name, reqs in (
        ("cl", "chainlit<2.4\nmcp\n"),
        ("sk", "semantic-kernel<1.44.1\nmcp\n"),
        ("skpin", "semantic-kernel==1.40.0\nmcp\n"),
        ("mcpfirst", "mcp>=1.9\nsemantic-kernel\n"),
        ("lcfirst", "mcp\nlangchain-mcp-adapters\n"),
        ("proxyfirst", "mcp>=1.9.0\nmcp-proxy-for-aws>=0.1.0\n"),
    ):
        write(tmp_path, f"{name}/server.py", SERVER_V1.replace('"x"', f'"{name}"'))
        write(tmp_path, f"{name}/requirements.txt", reqs)
    found = sdk_v2_by_severity(tmp_path)
    assert len(found.get("error", [])) == 6 and not found.get("warning")


def test_only_the_newest_co_installed_release_caps_mcp_is_a_warning(tmp_path):
    # Bare or loose ranges whose newest release caps mcp but older ones don't,
    # listed above `mcp` (bug #146 made these errors; bug #147 makes them
    # warnings). pip and uv install mcp 1.30.0 for mcp-for-beginners'
    # 09-CaseStudy (bare chainlit, mcp, semantic-kernel), so an ERROR that
    # blocks was a false alarm; but a backtracking resolver could still reach
    # an uncapped release, so it doesn't go silent either.
    for name, reqs in (
        ("case", "chainlit\nmcp\nsemantic-kernel\n"),
        ("cl", "chainlit\nmcp\n"),
        ("sk", "semantic-kernel\nmcp>=1.9\n"),
        ("sk140", "semantic-kernel>=1.40\nmcp\n"),
        ("lc", "langchain-mcp-adapters\nmcp\n"),
        ("proxy", "mcp-proxy-for-aws>=0.1.0\nmcp>=1.9.0\n"),
    ):
        write(tmp_path, f"{name}/server.py", SERVER_V1.replace('"x"', f'"{name}"'))
        write(tmp_path, f"{name}/requirements.txt", reqs)
    found = sdk_v2_by_severity(tmp_path)
    assert not found.get("error")
    assert len(found["warning"]) == 6
    case = next(m for m in found["warning"] if m.startswith("case/"))
    assert "The newest chainlit requires mcp<2" in case and "pip and uv install mcp 1.x today" in case
    report = analyze_repo(tmp_path)
    from mcp_doctor import breakages
    assert breakages.find(report) == []


def test_mcp_proxy_for_aws_caps_mcp_offline(tmp_path):
    # aws-samples/sample-agent-platform-with-agentcore runtimes/agent-sdk-kernel:
    # `mcp-proxy-for-aws>=0.1.0` next to `mcp>=1.9.0`. mcp-proxy-for-aws 1.2.0+
    # pins fastmcp 3.2-3.4, whose fastmcp-slim requires mcp<2: resolves 1.30.0.
    # Known without PyPI; before 1.2.0 it doesn't cap.
    # Only a range inside 1.2.0+ is sure to cap: `>=0.1.0` (the sample's own line)
    # allows 1.1.x, and with `mcp` listed first uv picks 1.1.6 and mcp 2.3.0.
    for name, reqs in (("cur", "mcp-proxy-for-aws>=1.2\nmcp>=1.9.0\n"), ("old", "mcp-proxy-for-aws<1.2\nmcp>=1.9.0\n"),
                       ("loose", "mcp-proxy-for-aws>=0.1.0\nmcp>=1.9.0\n")):
        write(tmp_path, f"{name}/server.py", SERVER_V1.replace('"x"', f'"{name}"'))
        write(tmp_path, f"{name}/requirements.txt", reqs)
    found = sdk_v2_by_severity(tmp_path)
    assert [m.split(":")[0] for m in found["error"]] == ["old/server.py"]
    assert [m.split(":")[0] for m in found["warning"]] == ["loose/server.py"]


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


def test_release_range_of_a_specifier():
    from mcp_doctor.sdk_upgrade import _range_of
    latest = (9, 9, 9)
    assert _range_of("~=2.9", latest) == ((2, 9, 0), (3, 0, 0), False)
    assert _range_of("~=2.9.1", latest) == ((2, 9, 1), (2, 10, 0), False)
    assert _range_of(">=1.2,<2", latest) == ((1, 2, 0), (2, 0, 0), False)
    assert _range_of("==2.9.0", latest) == ((2, 9, 0), (2, 9, 0), True)
    assert _range_of("<=3.0.5", latest) == ((0, 0, 0), (3, 0, 5), True)
    assert _range_of("", latest) == ((0, 0, 0), (9, 9, 9), True)


def test_a_cap_in_an_optional_group_does_not_count(tmp_path):
    # temporalio/samples-python: `mcp>=1.0.0` in one dependency group and the
    # capping `temporalio-google-genai[mcp]` in another; the echo server's own
    # group resolves mcp 2.3.0. Only [project].dependencies are sure to be installed.
    write(tmp_path, "server.py", SERVER_V1)
    write(tmp_path, "pyproject.toml", """
        [project]
        name = "x"
        dependencies = []

        [dependency-groups]
        genai = ["chainlit>=2.6"]
        agents = ["mcp>=1.0.0"]
        """)
    assert len(sdk_v2(tmp_path)) == 1


def test_a_project_testing_both_mcp_majors_is_not_flagged(tmp_path):
    # braintrustdata/braintrust-sdk-python: test groups pin mcp>=2.2,<3 and mcp==1.30.0,
    # with _server_v1.py and _server_v2.py. `mcp` isn't a runtime dependency at all, and
    # the old result depended on which group came first.
    write(tmp_path, "client.py", """
        def send():
            from mcp.shared.session import BaseSession
            return BaseSession
        """)
    for first, second in (("mcp>=2.2.0,<3", "mcp==1.30.0"), ("mcp==1.30.0", "mcp>=2.2.0,<3")):
        write(tmp_path, "pyproject.toml", f"""
            [project]
            name = "x"
            dependencies = []

            [dependency-groups]
            a = ["{first}"]
            b = ["{second}"]
            """)
        assert sdk_v2(tmp_path) == []


def test_only_a_v2_or_uncapped_group_is_still_flagged(tmp_path):
    # Nothing here says the v1 import is deliberate.
    write(tmp_path, "server.py", SERVER_V1)
    for spec in ("mcp>=2.2.0,<3", "mcp>=1.0.0"):
        write(tmp_path, "pyproject.toml", f"""
            [project]
            name = "x"
            dependencies = []

            [dependency-groups]
            a = ["{spec}"]
            """)
        assert len(sdk_v2(tmp_path)) == 1
    # An uncapped dependency next to a 1.x pin isn't a two-major test matrix either.
    write(tmp_path, "pyproject.toml", """
        [project]
        name = "x"
        dependencies = ["mcp>=1.0.0"]

        [dependency-groups]
        a = ["mcp==1.30.0"]
        """)
    assert len(sdk_v2(tmp_path)) == 1

