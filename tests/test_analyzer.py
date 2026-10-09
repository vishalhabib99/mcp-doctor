import subprocess
import sys
from pathlib import Path
from textwrap import dedent

from mcp_doctor.analyzer import analyze_repo

REPO_ROOT = Path(__file__).resolve().parent.parent


def write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
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
    (tmp_path / "requirements.txt").write_text("mcp==1.0.0\n")
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


def test_lowlevel_tool_property_from_zero_arg_builder_counts_as_documented(tmp_path):
    # `"paper_id": _paper_id_property()` — a schema property built by a
    # shared zero-arg helper rather than written inline — verified against
    # blazickjp/arxiv-mcp-server's `_paper_id_property`. The helper's own
    # dict literal has a description; that should count, not be reported as
    # undocumented just because it's a call rather than a literal.
    write(tmp_path, "server.py", """
        from mcp.types import Tool

        def _paper_id_property():
            return {"type": "string", "description": "Validated arXiv paper ID"}

        TOOLS = [
            Tool(
                name="get_paper",
                description="Get a paper by its arXiv ID.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "paper_id": _paper_id_property(),
                    },
                },
            )
        ]
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    assert not any(i.check == "param_docs" for i in tool.issues)


def test_lowlevel_tool_spread_properties_from_zero_arg_builder_expanded(tmp_path):
    # `**_page_properties()` — several properties spread in from a shared
    # helper, rather than counted as one opaque, always-undocumented
    # property (the pre-fix behavior: **spread's key is None, so it never
    # matched the inline-dict check at all).
    write(tmp_path, "server.py", """
        from mcp.types import Tool

        def _page_properties():
            return {
                "start": {"type": "integer", "description": "Offset"},
                "max_chars": {"type": "integer", "description": "Limit"},
                "return_full_text": {"type": "boolean"},
            }

        TOOLS = [
            Tool(
                name="read_section",
                description="Read part of a paper section.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "section_id": {"type": "string", "description": "Section id"},
                        **_page_properties(),
                    },
                },
            )
        ]
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    assert tool.param_count == 4
    assert tool.typed_param_count == 3
    docs_issue = next(i for i in tool.issues if i.check == "param_docs")
    assert "1/4" in docs_issue.message


