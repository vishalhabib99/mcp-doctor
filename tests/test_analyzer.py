import subprocess
import sys
from pathlib import Path
from textwrap import dedent

from mcp_doctor.analyzer import analyze_repo

REPO_ROOT = Path(__file__).resolve().parent.parent


def write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(dedent(content))
    return p


def test_fastmcp_tool_with_full_docs_passes_clean(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def get_forecast(city: str, days: int) -> str:
            \"\"\"Get a weather forecast.

            Args:
                city: The city name.
                days: How many days out.
            \"\"\"
            try:
                return f"{city} {days}"
            except ValueError as e:
                return str(e)
        """)
    (tmp_path / "README.md").write_text("# x\n\nHas get_forecast tool.")
    (tmp_path / "LICENSE").write_text("MIT")
    (tmp_path / "requirements.txt").write_text("mcp\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass")

    report = analyze_repo(tmp_path)
    assert len(report.tools) == 1
    tool = report.tools[0]
    assert tool.issues == []
    assert report.percent == 100


def test_undocumented_untyped_tool_is_flagged(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def do_thing(x, y):
            return x / y
        """)
    report = analyze_repo(tmp_path)
    assert len(report.tools) == 1
    tool = report.tools[0]
    checks = {i.check for i in tool.issues}
    assert "description" in checks
    assert "types" in checks
    assert "error_handling" in checks


def test_bare_except_is_an_error(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def run(cmd: str) -> str:
            \"\"\"Run a command.\"\"\"
            try:
                return cmd
            except:
                pass
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    bare_issue = next(i for i in tool.issues if i.check == "bare_except")
    assert bare_issue.severity == "error"


def test_delegation_to_handled_helper_same_file_clears_error_handling(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        def do_work(x):
            try:
                return 1 / x
            except ZeroDivisionError:
                return 0

        @mcp.tool()
        def divide(x: int) -> int:
            \"\"\"Divide.

            Args:
                x: The divisor.
            \"\"\"
            return do_work(x)
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    checks = {i.check for i in tool.issues}
    assert "error_handling" not in checks


def test_delegation_to_handled_helper_multi_hop_cross_file_clears_error_handling(tmp_path):
    # Mirrors the real pattern found dogfooding against tradingview-mcp:
    # tool -> service function -> fetch function -> the function with the
    # actual try/except, three calls deep and across two files.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        from service import analyze_sentiment
        mcp = FastMCP("x")

        @mcp.tool()
        def market_sentiment(symbol: str) -> dict:
            \"\"\"Sentiment for a symbol.

            Args:
                symbol: The ticker.
            \"\"\"
            return analyze_sentiment(symbol)
        """)
    write(tmp_path, "service.py", """
        def analyze_sentiment(symbol):
            articles = _get_articles(symbol)
            return {"symbol": symbol, "articles": articles}

        def _get_articles(symbol):
            return _request(symbol)

        def _request(symbol):
            try:
                return fetch(symbol)
            except Exception:
                return None
        """)
    report = analyze_repo(tmp_path)
    tool = next(t for t in report.tools if t.name == "market_sentiment")
    checks = {i.check for i in tool.issues}
    assert "error_handling" not in checks


def test_delegation_through_method_call_clears_error_handling(tmp_path):
    # Mirrors the real pattern found dogfooding MODSetter/SurfSense: a tool
    # calls a bare helper function, which delegates to an object *method*
    # (`client.request(...)`) rather than another bare function — the actual
    # try/except lives inside that method. All 28 of the repo's real tools
    # used this shape and were false-flagged before method calls were
    # resolved, since only bare-name calls (`foo(...)`) were followed.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        from service import run_scraper
        mcp = FastMCP("x")

        @mcp.tool()
        def scrape(query: str) -> str:
            \"\"\"Scrape something.

            Args:
                query: The search query.
            \"\"\"
            return run_scraper(query)
        """)
    write(tmp_path, "service.py", """
        class Client:
            def request(self, query):
                try:
                    return {"query": query}
                except Exception:
                    return {}

        def run_scraper(query):
            client = Client()
            return client.request(query)
        """)
    report = analyze_repo(tmp_path)
    tool = next(t for t in report.tools if t.name == "scrape")
    checks = {i.check for i in tool.issues}
    assert "error_handling" not in checks


def test_delegation_through_aliased_import_clears_error_handling(tmp_path):
    # Mirrors the real pattern found dogfooding against tradingview-mcp:
    # `from strategies import compare_strategies as _compare_strategies`,
    # called at the tool site as `_compare_strategies(...)`.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        from strategies import compare_strategies as _compare_strategies
        mcp = FastMCP("x")

        @mcp.tool()
        def compare(symbol: str) -> dict:
            \"\"\"Compare strategies.

            Args:
                symbol: The ticker.
            \"\"\"
            return _compare_strategies(symbol)
        """)
    write(tmp_path, "strategies.py", """
        def compare_strategies(symbol):
            try:
                return {"symbol": symbol}
            except Exception:
                return {}
        """)
    report = analyze_repo(tmp_path)
    tool = next(t for t in report.tools if t.name == "compare")
    checks = {i.check for i in tool.issues}
    assert "error_handling" not in checks


def test_delegation_to_unhandled_helper_still_flagged(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        def do_work(x):
            return 1 / x

        @mcp.tool()
        def divide(x: int) -> int:
            \"\"\"Divide.

            Args:
                x: The divisor.
            \"\"\"
            return do_work(x)
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    checks = {i.check for i in tool.issues}
    assert "error_handling" in checks


def test_delegation_to_external_call_still_flagged(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        import requests
        mcp = FastMCP("x")

        @mcp.tool()
        def fetch_url(url: str) -> str:
            \"\"\"Fetch a URL.

            Args:
                url: The URL.
            \"\"\"
            return requests.get(url).text
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    checks = {i.check for i in tool.issues}
    assert "error_handling" in checks


def test_lowlevel_tool_constructor_detected(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.types import Tool

        TOOLS = [
            Tool(
                name="search",
                description="Search the knowledge base for relevant docs.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search text"},
                    },
                },
            )
        ]
        """)
    report = analyze_repo(tmp_path)
    assert len(report.tools) == 1
    assert report.tools[0].name == "search"
    assert report.tools[0].issues == []