def test_lowlevel_tool_property_from_parameterized_call_not_guessed(tmp_path):
    # A property built by a call that takes arguments isn't safely
    # resolvable without evaluating it with the right arguments — correctly
    # left as an opaque, undocumented property rather than guessed at.
    write(tmp_path, "server.py", """
        from mcp.types import Tool

        def _string_property(desc):
            return {"type": "string", "description": desc}

        TOOLS = [
            Tool(
                name="get_paper",
                description="Get a paper by its arXiv ID.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "paper_id": _string_property("Validated arXiv paper ID"),
                    },
                },
            )
        ]
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    assert tool.param_count == 1
    assert tool.typed_param_count == 0
    assert any(i.check == "param_docs" for i in tool.issues)


def test_unparseable_file_is_flagged_not_silently_skipped(tmp_path):
    write(tmp_path, "server.py", """
        def broken(
        """)
    report = analyze_repo(tmp_path)
    parse_issue = next((i for i in report.repo_issues if i.check == "parse_error"), None)
    assert parse_issue is not None
    assert parse_issue.severity == "error"


def test_file_starting_with_utf8_bom_is_parsed(tmp_path):
    # Python runs a file that starts with a UTF-8 byte-order mark (common from
    # Windows editors), but ast.parse on a str rejects U+FEFF. Found on 71 files
    # across the fastmcp sweep (e.g. J621111/live2d-automation).
    (tmp_path / "server.py").write_bytes(
        b"\xef\xbb\xbfimport fastmcp\nmcp = fastmcp.FastMCP('x')\n\n"
        b"@mcp.tool()\ndef ping() -> str:\n    \"\"\"Ping.\"\"\"\n    return 'pong'\n"
    )
    report = analyze_repo(tmp_path)
    assert [t.name for t in report.tools] == ["ping"]
    assert not any(i.check == "parse_error" for i in report.repo_issues)


def test_no_tools_found_gives_empty_report(tmp_path):
    write(tmp_path, "server.py", "x = 1\n")
    report = analyze_repo(tmp_path)
    assert report.tools == []


def test_class_based_tool_detected_with_name_from_class_and_class_docstring(tmp_path):
    # oraios/serena's own shape: no decorator, no Tool(...) constructor — the
    # tool name is derived from the class name and the description from the
    # class's own docstring, not `apply`'s.
    write(tmp_path, "server.py", """
        class ReadFileTool(Tool):
            \"\"\"Reads a file within the project directory.\"\"\"

            def apply(self, relative_path: str, start_line: int = 0) -> str:
                \"\"\"
                Reads the given file or a chunk of it.

                :param relative_path: the relative path to the file to read
                :param start_line: the 0-based index of the first line to retrieve
                \"\"\"
                try:
                    return relative_path
                except OSError as e:
                    return str(e)
        """)
    report = analyze_repo(tmp_path)
    assert len(report.tools) == 1
    tool = report.tools[0]
    assert tool.name == "read_file"
    assert tool.description_text == "Reads a file within the project directory."
    assert tool.issues == []


def test_class_based_tool_without_apply_method_is_not_a_tool(tmp_path):
    write(tmp_path, "server.py", """
        class ToolMarker:
            \"\"\"Not a real tool — a marker base class, no apply() method.\"\"\"
        """)
    report = analyze_repo(tmp_path)
    assert report.tools == []


def test_class_based_tool_with_no_class_docstring_falls_back_to_apply_docstring(tmp_path):
    # Verified against serena's own src/serena/mcp.py: `func_doc =
    # tool.get_apply_docstring() or ""` is passed straight into
    # `description=` at tool-registration time — the *class* docstring is
    # never read there. Real tools like SearchForPatternTool and
    # SafeDeleteSymbol only ever docstring the apply() method, never the
    # class itself, and are genuinely described to the agent at runtime.
    write(tmp_path, "server.py", """
        class SearchForPatternTool(Tool):
            def apply(self, substring_pattern: str) -> str:
                \"\"\"
                Searches for a pattern.

                :param substring_pattern: the pattern
                \"\"\"
                return substring_pattern
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    assert tool.description_text == "Searches for a pattern."
    assert not any(i.check == "description" for i in tool.issues)


def test_class_based_tool_with_no_docstring_anywhere_flags_missing_description(tmp_path):
    write(tmp_path, "server.py", """
        class SearchForPatternTool(Tool):
            def apply(self, substring_pattern: str) -> str:
                return substring_pattern
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    assert tool.description_text == ""
    assert any(i.check == "description" for i in tool.issues)


def test_sphinx_style_param_docs_recognized_for_decorator_tool(tmp_path):
    # :param name: ... (reST/Sphinx style) is a distinct, real convention from
    # the Google-style `Args:` section already supported — no heading needed.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def get_forecast(city: str, days: int) -> str:
            \"\"\"Get a weather forecast.

            :param city: the city name
            :param int days: how many days out
            \"\"\"
            try:
                return f"{city} {days}"
            except ValueError as e:
                return str(e)
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    assert not any(i.check == "param_docs" for i in tool.issues)


def test_numpy_style_param_docs_recognized(tmp_path):
    # NumPy style: a "Parameters" heading underlined with dashes, then
    # "name : type" lines (kicad-mcp-pro uses it on ~35 tools). FastMCP sends
    # the whole docstring as the tool description, so the model sees these.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def get_forecast(city: str, days: int, lat: float, lon: float) -> str:
            \"\"\"Get a weather forecast.

            Parameters
            ----------
            city : str
                The city name.
            days : int
                How many days out.
            lat, lon : float
                Optional coordinates.

            Returns
            -------
            str
                The forecast.
            \"\"\"
            try:
                return f"{city} {days} {lat} {lon}"
            except ValueError as e:
                return str(e)
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    assert not any(i.check == "param_docs" for i in tool.issues)


def test_numpy_style_partial_docs_still_flagged(tmp_path):
    # Only `city` is documented, and names under Returns don't count.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def get_forecast(city: str, days: int) -> str:
            \"\"\"Get a weather forecast.

            Parameters
            ----------
            city : str
                The city name.

            Returns
            -------
            days : str
                Not a parameter.
            \"\"\"
            try:
                return f"{city} {days}"
            except ValueError as e:
                return str(e)
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    assert any(i.check == "param_docs" for i in tool.issues)


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


def test_mounted_namespace_tools_not_flagged_as_duplicate(tmp_path):
    write(tmp_path, "jira.py", """
        from mcp.server.fastmcp import FastMCP
        jira_mcp = FastMCP("jira")

        @jira_mcp.tool()
        def search() -> str:
            \"\"\"Search Jira issues.\"\"\"
            try:
                return "a"
            except ValueError as e:
                return str(e)
        """)
    write(tmp_path, "confluence.py", """
        from mcp.server.fastmcp import FastMCP
        confluence_mcp = FastMCP("confluence")

        @confluence_mcp.tool()
        def search() -> str:
            \"\"\"Search Confluence pages.\"\"\"
            try:
                return "a"
            except ValueError as e:
                return str(e)
        """)
    write(tmp_path, "main.py", """
        from mcp.server.fastmcp import FastMCP
        from .jira import jira_mcp
        from .confluence import confluence_mcp

        main_mcp = FastMCP("main")
        main_mcp.mount(jira_mcp, namespace="jira")
        main_mcp.mount(confluence_mcp, namespace="confluence")
        """)
    report = analyze_repo(tmp_path)
    dup_issues = [i for i in report.repo_issues if i.check == "tool_name" and "unique" in i.message]
    assert not dup_issues
    names = {t.name for t in report.tools}
    assert "jira_search" in names
    assert "confluence_search" in names


def test_standalone_entrypoint_files_not_compared_for_duplicates(tmp_path):
    write(tmp_path, "main_server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("real")

        @mcp.tool()
        def create_object() -> str:
            \"\"\"Create a real object.\"\"\"
            try:
                return "a"
            except ValueError as e:
                return str(e)

        if __name__ == "__main__":
            mcp.run()
        """)
    write(tmp_path, "scratch_mcp.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("scratch")

        @mcp.tool()
        def create_object() -> str:
            \"\"\"Scratch reimplementation for local testing.\"\"\"
            try:
                return "b"
            except ValueError as e:
                return str(e)

        if __name__ == "__main__":
            mcp.run()
        """)
    report = analyze_repo(tmp_path)
    dup_issues = [i for i in report.repo_issues if i.check == "tool_name" and "unique" in i.message]
    assert not dup_issues


def test_real_duplicate_within_standalone_entrypoint_still_flagged(tmp_path):
    write(tmp_path, "scratch_mcp.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("scratch")

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

        if __name__ == "__main__":
            mcp.run()
        """)
    report = analyze_repo(tmp_path)
    issue = next(i for i in report.repo_issues if i.check == "tool_name" and "unique" in i.message)
    assert "dupe" in issue.message


def _dup_issues(report):
    return [i for i in report.repo_issues if i.check == "tool_name" and "unique" in i.message]


def test_supervisor_and_worker_via_main_not_compared_for_duplicates(tmp_path):
    # mrexodia/ida-pro-mcp: a supervisor server and a worker server, each started from main(),
    # both expose idb_open. They're separate processes, so the names don't collide.
    for name, label in (("supervisor.py", "supervisor"), ("worker.py", "worker")):
        write(tmp_path, name, f"""
            from mcp.server.fastmcp import FastMCP
            mcp = FastMCP("{label}")

            @mcp.tool()
            def idb_open(path: str) -> str:
                \"\"\"Open a database.\"\"\"
                return path

            def main():
                mcp.run()

            if __name__ == "__main__":
                main()
            """)
    assert not _dup_issues(analyze_repo(tmp_path))


def test_instance_imported_elsewhere_is_one_server(tmp_path):
    # A main()-started server whose instance another module imports to register more tools:
    # all of them share one server, so a name declared in both files is a real duplicate.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def search(q: str) -> str:
            \"\"\"Search.\"\"\"
            return q

        def main():
            mcp.run()

        if __name__ == "__main__":
            main()
        """)
    write(tmp_path, "tools.py", """
        from server import mcp

        @mcp.tool()
        def search(q: str) -> str:
            \"\"\"Search again.\"\"\"
            return q
        """)
    issues = _dup_issues(analyze_repo(tmp_path))
    assert issues and "search" in issues[0].message


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


def test_direct_call_tool_resolved_via_same_name_function(tmp_path):
    # `provider.tool(get_forecast, name="...")` — the plain, unaliased case:
    # the registered function is literally the def it names, no reassignment
    # to trace at all. One of FastMCP's own documented `.tool()` calling
    # patterns ("direct function call"), distinct from decorator use.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        def get_forecast(city: str) -> str:
            \"\"\"Get a weather forecast.

            Args:
                city: The city name.
            \"\"\"
            try:
                return city
            except ValueError as e:
                return str(e)

        mcp.tool(get_forecast, name="get_forecast", description="Get a weather forecast for a city.")
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    assert tool.name == "get_forecast"
    assert tool.description_text == "Get a weather forecast for a city."
    assert tool.param_count == 1
    assert not any(i.check == "param_docs" for i in tool.issues)


def test_direct_call_tool_resolved_via_single_reassignment(tmp_path):
    # `find_foo = find` (one unconditional rename), then registered as
    # `find_foo` — still safely resolvable back to `find`'s own definition.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        def setup():
            def find(query: str) -> str:
                \"\"\"Find things.

                Args:
                    query: What to search for.
                \"\"\"
                try:
                    return query
                except ValueError as e:
                    return str(e)

            find_foo = find
            mcp.tool(find_foo, name="qdrant-find", description="Find memories.")
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    assert tool.name == "qdrant-find"
    assert tool.param_count == 1
    assert not any(i.check == "param_docs" for i in tool.issues)


def test_direct_call_tool_with_ambiguous_reassignment_not_guessed(tmp_path):
    # `find_foo` is reassigned again through a wrapping call before
    # registration (a common way to conditionally post-process a tool
    # function — verified against qdrant/mcp-server-qdrant) — which branch
    # actually runs depends on runtime config, so this is correctly left
    # unresolved: name/description are still checked, but params aren't
    # guessed at from the wrong (or right) branch.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        def setup():
            def find(query: str) -> str:
                \"\"\"Find things.

                Args:
                    query: What to search for.
                \"\"\"
                try:
                    return query
                except ValueError as e:
                    return str(e)

            find_foo = find
            if some_condition:
                find_foo = wrap_filters(find_foo)
            mcp.tool(find_foo, name="qdrant-find", description="Find memories.")
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    assert tool.name == "qdrant-find"
    assert tool.description_text == "Find memories."
    assert tool.param_count == 0
    assert not any(i.check == "param_docs" for i in tool.issues)


def test_direct_call_tool_with_non_literal_description_not_guessed(tmp_path):
    # `description=self.tool_settings.tool_find_description` — a settings
    # attribute reference, not a literal string — combined with an
    # unresolvable registered function (no docstring to fall back on
    # either). Correctly left with no description at all rather than
    # chasing the attribute back through a Settings class's Field default.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        def setup():
            def find(query: str) -> str:
                return query

            find_foo = find
            if some_condition:
                find_foo = wrap_filters(find_foo)
            mcp.tool(find_foo, name="qdrant-find", description=self.tool_settings.tool_find_description)
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    assert tool.name == "qdrant-find"
    assert not tool.has_description
    assert any(i.check == "description" for i in tool.issues)


def test_direct_call_without_name_uses_function_name(tmp_path):
    # reinthal/icloud-calendar-mcp: `mcp.tool(list_calendars)`, no name=.
    write(tmp_path, "server.py", """
        from fastmcp import FastMCP
        mcp = FastMCP("x")

        def list_calendars(account: str) -> str:
            \"\"\"List calendars.

            Args:
                account: Account id.
            \"\"\"
            return account

        mcp.tool(list_calendars)
        """)
    report = analyze_repo(tmp_path)
    assert [t.name for t in report.tools] == ["list_calendars"]
    assert report.tools[0].param_count == 1


def test_decorator_applied_by_hand_to_self_method(tmp_path):
    # openstack-kr/python-openstackmcp-server: `mcp.tool()(self.get_regions)`
    # inside a register method; `self` is not a tool parameter.
    write(tmp_path, "tools.py", """
        from fastmcp import FastMCP

        class IdentityTools:
            def register_tools(self, mcp: FastMCP):
                mcp.tool()(self.get_regions)
                mcp.tool(name="region_get")(self.get_region)

            def get_regions(self) -> list:
                \"\"\"List every region.\"\"\"
                return []

            def get_region(self, region_id: str) -> dict:
                \"\"\"Get one region by id.\"\"\"
                return {}
        """)
    report = analyze_repo(tmp_path)
    by_name = {t.name: t for t in report.tools}
    assert set(by_name) == {"get_regions", "region_get"}
    assert by_name["get_regions"].param_count == 0
    assert by_name["region_get"].param_count == 1


def test_add_tool_with_function_imported_from_another_module(tmp_path):
    # lens-finance/mcp: app.py imports each tool function from its own module
    # and registers it with `app.add_tool(fn)`. The finding points at the
    # function's own file.
    (tmp_path / "mcp_server" / "tools").mkdir(parents=True)
    write(tmp_path, "mcp_server/__init__.py", "")
    write(tmp_path, "mcp_server/tools/__init__.py", "")
    write(tmp_path, "mcp_server/tools/net_worth.py", """
        def get_net_worth(user_id: str) -> float:
            \"\"\"Return the user's net worth.\"\"\"
            return 0.0
        """)
    write(tmp_path, "mcp_server/tools/items.py", """
        def get_all_items() -> list:
            \"\"\"Return every linked item.\"\"\"
            return []
        """)
    write(tmp_path, "app.py", """
        from fastmcp import FastMCP
        from mcp_server.tools.net_worth import get_net_worth
        from .mcp_server.tools import items

        app = FastMCP("lens")
        app.add_tool(get_net_worth)
        app.tool(items.get_all_items)
        """)
    report = analyze_repo(tmp_path)
    by_name = {t.name: t for t in report.tools}
    assert set(by_name) == {"get_net_worth", "get_all_items"}
    assert by_name["get_net_worth"].file == "mcp_server/tools/net_worth.py"


def test_add_tool_with_tool_from_function(tmp_path):
    # SemyonSinchenko/pyspark-mcp-server: add_tool(Tool.from_function(fn, name=...)).
    write(tmp_path, "server.py", """
        from fastmcp import FastMCP
        from fastmcp.tools import Tool
        mcp = FastMCP("x")

        def read_table(table: str) -> str:
            \"\"\"Read a table.\"\"\"
            return table

        mcp.add_tool(Tool.from_function(read_table, name="spark_read_table"))
        """)
    report = analyze_repo(tmp_path)
    assert [t.name for t in report.tools] == ["spark_read_table"]


def test_tool_from_function_counts_where_defined(tmp_path):
    # fancyboi999/daily-hot-mcp: each module builds its tool with
    # Tool.from_function(fn=..., name=...); server.py adds them in a loop.
    (tmp_path / "tools").mkdir()
    write(tmp_path, "tools/kr36.py", """
        from fastmcp.tools import Tool

        def get_36kr(limit: int = 10) -> list:
            \"\"\"Trending on 36kr.\"\"\"
            return []

        kr36_tool = Tool.from_function(fn=get_36kr, name="get-36kr-trending", description="36kr trending.")
        """)
    write(tmp_path, "server.py", """
        from fastmcp import FastMCP
        from tools.kr36 import kr36_tool
        server = FastMCP("hot")
        for tool in [kr36_tool]:
            server.add_tool(tool)
        """)
    report = analyze_repo(tmp_path)
    assert [t.name for t in report.tools] == ["get-36kr-trending"]
    assert report.tools[0].param_count == 1


def test_by_reference_registration_ignored_outside_fastmcp_files(tmp_path):
    # pydantic-ai and other agent frameworks have their own `.tool(fn)` and
    # `.add_tool(fn)`; without a FastMCP import these aren't MCP tools.
    write(tmp_path, "agent.py", """
        from pydantic_ai import Agent
        agent = Agent("model")

        def roll_die() -> str:
            \"\"\"Roll a die.\"\"\"
            return "4"

        agent.tool(roll_die)
        agent.add_tool(roll_die)
        """)
    report = analyze_repo(tmp_path)
    assert report.tools == []


def test_unresolvable_reference_without_name_not_reported(tmp_path):
    # A loop variable or a factory call can't be named without running code.
    write(tmp_path, "server.py", """
        from fastmcp import FastMCP
        mcp = FastMCP("x")
        for fn in load_plugins():
            mcp.tool()(fn)
        mcp.add_tool(make_tool("x"))
        """)
    report = analyze_repo(tmp_path)
    assert report.tools == []


def test_direct_call_not_confused_with_decorator_usage(tmp_path):
    # A decorator call site (`@mcp.tool(name=...)`) shouldn't also be
    # double-counted as a direct-call registration — its shape (no
    # positional function argument in the call itself) doesn't match the
    # direct-call pattern's detection criterion.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool(name="get_forecast", description="Get a weather forecast.")
        def get_forecast(city: str) -> str:
            try:
                return city
            except ValueError as e:
                return str(e)
        """)
    report = analyze_repo(tmp_path)
    assert len(report.tools) == 1


def test_missing_readme_and_license_flagged(tmp_path):
    write(tmp_path, "server.py", "x = 1\n")
    report = analyze_repo(tmp_path)
    checks = {i.check for i in report.repo_issues}
    assert "readme" in checks
    assert "license" in checks


def test_undocumented_tool_missing_from_readme_is_flagged(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def get_forecast(city: str) -> str:
            \"\"\"Get a weather forecast.

            Args:
                city: The city name.
            \"\"\"
            return city
        """)
    (tmp_path / "README.md").write_text("# x\n\nNo tools mentioned here.")

    report = analyze_repo(tmp_path)
    readme_issues = [i for i in report.repo_issues if i.check == "readme"]
    assert any("get_forecast" in i.message for i in readme_issues)


def test_deprecated_tool_missing_from_readme_is_not_flagged(tmp_path):
    # Real false positive found dogfooding firecrawl/firecrawl-mcp-server:
    # `firecrawl_extract`'s description opens with "Deprecated compatibility
    # entry point. Use firecrawl_scrape instead" and is correctly left out of
    # the README's tool list, which only documents the current surface —
    # flagging it pushes toward re-documenting a tool being phased out.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def legacy_extract(url: str) -> str:
            \"\"\"Deprecated compatibility entry point. Use scrape instead.

            Args:
                url: The URL to extract.
            \"\"\"
            return url
        """)
    (tmp_path / "README.md").write_text("# x\n\nNo tools mentioned here.")

    report = analyze_repo(tmp_path)
    readme_issues = [i for i in report.repo_issues if i.check == "readme"]
    assert not any("legacy_extract" in i.message for i in readme_issues)


def test_tool_documented_only_in_a_linked_md_file_is_not_flagged(tmp_path):
    # Real false positive found dogfooding the official
    # modelcontextprotocol/servers "everything" reference server: its
    # README says "A complete list of the registered MCP primitives...
    # can be found in the Server Features document" and links
    # docs/features.md, which lists every tool by name — the README
    # itself never repeats them.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def get_structured_content(location: str) -> str:
            \"\"\"Demonstrate a structured response.

            Args:
                location: Where to look up.
            \"\"\"
            return location
        """)
    (tmp_path / "README.md").write_text(
        "# x\n\nA complete list of tools can be found in the "
        "[Server Features](docs/features.md) document.\n"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "features.md").write_text(
        "- `get_structured_content`: demonstrates a structured response.\n"
    )

    report = analyze_repo(tmp_path)
    readme_issues = [i for i in report.repo_issues if i.check == "readme"]
    assert not any("get_structured_content" in i.message for i in readme_issues)


def test_linked_doc_outside_repo_root_is_not_followed(tmp_path):
    # A relative link that escapes the repo root (e.g. `../../secrets.md`)
    # must not be read — guards the traversal check above, not a pattern
    # seen in the wild, just a safety boundary worth pinning down.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def get_widget(id: str) -> str:
            \"\"\"Get a widget.

            Args:
                id: The widget id.
            \"\"\"
            return id
        """)
    outside = tmp_path.parent / "outside_docs.md"
    outside.write_text("- `get_widget`: fully documented here.\n")
    (tmp_path / "README.md").write_text(
        "# x\n\nSee [outside docs](../outside_docs.md) for the tool list.\n"
    )

    try:
        report = analyze_repo(tmp_path)
        readme_issues = [i for i in report.repo_issues if i.check == "readme"]
        assert any("get_widget" in i.message for i in readme_issues)
    finally:
        outside.unlink()


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