def test_lowlevel_tool_with_dynamic_name_is_skipped_not_unnamed(tmp_path):
    # A common class-based tool framework: a registry of tool objects, converted
    # to Tool(...) constructor calls in a loop. The name isn't a string literal,
    # so it can't be attributed to a single finding — must be skipped entirely,
    # not misreported as a fabricated "<unnamed>" tool.
    write(tmp_path, "server.py", """
        from mcp.types import Tool

        TOOLS = {"chat": ChatTool()}

        def handle_list_tools():
            tools = []
            for tool in TOOLS.values():
                tools.append(
                    Tool(
                        name=tool.name,
                        description=tool.description,
                        inputSchema=tool.get_input_schema(),
                    )
                )
            return tools
        """)
    report = analyze_repo(tmp_path)
    assert report.tools == []


def test_unparseable_file_is_flagged_not_silently_skipped(tmp_path):
    write(tmp_path, "server.py", """
        def broken(
        """)
    report = analyze_repo(tmp_path)
    parse_issue = next((i for i in report.repo_issues if i.check == "parse_error"), None)
    assert parse_issue is not None
    assert parse_issue.severity == "error"


def test_no_tools_found_gives_empty_report(tmp_path):
    write(tmp_path, "server.py", "x = 1\n")
    report = analyze_repo(tmp_path)
    assert report.tools == []


def test_hardcoded_secret_flagged(tmp_path):
    write(tmp_path, "server.py", """
        api_key = "sk-ab12cd34ef56gh78ij90kl"
        """)
    report = analyze_repo(tmp_path)
    assert any(i.check == "secrets" for i in report.repo_issues)


def test_identifier_named_like_a_secret_is_not_flagged(tmp_path):
    write(tmp_path, "server.py", """
        SERVICE_GET_CALLER_TOKEN = "get_caller_token"
        OAUTH_MODE_TOKEN = "oauth-mode-token"
        """)
    report = analyze_repo(tmp_path)
    assert not any(i.check == "secrets" for i in report.repo_issues)


def test_secret_pattern_in_test_file_is_not_flagged(tmp_path):
    write(tmp_path, "server.py", "x = 1\n")
    (tmp_path / "tests").mkdir()
    write(tmp_path, "tests/test_auth.py", """
        access_token = "sk-abcdefghijklmnopqrstuvwx"
        """)
    report = analyze_repo(tmp_path)
    assert not any(i.check == "secrets" for i in report.repo_issues)