def test_bare_url_param_flagged_for_missing_format_hint(tmp_path):
    write(tmp_path, "server.py", """
        from typing import Annotated
        from pydantic import Field
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def add_bookmark(url: Annotated[str, Field(description="The bookmark URL.")]) -> str:
            \"\"\"Add a bookmark.\"\"\"
            try:
                return url
            except ValueError as e:
                return str(e)
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    issue = next(i for i in tool.issues if i.check == "url_format_hint")
    assert "url" in issue.message


def test_icon_url_optional_str_also_flagged(tmp_path):
    write(tmp_path, "server.py", """
        from typing import Annotated, Optional
        from pydantic import Field
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def create_link(
            icon_url: Annotated[Optional[str], Field(description="Icon URL.")] = None,
        ) -> str:
            \"\"\"Create a link.\"\"\"
            try:
                return icon_url or ""
            except ValueError as e:
                return str(e)
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    issue = next(i for i in tool.issues if i.check == "url_format_hint")
    assert "icon_url" in issue.message


def test_field_format_kwarg_clears_url_format_hint(tmp_path):
    write(tmp_path, "server.py", """
        from typing import Annotated
        from pydantic import Field
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def add_bookmark(
            url: Annotated[str, Field(description="The bookmark URL.", format="uri")],
        ) -> str:
            \"\"\"Add a bookmark.\"\"\"
            try:
                return url
            except ValueError as e:
                return str(e)
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    assert not any(i.check == "url_format_hint" for i in tool.issues)


def test_json_schema_extra_format_clears_url_format_hint(tmp_path):
    write(tmp_path, "server.py", """
        from typing import Annotated
        from pydantic import Field
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def add_bookmark(
            url: Annotated[str, Field(description="The bookmark URL.", json_schema_extra={"format": "uri"})],
        ) -> str:
            \"\"\"Add a bookmark.\"\"\"
            try:
                return url
            except ValueError as e:
                return str(e)
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    assert not any(i.check == "url_format_hint" for i in tool.issues)


def test_default_value_field_format_clears_url_format_hint(tmp_path):
    write(tmp_path, "server.py", """
        from pydantic import Field
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def add_bookmark(url: str = Field(description="The bookmark URL.", format="uri")) -> str:
            \"\"\"Add a bookmark.\"\"\"
            try:
                return url
            except ValueError as e:
                return str(e)
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    assert not any(i.check == "url_format_hint" for i in tool.issues)


def test_non_url_param_name_not_falsely_flagged(tmp_path):
    write(tmp_path, "server.py", """
        from typing import Annotated
        from pydantic import Field
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def run_curl(curl_command: Annotated[str, Field(description="Shell command.")]) -> str:
            \"\"\"Run a curl command.\"\"\"
            try:
                return curl_command
            except ValueError as e:
                return str(e)
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    assert not any(i.check == "url_format_hint" for i in tool.issues)


def test_non_str_url_param_not_flagged(tmp_path):
    write(tmp_path, "server.py", """
        from typing import Annotated
        from pydantic import Field
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def fetch(url: Annotated[bytes, Field(description="Encoded URL.")]) -> str:
            \"\"\"Fetch something.\"\"\"
            try:
                return str(url)
            except ValueError as e:
                return str(e)
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    assert not any(i.check == "url_format_hint" for i in tool.issues)


def test_cjk_description_is_not_falsely_flagged_as_too_short(tmp_path):
    # Real bug found on xpzouying/xiaohongshu-mcp (15.6k★): a complete,
    # well-formed Chinese description is only 9 raw characters, under the
    # 10-char threshold calibrated for English character density — but each
    # CJK character conveys roughly a full word's worth of meaning, so a raw
    # count unfairly flags it. Fixed via description_display_width, which
    # counts a Wide/Fullwidth character as 2 columns (wcwidth convention).
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def check_login() -> bool:
            \"\"\"检查小红书登录状态\"\"\"
            return True
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    assert not any(i.check == "description" for i in tool.issues)


def test_short_cjk_description_is_still_flagged(tmp_path):
    # The display-width fix must not disable the check for CJK text
    # entirely — a genuinely vague one-word description should still warn.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def get_weather() -> str:
            \"\"\"天气\"\"\"
            return "sunny"
        """)
    report = analyze_repo(tmp_path)
    tool = report.tools[0]
    issues = [i for i in tool.issues if i.check == "description"]
    assert len(issues) == 1
    assert issues[0].severity == "warning"


def _param_docs_issues(report):
    return [i for t in report.tools for i in t.issues if i.check == "param_docs"]


def test_plain_string_annotated_counts_as_docs_with_standalone_fastmcp(tmp_path):
    # fastmcp 3.4.7 turns Annotated[T, "text"] into the parameter's description
    # (verified 2026-09-29 by listing the tool's inputSchema).
    write(tmp_path, "server.py", """
        from typing import Annotated
        from fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool
        def add_bp(addr: Annotated[str, "Address to break at"]) -> str:
            \"\"\"Add a breakpoint.\"\"\"
            return addr
        """)
    assert _param_docs_issues(analyze_repo(tmp_path)) == []


def test_plain_string_annotated_counts_as_docs_with_custom_framework(tmp_path):
    # mrexodia/ida-pro-mcp's own zeromcp reads the string as the description.
    write(tmp_path, "api.py", """
        from typing import Annotated
        from .rpc import tool

        @tool
        def dbg_add_bp(addrs: Annotated[list[str] | str, "Address(es) to add breakpoints at"]) -> list:
            \"\"\"Add breakpoints at one or more addresses.\"\"\"
            return []
        """)
    assert _param_docs_issues(analyze_repo(tmp_path)) == []


def test_plain_string_annotated_flagged_specifically_with_official_sdk(tmp_path):
    # The official SDK (1.30 and 2.1) drops bare-string Annotated metadata, so
    # the model never sees it; the warning names the param and the fix.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")
        """)
    write(tmp_path, "tools.py", """
        from typing import Annotated
        from server import mcp

        @mcp.tool()
        def add_bp(addr: Annotated[str, "Address to break at"]) -> str:
            \"\"\"Add a breakpoint.\"\"\"
            return addr
        """)
    issues = _param_docs_issues(analyze_repo(tmp_path))
    assert len(issues) == 1
    assert "addr" in issues[0].message and "official MCP Python SDK drops" in issues[0].message


def test_official_sdk_v2_import_also_drops_plain_string(tmp_path):
    write(tmp_path, "server.py", """
        from typing import Annotated
        from mcp.server.mcpserver import MCPServer
        mcp = MCPServer("x")

        @mcp.tool()
        def add_bp(addr: Annotated[str, "Address to break at"]) -> str:
            \"\"\"Add a breakpoint.\"\"\"
            return addr
        """)
    assert len(_param_docs_issues(analyze_repo(tmp_path))) == 1


def test_annotated_type_alone_is_not_docs(tmp_path):
    # Only metadata after the type counts; Annotated["SomeType", ...] with a
    # forward-ref string as the type itself must not.
    write(tmp_path, "server.py", """
        from typing import Annotated
        from fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool
        def add_bp(addr: Annotated["Address", 1]) -> str:
            \"\"\"Add a breakpoint.\"\"\"
            return addr
        """)
    issues = _param_docs_issues(analyze_repo(tmp_path))
    assert len(issues) == 1 and "aren't documented" in issues[0].message


def test_readme_named_directory_does_not_crash(tmp_path):
    # MaximeRivest/mcp2py ships a README_files/ directory next to README.md.
    (tmp_path / "README_files").mkdir()
    write(tmp_path, "README.md", "# x\n\nTools: ping\n")
    write(tmp_path, "server.py", """
        from fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def ping() -> str:
            \"\"\"Ping.\"\"\"
            return "pong"
        """)
    report = analyze_repo(tmp_path)
    assert [t.name for t in report.tools] == ["ping"]


def test_broken_symlink_py_file_does_not_crash(tmp_path):
    # jiangyi01/SpatialOmicsLab has a .py symlink whose target isn't in the repo.
    (tmp_path / "gone.py").symlink_to(tmp_path / "missing" / "gone.py")
    write(tmp_path, "server.py", """
        from fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def ping() -> str:
            \"\"\"Ping.\"\"\"
            return "pong"
        """)
    report = analyze_repo(tmp_path)
    assert [t.name for t in report.tools] == ["ping"]


def test_registration_onto_passed_in_server_without_fastmcp_import(tmp_path):
    # Raudbjorn/MDMAI: session/mcp_tools.py gets the server as a parameter and
    # never imports FastMCP; main.py does.
    write(tmp_path, "main.py", """
        from fastmcp import FastMCP
        from mcp_tools import register_session_tools
        mcp = FastMCP("x")
        register_session_tools(mcp)
        """)
    write(tmp_path, "mcp_tools.py", """
        async def start_session(campaign_id: str) -> dict:
            \"\"\"Start a game session.\"\"\"
            return {}

        def register_session_tools(mcp_server):
            mcp_server.tool()(start_session)
        """)
    report = analyze_repo(tmp_path)
    assert [t.name for t in report.tools] == ["start_session"]


def test_langchain_tool_from_function_not_counted(tmp_path):
    # LangChain has Tool.from_function too; only FastMCP's Tool counts, even
    # in a repo (and file) that also uses FastMCP.
    write(tmp_path, "server.py", """
        from fastmcp import FastMCP
        from langchain_core.tools import Tool, StructuredTool
        mcp = FastMCP("x")

        def search(q: str) -> str:
            \"\"\"Search.\"\"\"
            return q

        lc_tool = Tool.from_function(func=search, name="search", description="Search.")
        lc_tool2 = StructuredTool.from_function(search)
        """)
    report = analyze_repo(tmp_path)
    assert report.tools == []


def test_identical_files_keep_the_same_copy_on_any_python(tmp_path, monkeypatch):
    # Bsh13lder/Lazy-Claw ships the same server twice; rglob order differs
    # between Python 3.12 and 3.14, which changed which copy was reported.
    # Force the unfavourable order: the result must not depend on it.
    real_rglob = Path.rglob
    monkeypatch.setattr(Path, "rglob", lambda self, pattern: iter(sorted(real_rglob(self, pattern), reverse=True)))
    src = """
        from fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def search_jobs(q: str) -> str:
            \"\"\"Search jobs.\"\"\"
            return q
        """
    for d in ("z_copy", "a_copy"):
        (tmp_path / d).mkdir()
        write(tmp_path, f"{d}/server.py", src)
    report = analyze_repo(tmp_path)
    assert [(t.name, t.file) for t in report.tools] == [("search_jobs", "a_copy/server.py")]


def test_delegation_result_does_not_depend_on_resolution_order(tmp_path):
    # The registry used to memoize recursively, caching a result cut short by
    # the depth limit (or by a call cycle's provisional False). Here c1 is
    # resolved first and reaches c6 at depth 5, where h is one hop past the
    # limit, so c6 was cached as unhandled even though it calls h directly.
    # The same flaw made oraios/serena's results change with the hash seed.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        def c1(x): return c2(x)
        def c2(x): return c3(x)
        def c3(x): return c4(x)
        def c4(x): return c5(x)
        def c5(x): return c6(x)
        def c6(x): return h(x)

        def h(x):
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
            return c6(x)
        """)
    report = analyze_repo(tmp_path)
    checks = {i.check for i in report.tools[0].issues}
    assert "error_handling" not in checks


def test_sdk_v2_tools_registered_in_a_loop_over_a_literal_tuple_are_found(tmp_path):
    # Verified against a real miss: tskovlund/mcp-score, 0 of 23 tools found.
    # Each module registers its tools with
    # `for tool in (a, b): server.tool()(tool)` on the SDK v2 MCPServer.
    write(tmp_path, "tools.py", """
        from mcp.server.mcpserver import MCPServer

        async def ping(host: str) -> str:
            \"\"\"Ping the app.

            Args:
                host: Host to ping.
            \"\"\"
            return host

        async def stop() -> str:
            \"\"\"Stop the app.\"\"\"
            return "ok"

        def register(server: MCPServer) -> None:
            for tool in (ping, stop):
                server.tool()(tool)
        """)
    names = sorted(t.name for t in analyze_repo(tmp_path).tools)
    assert names == ["ping", "stop"]


def test_loop_over_a_runtime_value_is_not_expanded(tmp_path):
    write(tmp_path, "tools.py", """
        from mcp.server.mcpserver import MCPServer

        def ping() -> str:
            \"\"\"Ping.\"\"\"
            return "ok"

        def register(server: MCPServer, extra) -> None:
            for tool in extra:
                server.tool()(tool)
        """)
    assert analyze_repo(tmp_path).tools == []


def test_context_alias_and_subscripted_context_are_not_tool_parameters(tmp_path):
    # mcp-score: `ScoreContext = Context[AppState, Any]`, injected by the SDK
    # and never shown to the model, was counted as an undocumented parameter.
    write(tmp_path, "context.py", """
        from typing import Any
        from mcp.server.mcpserver import Context
        ScoreContext = Context[dict, Any]
        """)
    write(tmp_path, "server.py", """
        from typing import Any
        from mcp.server.mcpserver import MCPServer, Context
        from context import ScoreContext
        mcp = MCPServer("x")

        @mcp.tool()
        def disconnect(context: ScoreContext) -> str:
            \"\"\"Disconnect from the running score application.\"\"\"
            try:
                return "ok"
            except OSError:
                return "failed"

        @mcp.tool()
        def connect(ctx: Context[dict, Any], host: str) -> str:
            \"\"\"Connect to a running score application.

            Args:
                host: Host to connect to.
            \"\"\"
            try:
                return host
            except OSError:
                return "failed"
        """)
    report = analyze_repo(tmp_path)
    assert {t.name: t.param_count for t in report.tools} == {"disconnect": 0, "connect": 1}
    assert all(not t.issues for t in report.tools)


def test_local_decorator_with_its_own_try_except_counts_as_error_handling(tmp_path):
    write(tmp_path, "server.py", """
        import functools
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        def score_tool(fn):
            @functools.wraps(fn)
            async def deliver(*args, **kwargs):
                try:
                    return await fn(*args, **kwargs)
                except ValueError as error:
                    raise RuntimeError(str(error)) from error
            return deliver

        @mcp.tool()
        @score_tool
        async def stop() -> str:
            \"\"\"Stop the app.\"\"\"
            return "ok"
        """)
    checks = {i.check for i in analyze_repo(tmp_path).tools[0].issues}
    assert "error_handling" not in checks


def test_test_prefixed_module_with_tools_and_no_tests_is_analyzed(tmp_path):
    # Verified against a real miss: oaslananka/kicad-mcp-pro keeps 4 tools in
    # src/kicad_mcp/tools/test_points.py (test points on a PCB), skipped as a
    # test file by name alone.
    write(tmp_path, "src/pkg/tools/test_points.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def pcb_list_test_points() -> list[str]:
            \"\"\"List the test points placed on the active board.\"\"\"
            try:
                return []
            except OSError:
                return []
        """)
    write(tmp_path, "src/pkg/test_server.py", """
        import pytest
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def fixture_tool() -> str:
            \"\"\"A tool defined only inside a test module.\"\"\"
            return "ok"

        def test_fixture_tool():
            assert fixture_tool() == "ok"
        """)
    names = [t.name for t in analyze_repo(tmp_path).tools]
    assert names == ["pcb_list_test_points"]


def test_test_named_harness_script_without_mcp_import_stays_auxiliary(tmp_path):
    # pal-mcp-server's communication_simulator_test.py drives the server with
    # subprocess but has no pytest functions; it must not be security-scanned
    # as server code.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")
        """)
    write(tmp_path, "communication_simulator_test.py", """
        import subprocess
        def main(cmd):
            subprocess.run(cmd, shell=True)
        """)
    report = analyze_repo(tmp_path)
    assert not any(i.check == "dangerous_exec" for i in report.repo_issues)


def test_none_default_on_non_optional_type_flagged(tmp_path):
    # Reproduced on barvhaim/qiskit-mcp-server: `name: str = None` advertises
    # {"type": "string", "default": null}, and an explicit `name: null` call
    # fails pydantic validation.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def create_circuit(num_qubits: int, num_bits: int = None, name: str = None) -> str:
            \"\"\"Create a circuit.\"\"\"
            try:
                return str(num_qubits)
            except ValueError as e:
                return str(e)
        """)
    tool = analyze_repo(tmp_path).tools[0]
    issue = next(i for i in tool.issues if i.check == "none_default_type")
    assert "num_bits" in issue.message and "name" in issue.message
    assert "num_qubits" not in issue.message


def test_none_default_field_and_annotated_forms_flagged(tmp_path):
    write(tmp_path, "server.py", """
        from typing import Annotated
        from pydantic import Field
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def search(
            query: Annotated[str, Field(description="Query.")] = None,
            tags: list[str] = Field(default=None, description="Tags."),
        ) -> str:
            \"\"\"Search things.\"\"\"
            try:
                return query or ""
            except ValueError as e:
                return str(e)
        """)
    tool = analyze_repo(tmp_path).tools[0]
    issue = next(i for i in tool.issues if i.check == "none_default_type")
    assert "query" in issue.message and "tags" in issue.message


def test_none_default_on_nullable_or_unknown_type_not_flagged(tmp_path):
    write(tmp_path, "server.py", """
        from typing import Any, Optional, Union
        from mcp.server.fastmcp import FastMCP
        from .types import MaybeName
        mcp = FastMCP("x")

        @mcp.tool()
        def create_circuit(
            a: str | None = None,
            b: Optional[int] = None,
            c: Union[int, None] = None,
            d: Any = None,
            e: MaybeName = None,
            f: str = "",
            g=None,
        ) -> str:
            \"\"\"Create a circuit.\"\"\"
            try:
                return ""
            except ValueError as e:
                return str(e)
        """)
    tool = analyze_repo(tmp_path).tools[0]
    assert not any(i.check == "none_default_type" for i in tool.issues)


def test_none_default_on_typing_generic_flagged(tmp_path):
    # Missed by v1.13.0: ahujasid/mcp-for-blender's describe_node_type has
    # `property_overrides: Dict[str, Any] = None`.
    write(tmp_path, "server.py", """
        import typing
        from typing import Any, Dict, List
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def describe(
            overrides: Dict[str, Any] = None,
            names: List[str] = None,
            extra: typing.Dict[str, int] = None,
            ok: typing.Optional[Dict[str, Any]] = None,
        ) -> str:
            \"\"\"Describe a node.\"\"\"
            try:
                return ""
            except ValueError as e:
                return str(e)
        """)
    tool = analyze_repo(tmp_path).tools[0]
    issue = next(i for i in tool.issues if i.check == "none_default_type")
    assert all(n in issue.message for n in ("overrides", "names", "extra"))
    assert "ok" not in issue.message.split(" defaults")[0].split(", ")


def test_partially_documented_params_name_the_missing_ones(tmp_path):
    # chigwell/telegram-mcp: an Args: section that covers page/page_size but
    # leaves out `account` was reported as "no Args: section" (bug #75).
    write(tmp_path, "server.py", """
        from typing import Optional
        from fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool
        def get_chats(account: Optional[str] = None, page: int = 1, page_size: int = 20) -> str:
            \"\"\"
            Get a paginated list of chats.
            Args:
                page: Page number (1-indexed).
                page_size: Number of chats per page.
            \"\"\"
            return ""
        """)
    issues = _param_docs_issues(analyze_repo(tmp_path))
    assert len(issues) == 1
    msg = issues[0].message
    assert msg.startswith("1 of 3 parameters aren't documented: account")
    assert "no Args:" not in msg


def test_fully_undocumented_params_keep_the_section_message(tmp_path):
    write(tmp_path, "server.py", """
        from fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool
        def get_chat(chat_id: int) -> str:
            \"\"\"Get one chat.\"\"\"
            return ""
        """)
    issues = _param_docs_issues(analyze_repo(tmp_path))
    assert len(issues) == 1 and "no Args:/:param: docstring section" in issues[0].message


def test_blank_lines_between_args_entries_keep_the_section_open(tmp_path):
    # Bug #76, from zinja-coder/jadx-mcp-server's search_classes: entries
    # separated by blank lines were reported as undocumented.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def search(search_term: str, package: str = "", count: int = 20) -> str:
            \"\"\"Search the code.

            Args:
                search_term: The keyword to search for.

                package (optional): Package name to limit the search scope.
                    - If empty string (default), searches all packages

                count: Max results.

            Returns:
                The matches.
            \"\"\"
            try:
                return search_term
            except ValueError as e:
                return str(e)
        """)
    tool = analyze_repo(tmp_path).tools[0]
    assert tool.has_docstring_params
    assert not any(i.check == "param_docs" for i in tool.issues)


def test_blank_line_then_dedent_still_ends_args_section(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def search(query: str, limit: int = 5) -> str:
            \"\"\"Search the code.

            Args:
                query: The query.

            limit: this line is prose after the section, not an entry.
            \"\"\"
            try:
                return query
            except ValueError as e:
                return str(e)
        """)
    tool = analyze_repo(tmp_path).tools[0]
    issue = next(i for i in tool.issues if i.check == "param_docs")
    assert "limit" in issue.message


def test_langchain_tool_runtime_param_is_injected_not_undocumented(tmp_path):
    # Bug #77, from MODSetter/SurfSense: LangChain injects `runtime: ToolRuntime`
    # and strips it from the schema, like FastMCP's Context.
    write(tmp_path, "server.py", """
        from langchain.tools import ToolRuntime
        from langchain_core.tools import tool

        @tool
        async def send_email(to: str, body: str, runtime: ToolRuntime) -> str:
            \"\"\"Send an email.

            Args:
                to: Recipient address.
                body: Email body content.
            \"\"\"
            try:
                return to
            except ValueError as e:
                return str(e)
        """)
    tool = analyze_repo(tmp_path).tools[0]
    assert tool.param_count == 2
    assert not any(i.check == "param_docs" for i in tool.issues)


def test_wrapper_decorator_returning_mcp_tool_counts(tmp_path):
    # Bug #78, from twelvedata/mcp (0 of 27 tools found): a repo's own
    # decorator factory that returns `mcp.tool(...)`, and a second one that
    # returns the first.
    write(tmp_path, "state.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        def td_tool(*, scope: str, **kwargs):
            return mcp.tool(meta={"scope": scope}, **kwargs)

        def _local_only_tool(**kwargs):
            if not LOCAL:
                return lambda fn: fn
            return td_tool(**kwargs)
        """)
    write(tmp_path, "tools.py", """
        from state import td_tool, _local_only_tool

        @td_tool(scope="market")
        def get_quote(symbol: str) -> str:
            \"\"\"Get the latest quote for a ticker symbol.\"\"\"
            return symbol

        @_local_only_tool(name="login")
        def oauth_login() -> str:
            \"\"\"Start a browser login to the data API.\"\"\"
            return "ok"

        @some_other_decorator(scope="market")
        def not_a_tool(x: str) -> str:
            return x
        """)
    names = sorted(t.name for t in analyze_repo(tmp_path).tools)
    assert names == ["get_quote", "login"]


def test_registry_decorator_registered_through_mcp_tool_counts(tmp_path):
    # Bug #78, from panther-labs/mcp-panther (0 of 36): `@mcp_tool` adds the
    # function to a module-level set; `register_all_tools` passes each one to
    # `mcp.tool(...)`.
    write(tmp_path, "registry.py", """
        _tool_registry = set()

        def mcp_tool(func=None, *, name=None, description=None, annotations=None):
            def decorator(func):
                _tool_registry.add(func)
                return func
            if func is None:
                return decorator
            return decorator(func)

        def register_all_tools(mcp_instance):
            for tool in _tool_registry:
                mcp_instance.tool(name=None)(tool)
        """)
    write(tmp_path, "alerts.py", """
        from registry import mcp_tool

        @mcp_tool
        async def get_alert(alert_id: str) -> dict:
            \"\"\"Get one alert by its ID.\"\"\"
            return {}

        @mcp_tool(annotations={"readOnlyHint": True})
        async def list_alerts() -> list:
            \"\"\"List open alerts, newest first.\"\"\"
            return []
        """)
    names = sorted(t.name for t in analyze_repo(tmp_path).tools)
    assert names == ["get_alert", "list_alerts"]


def test_registry_class_method_decorators_count(tmp_path):
    # Bug #82, from haris-musa/excel-mcp-server (0 of 37 found): methods of a
    # registry class return `self._register(...)`, which returns a nested
    # `decorate(fn)` that calls `self.server.tool(...)(fn)`.
    write(tmp_path, "registry.py", """
        class ToolRegistry:
            def __init__(self, server):
                self.server = server

            def reader(self, title):
                return self._register(title, True)

            def writer(self, title):
                return self._register(title, False)

            def _register(self, title, read_only):
                def decorate(function):
                    self.server.tool(title=title)(function)
                    return function
                return decorate

            def cached(self, ttl):
                def decorate(function):
                    return function
                return decorate
        """)
    write(tmp_path, "data_tools.py", """
        def register(tools, workspace):
            @tools.reader("Read range")
            def read_range(path: str) -> str:
                \"\"\"Read cell values from a range in a workbook.\"\"\"
                return path

            @tools.writer("Write range")
            def write_range(path: str, values: list) -> str:
                \"\"\"Write values into a range, replacing what is there.\"\"\"
                return path

            @tools.cached(60)
            def not_a_tool(path: str) -> str:
                return path
        """)
    names = sorted(t.name for t in analyze_repo(tmp_path).tools)
    assert names == ["read_range", "write_range"]


def test_collecting_decorator_without_tool_registration_is_not_a_tool(tmp_path):
    # Same collect-into-a-set shape, but nothing registers the set as tools.
    write(tmp_path, "plugins.py", """
        _hooks = []

        def on_tool_event(func):
            _hooks.append(func)
            return func

        @on_tool_event
        def log_it(event: str) -> None:
            \"\"\"Log a tool event.\"\"\"
        """)
    assert analyze_repo(tmp_path).tools == []


def test_method_of_runtime_plugin_object_resolves_when_unique(tmp_path):
    # Bug #79, from mcpcap/mcpcap (0 of 9): `self.mcp.tool(module.analyze_dns)`
    # in a loop over plugin objects; the method name is defined once in the repo.
    write(tmp_path, "modules/dns.py", """
        class DNSModule:
            def analyze_dns_packets(self, pcap_file: str) -> dict:
                \"\"\"Analyze DNS packets in a PCAP file.

                Args:
                    pcap_file: Path or URL of the capture.
                \"\"\"
                try:
                    return {}
                except OSError as e:
                    return {"error": str(e)}
        """)
    write(tmp_path, "server.py", """
        from fastmcp import FastMCP

        class Server:
            def __init__(self, modules):
                self.mcp = FastMCP("x")
                self.modules = modules

            def _register_tools(self):
                for name, module in self.modules.items():
                    if name == "dns":
                        self.mcp.tool(module.analyze_dns_packets)
        """)
    tools = analyze_repo(tmp_path).tools
    assert [t.name for t in tools] == ["analyze_dns_packets"]
    assert tools[0].param_count == 1
    assert tools[0].file == "modules/dns.py"


def test_method_of_runtime_object_not_guessed_when_ambiguous(tmp_path):
    write(tmp_path, "a.py", """
        class A:
            def run(self, x: str) -> str:
                \"\"\"Run A.\"\"\"
                return x

        class B:
            def run(self, x: str) -> str:
                \"\"\"Run B.\"\"\"
                return x
        """)
    write(tmp_path, "server.py", """
        from fastmcp import FastMCP
        mcp = FastMCP("x")

        def register(obj):
            mcp.tool(obj.run)
        """)
    assert analyze_repo(tmp_path).tools == []


def test_lowlevel_tool_name_from_handler_class(tmp_path):
    # Bug #80, from isdaniel/mcp_weather_server (0 of 8): the mcp-gsuite
    # ToolHandler pattern, `Tool(name=self.name, ...)` where each handler
    # passes its name up with `super().__init__("...")`.
    write(tmp_path, "tools.py", """
        from mcp.types import Tool

        class ToolHandler:
            def __init__(self, tool_name: str):
                self.name = tool_name

        class GetCurrentWeatherToolHandler(ToolHandler):
            def __init__(self):
                super().__init__("get_current_weather")

            def get_tool_description(self) -> Tool:
                return Tool(
                    name=self.name,
                    description="Get the current weather for a city.",
                    inputSchema={"type": "object", "properties": {
                        "city": {"type": "string", "description": "City name."},
                    }},
                )

        class Ping:
            name = "ping"

            def describe(self) -> Tool:
                return Tool(name=self.name, description="Check the server is up.", inputSchema={})
        """)
    names = sorted(t.name for t in analyze_repo(tmp_path).tools)
    assert names == ["get_current_weather", "ping"]


def test_lowlevel_tool_self_name_set_at_runtime_is_skipped(tmp_path):
    write(tmp_path, "tools.py", """
        from mcp.types import Tool

        class Handler:
            def __init__(self, name: str):
                self.name = name

            def describe(self) -> Tool:
                return Tool(name=self.name, description="A tool built at runtime.", inputSchema={})
        """)
    assert analyze_repo(tmp_path).tools == []


def test_tool_from_function_on_local_function_tool_subclass(tmp_path):
    # Bug #81, from keboola/mcp-server (0 of 44): every tool is built with a
    # local FunctionTool subclass, sometimes imported under the base's name.
    write(tmp_path, "base.py", """
        from fastmcp.tools import FunctionTool

        class _Serializing(FunctionTool):
            pass

        class PlainFunctionTool(_Serializing):
            pass

        class ToonFunctionTool(_Serializing):
            pass
        """)
    write(tmp_path, "storage.py", """
        from base import ToonFunctionTool
        from base import PlainFunctionTool as FunctionTool

        def get_buckets() -> list:
            \"\"\"List the storage buckets in the project.\"\"\"
            return []

        def get_tables(bucket_id: str) -> list:
            \"\"\"List the tables in one bucket.\"\"\"
            return []

        def add_storage_tools(mcp):
            mcp.add_tool(ToonFunctionTool.from_function(get_buckets))
            mcp.add_tool(FunctionTool.from_function(get_tables))
        """)
    names = sorted(t.name for t in analyze_repo(tmp_path).tools)
    assert names == ["get_buckets", "get_tables"]


def test_lowlevel_tool_description_from_class_attribute(tmp_path):
    # Bug #80 follow-on, from isdaniel/pgtuner_mcp: `description=self.description`
    # read as "no description" once the tool was found.
    write(tmp_path, "tools.py", """
        from mcp.types import Tool

        class AnalyzeBufferCache:
            name = "analyze_buffer_cache"
            description = (
                "Analyze PostgreSQL buffer cache usage "
                "and hit ratios per table."
            )

            def get_tool_definition(self) -> Tool:
                return Tool(name=self.name, description=self.description, inputSchema={})
        """)
    tool = analyze_repo(tmp_path).tools[0]
    assert tool.name == "analyze_buffer_cache"
    assert tool.has_description
    assert not any(i.check == "description" for i in tool.issues)


def test_getattr_loop_over_imported_method_name_tuple_counts(tmp_path):
    # Bug #86, from Evil0ctal/Douyin_TikTok_Download_API (0 of 8 found):
    # `for method in TOOL_METHODS: server.add_tool(getattr(tools, method), name=method)`
    # with `tools = ToolSet(context)` and TOOL_METHODS a tuple imported from
    # the module that defines ToolSet.
    write(tmp_path, "dtk/tools.py", """
        from typing import Final

        class ToolSet:
            async def get_video(self, video_id: str) -> dict:
                \"\"\"Get one video's metadata by its id.\"\"\"
                return {}

            async def pool_status(self) -> dict:
                \"\"\"Report why the identity pool is degraded, if it is.\"\"\"
                return {}

            def _helper(self) -> None:
                pass

        TOOL_METHODS: Final[tuple[str, ...]] = ("get_video", "pool_status")
        OTHER_NAMES = ("get_video", "not_a_method")
        """)
    write(tmp_path, "dtk/server.py", """
        from mcp.server import MCPServer
        from dtk.tools import TOOL_METHODS, OTHER_NAMES, ToolSet

        def build_server(context):
            server = MCPServer(name="dtk")
            tools = ToolSet(context)
            for method in TOOL_METHODS:
                server.add_tool(getattr(tools, method), name=method)
            for method in OTHER_NAMES:
                server.add_tool(getattr(tools, method), name=method)
            return server
        """)
    names = sorted(t.name for t in analyze_repo(tmp_path).tools)
    # OTHER_NAMES has a name ToolSet doesn't define, so none of it counts.
    assert names == ["get_video", "pool_status"]


def test_registry_dict_decorator_read_by_list_tools_counts(tmp_path):
    # Bug #88, from chunkhound/chunkhound (0 of 5 found): `@register_tool(
    # description=SEARCH_DESCRIPTION, name="search")` stores
    # `TOOL_REGISTRY[name] = Tool(...)`, which a low-level list_tools handler
    # reads with `TOOL_REGISTRY.items()`. The description is a module constant.
    write(tmp_path, "mcp_server/tools.py", """
        TOOL_REGISTRY: dict = {}
        SEARCH_DESCRIPTION = \"\"\"Find code chunks that match a regex or a semantic query.\"\"\"

        def register_tool(description: str, name: str | None = None):
            def decorator(func):
                TOOL_REGISTRY[name or func.__name__] = Tool(name=name, description=description, implementation=func)
                return func
            return decorator

        def cache_result(ttl: int):
            def decorator(func):
                _CACHE[func.__name__] = ttl
                return func
            return decorator

        @register_tool(description=SEARCH_DESCRIPTION, name="search")
        async def search_impl(query: str) -> dict:
            return {}

        @cache_result(60)
        async def not_a_tool(x: str) -> str:
            return x
        """)
    write(tmp_path, "mcp_server/base.py", """
        from .tools import TOOL_REGISTRY

        def list_tools():
            return [t for _, t in TOOL_REGISTRY.items()]
        """)
    tools = analyze_repo(tmp_path).tools
    assert [t.name for t in tools] == ["search"]
    assert tools[0].description_text.startswith("Find code chunks")


def test_plain_function_writing_a_read_dict_is_not_a_tool_decorator(tmp_path):
    # Found by the before/after for bug #88: MikeRecognex/mcp-codebase-index's
    # low-level `async def call_tool(name, arguments)` writes a module dict that
    # is read elsewhere; it is not a decorator factory, so `@server.call_tool()`
    # must stay the SDK's dispatcher, not a tool.
    write(tmp_path, "server.py", """
        from mcp.server import Server
        server = Server("x")
        _STATS = {}

        def report():
            return list(_STATS.items())

        @server.call_tool()
        async def call_tool(name: str, arguments: dict) -> list:
            _STATS[name] = 1
            return []
        """)
    assert [t.name for t in analyze_repo(tmp_path).tools] == []


def test_import_bindings_parsed_once_per_file(tmp_path, monkeypatch):
    # Bug #91: v1.15.4's getattr-loop support (#86) re-parsed a file's imports
    # for every `for x in NAME` loop, ~12x slower on amingclawdev/aming-claw
    # (691 files, ~1,900 loops: 29s on v1.15.3, ~355s on v1.15.4).
    import mcp_doctor.analyzer as analyzer
    loops = "\n".join(f"for x{i} in NAMES:\n    pass" for i in range(50))
    write(tmp_path, "server.py", "from mcp.server import MCPServer\nfrom names import NAMES\n" + loops + "\n")
    write(tmp_path, "names.py", "NAMES = ('a', 'b')\n")
    calls = []
    real = analyzer._import_bindings
    monkeypatch.setattr(analyzer, "_import_bindings", lambda tree: calls.append(1) or real(tree))
    analyze_repo(tmp_path)
    assert len(calls) <= 5  # a handful of other callers, not one per loop


def test_none_default_on_runtime_typed_factory_closure_not_flagged(tmp_path):
    # cuga-project/cuga-agent adapter.py: one generic handler per OpenAPI
    # operation, typed by a model built at runtime and registered in a loop.
    # The caller fills `headers` itself; the model never sees it, so the
    # static schema read doesn't describe what clients get (census FP,
    # 2026-10-09).
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP

        def create_handler(api, model):
            def handler(params: model, headers: dict = None):
                \"\"\"Call the API.\"\"\"
                try:
                    return str(params)
                except ValueError as e:
                    return str(e)
            return handler

        def build(apis):
            mcp = FastMCP("x")
            for api in apis:
                handler = create_handler(api, api.model)
                mcp.tool(name=api.name, description=api.description)(handler)
            return mcp
        """)
    report = analyze_repo(tmp_path)
    assert report.tools
    assert not any(i.check == "none_default_type" for t in report.tools for i in t.issues)


def test_none_default_on_plain_nested_closure_still_flagged(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP

        def register(mcp: FastMCP):
            @mcp.tool()
            def search(query: str, limit: int = None) -> str:
                \"\"\"Search.\"\"\"
                try:
                    return query
                except ValueError as e:
                    return str(e)
        """)
    report = analyze_repo(tmp_path)
    tool = next(t for t in report.tools if t.name == "search")
    assert any(i.check == "none_default_type" for i in tool.issues)