def test_secret_pattern_in_js_test_file_is_not_flagged(tmp_path):
    write(tmp_path, "server.py", "x = 1\n")
    write(tmp_path, "client.test.ts", """
        const apiKey = "ctx7sk-abcdefghijklmnopqrstuvwx";
        """)
    report = analyze_repo(tmp_path)
    assert not any(i.check == "secrets" for i in report.repo_issues)


def test_secret_pattern_in_spec_file_is_not_flagged(tmp_path):
    write(tmp_path, "server.py", "x = 1\n")
    write(tmp_path, "client.spec.js", """
        const apiKey = "sk-abcdefghijklmnopqrstuvwx";
        """)
    report = analyze_repo(tmp_path)
    assert not any(i.check == "secrets" for i in report.repo_issues)


def test_tool_defined_in_test_file_is_not_counted(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def get_forecast(city: str) -> str:
            \"\"\"Get a weather forecast.

            Args:
                city: The city name.
            \"\"\"
            try:
                return city
            except ValueError as e:
                return str(e)
        """)
    (tmp_path / "tests").mkdir()
    write(tmp_path, "tests/test_middleware.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def fake_tool_for_testing(x):
            return x
        """)
    report = analyze_repo(tmp_path)
    assert len(report.tools) == 1
    assert report.tools[0].name == "get_forecast"


def test_annotated_field_description_counts_as_param_docs(tmp_path):
    write(tmp_path, "server.py", """
        from typing import Annotated
        from pydantic import Field
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def get_forecast(
            city: Annotated[str, Field(description="The city name.")],
            days: Annotated[int, Field(description="How many days out.")] = 1,
        ) -> str:
            \"\"\"Get a weather forecast.\"\"\"
            try:
                return f"{city} {days}"
            except ValueError as e:
                return str(e)
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    checks = {i.check for i in tool.issues}
    assert "param_docs" not in checks


def test_default_field_description_counts_as_param_docs(tmp_path):
    write(tmp_path, "server.py", """
        from pydantic import Field
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def get_forecast(city: str = Field(description="The city name.")) -> str:
            \"\"\"Get a weather forecast.\"\"\"
            try:
                return city
            except ValueError as e:
                return str(e)
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    checks = {i.check for i in tool.issues}
    assert "param_docs" not in checks


def test_cross_file_field_alias_counts_as_param_docs(tmp_path):
    write(tmp_path, "shared_types.py", """
        from typing import Annotated
        from pydantic import Field

        NameParam = Annotated[str, Field(description="The city name.")]
        """)
    write(tmp_path, "server.py", """
        from typing import Annotated
        from mcp.server.fastmcp import FastMCP
        from .shared_types import NameParam
        mcp = FastMCP("x")

        @mcp.tool()
        def get_forecast(city: NameParam = None) -> str:
            \"\"\"Get a weather forecast.\"\"\"
            try:
                return city
            except ValueError as e:
                return str(e)
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    checks = {i.check for i in tool.issues}
    assert "param_docs" not in checks


def test_exclude_args_param_not_required_to_be_documented(tmp_path):
    write(tmp_path, "server.py", """
        from typing import Any
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool(exclude_args=["extractor"])
        def search_posts(keywords: str, extractor: Any | None = None) -> str:
            \"\"\"Search posts.

            Args:
                keywords: Search keywords.
            \"\"\"
            try:
                return keywords
            except ValueError as e:
                return str(e)
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    checks = {i.check for i in tool.issues}
    assert "param_docs" not in checks
    assert tool.param_count == 1


def test_context_param_not_required_to_be_documented(tmp_path):
    # Found dogfooding CursorTouch/Windows-MCP: FastMCP injects a Context-typed
    # parameter at call time and strips it from the tool's exposed schema
    # before it's ever built (verified against fastmcp's own
    # function_parsing.py, without_injected_parameters) — same treatment as
    # self/cls, without needing an explicit exclude_args entry. Every one of
    # the target repo's tools had this param, and every one was false-flagged
    # for "undocumented" because of it, even when every real param had a
    # Field(description=...).
    write(tmp_path, "server.py", """
        from typing import Annotated
        from fastmcp import Context
        from mcp.server.fastmcp import FastMCP
        from pydantic import Field
        mcp = FastMCP("x")

        @mcp.tool(name="Notification")
        def notification_tool(
            title: Annotated[str, Field(description="The notification title.")],
            ctx: Context = None,
        ) -> str:
            return title
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    checks = {i.check for i in tool.issues}
    assert "param_docs" not in checks
    assert tool.param_count == 1


def test_undocumented_non_excluded_param_still_flagged(tmp_path):
    write(tmp_path, "server.py", """
        from typing import Any
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool(exclude_args=["extractor"])
        def search_posts(keywords: str, note: str, extractor: Any | None = None) -> str:
            \"\"\"Search posts.

            Args:
                keywords: Search keywords.
            \"\"\"
            try:
                return keywords
            except ValueError as e:
                return str(e)
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    checks = {i.check for i in tool.issues}
    assert "param_docs" in checks


def test_bold_bulleted_parameters_heading_recognized(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def ha_restart(confirm: bool = False) -> dict:
            \"\"\"
            Restart the system.

            **Parameters:**
            - confirm: Must be set to True to confirm the restart. This is a
                       safety measure to prevent accidental restarts.
            \"\"\"
            try:
                return {"ok": confirm}
            except ValueError as e:
                return {"error": str(e)}
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    checks = {i.check for i in tool.issues}
    assert "param_docs" not in checks


def test_tool_name_violating_charset_flagged(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool(name="do thing!")
        def do_thing() -> str:
            \"\"\"Does a thing.\"\"\"
            try:
                return "ok"
            except ValueError as e:
                return str(e)
        """)
    report = analyze_repo(tmp_path)
    issue = next(i for i in report.repo_issues if i.check == "tool_name" and "1-128" in i.message)
    assert "do thing!" in issue.message


def test_duplicate_tool_names_flagged(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool(name="dupe")
        def a() -> str:
            \"\"\"First.\"\"\"
            try:
                return "a"
            except ValueError as e:
                return str(e)

        @mcp.tool(name="dupe")
        def b() -> str:
            \"\"\"Second.\"\"\"
            try:
                return "b"
            except ValueError as e:
                return str(e)
        """)
    report = analyze_repo(tmp_path)
    issue = next(i for i in report.repo_issues if i.check == "tool_name" and "unique" in i.message)
    assert "dupe" in issue.message


def test_valid_tool_name_not_flagged(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def get_forecast(city: str) -> str:
            \"\"\"Get a weather forecast.

            Args:
                city: The city name.
            \"\"\"
            try:
                return city
            except ValueError as e:
                return str(e)
        """)
    report = analyze_repo(tmp_path)
    assert not any(i.check == "tool_name" for i in report.repo_issues)


def test_missing_readme_and_license_flagged(tmp_path):
    write(tmp_path, "server.py", "x = 1\n")
    report = analyze_repo(tmp_path)
    checks = {i.check for i in report.repo_issues}
    assert "readme" in checks
    assert "license" in checks


def test_cli_runs_against_bad_example_and_reports_low_score():
    example = REPO_ROOT / "examples" / "bad_server"
    result = subprocess.run(
        [sys.executable, "-m", "mcp_doctor.cli", str(example), "--json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert '"score"' in result.stdout


def test_cli_fail_under_exits_nonzero_on_bad_example():
    example = REPO_ROOT / "examples" / "bad_server"
    result = subprocess.run(
        [sys.executable, "-m", "mcp_doctor.cli", str(example), "--fail-under", "90"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1


def test_cli_good_example_scores_well():
    example = REPO_ROOT / "examples" / "good_server"
    result = subprocess.run(
        [sys.executable, "-m", "mcp_doctor.cli", str(example), "--fail-under", "50"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


def test_cli_good_example_ts_scores_well():
    example = REPO_ROOT / "examples" / "good_server_ts"
    result = subprocess.run(
        [sys.executable, "-m", "mcp_doctor.cli", str(example), "--fail-under", "90"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


def test_cli_bad_example_ts_reports_low_score():
    example = REPO_ROOT / "examples" / "bad_server_ts"
    result = subprocess.run(
        [sys.executable, "-m", "mcp_doctor.cli", str(example), "--fail-under", "50"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
