from pathlib import Path
from textwrap import dedent

from mcp_doctor.analyzer import analyze_repo
from mcp_doctor.fix import apply_fixes


def write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(dedent(content))
    return p


def test_fixes_bare_except(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def run() -> str:
            \"\"\"Run the thing.\"\"\"
            try:
                return "ok"
            except:
                return "fail"
        """)

    report = analyze_repo(tmp_path)
    changed = apply_fixes(tmp_path, report)
    assert changed == ["server.py"]

    fixed_source = (tmp_path / "server.py").read_text()
    assert "except Exception:" in fixed_source
    assert "except:" not in fixed_source

    after = analyze_repo(tmp_path)
    assert after.tools[0].has_bare_except is False


def test_stubs_args_for_single_line_docstring(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def get_forecast(city: str, days: int) -> str:
            \"\"\"Get a weather forecast.\"\"\"
            return f"{city} {days}"
        """)

    report = analyze_repo(tmp_path)
    assert report.tools[0].has_docstring_params is False

    changed = apply_fixes(tmp_path, report)
    assert changed == ["server.py"]

    fixed_source = (tmp_path / "server.py").read_text()
    assert "Args:" in fixed_source
    assert "city: TODO: describe this parameter." in fixed_source
    assert "days: TODO: describe this parameter." in fixed_source
    assert "Get a weather forecast." in fixed_source  # summary preserved

    after = analyze_repo(tmp_path)
    assert after.tools[0].has_docstring_params is True


def test_stubs_args_for_multiline_docstring(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def get_forecast(city: str) -> str:
            \"\"\"Get a weather forecast.

            More detail on the second line.
            \"\"\"
            return city
        """)

    report = analyze_repo(tmp_path)
    changed = apply_fixes(tmp_path, report)
    assert changed == ["server.py"]

    fixed_source = (tmp_path / "server.py").read_text()
    assert "More detail on the second line." in fixed_source
    assert "Args:" in fixed_source
    assert "city: TODO: describe this parameter." in fixed_source

    after = analyze_repo(tmp_path)
    assert after.tools[0].has_docstring_params is True
    # source stays valid Python after the fix
    compile(fixed_source, "server.py", "exec")


def test_does_not_fabricate_a_description(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def run(x: str) -> str:
            return x
        """)

    report = analyze_repo(tmp_path)
    original = (tmp_path / "server.py").read_text()
    changed = apply_fixes(tmp_path, report)

    assert changed == []
    assert (tmp_path / "server.py").read_text() == original


def test_partial_args_section_ending_at_closing_quote(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def run(city: str, days: int) -> str:
            \"\"\"Run the thing.

            Args:
                city: The city name.
            \"\"\"
            return f"{city} {days}"
        """)

    changed = apply_fixes(tmp_path, analyze_repo(tmp_path))
    assert changed == ["server.py"]
    assert (
        "        city: The city name.\n"
        "        days: TODO: describe this parameter.\n"
        '    \"\"\"\n'
    ) in (tmp_path / "server.py").read_text()


def test_args_entry_that_closes_docstring_left_alone(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def run(city: str, days: int) -> str:
            \"\"\"Run the thing.

            Args:
                city: The city name.\"\"\"
            return f"{city} {days}"
        """)

    original = (tmp_path / "server.py").read_text()
    assert apply_fixes(tmp_path, analyze_repo(tmp_path)) == []
    assert (tmp_path / "server.py").read_text() == original


def test_fix_flag_improves_score_via_cli():
    import subprocess
    import sys
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        write(tmp_path, "server.py", """
            from mcp.server.fastmcp import FastMCP
            mcp = FastMCP("x")

            @mcp.tool()
            def run(city: str) -> str:
                \"\"\"Run the thing.\"\"\"
                try:
                    return city
                except:
                    return ""
            """)

        before = subprocess.run(
            [sys.executable, "-m", "mcp_doctor.cli", str(tmp_path), "--json"],
            capture_output=True, text=True,
        )
        after = subprocess.run(
            [sys.executable, "-m", "mcp_doctor.cli", str(tmp_path), "--fix", "--json"],
            capture_output=True, text=True,
        )

        import json
        before_pct = json.loads(before.stdout)["percent"]
        after_pct = json.loads(after.stdout)["percent"]
        assert after_pct > before_pct
        assert "Fixed 1 file(s)" in after.stderr


def test_no_args_stub_when_numpy_or_sphinx_docs_exist(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def get_forecast(city: str) -> str:
            \"\"\"Get a weather forecast.

            Parameters
            ----------
            city : str
                The city name.
            \"\"\"
            return city

        @mcp.tool()
        def get_alerts(region: str) -> str:
            \"\"\"Get weather alerts.

            :param region: the region code
            \"\"\"
            return region
        """)

    report = analyze_repo(tmp_path)
    apply_fixes(tmp_path, report)
    assert "TODO: describe this parameter" not in (tmp_path / "server.py").read_text()


def test_none_default_retyped_as_nullable(tmp_path):
    # The telegram-mcp / davinci-resolve-mcp shape: `str = None` advertises
    # {"type": "string", "default": null}, so an explicit null fails validation.
    write(tmp_path, "server.py", """
        from typing import Annotated, Any, Dict
        from pydantic import Field
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def search(
            query: Annotated[str, Field(description="Query.")] = None,
            tags: list[str] = Field(default=None, description="Tags."),
            overrides: Dict[str, Any] = None,
            limit: int = 10,
            ok: str | None = None,
        ) -> str:
            \"\"\"Search things.\"\"\"
            try:
                return query or ""
            except ValueError as e:
                return str(e)
        """)

    changed = apply_fixes(tmp_path, analyze_repo(tmp_path))
    assert changed == ["server.py"]

    fixed = (tmp_path / "server.py").read_text()
    assert 'query: Annotated[str | None, Field(description="Query.")] = None' in fixed
    assert "tags: list[str] | None = Field(default=None" in fixed
    assert "overrides: Dict[str, Any] | None = None" in fixed
    assert "limit: int = 10" in fixed
    assert "ok: str | None = None" in fixed

    after = analyze_repo(tmp_path).tools[0]
    assert not any(i.check == "none_default_type" for i in after.issues)


def test_none_default_matches_file_optional_style(tmp_path):
    write(tmp_path, "server.py", """
        from typing import Optional
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def run(name: str = None, other: Optional[int] = None) -> str:
            \"\"\"Run the thing.

            Args:
                name: Name.
                other: Other.
            \"\"\"
            try:
                return name or ""
            except ValueError as e:
                return str(e)
        """)

    apply_fixes(tmp_path, analyze_repo(tmp_path))
    fixed = (tmp_path / "server.py").read_text()
    assert "name: Optional[str] = None" in fixed
    assert "other: Optional[int] = None" in fixed


def test_none_default_fix_leaves_non_tool_functions_alone(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        def helper(name: str = None) -> str:
            return name or ""

        @mcp.tool()
        def run(city: str) -> str:
            \"\"\"Run the thing.

            Args:
                city: City name.
            \"\"\"
            try:
                return helper(city)
            except ValueError as e:
                return str(e)
        """)

    original = (tmp_path / "server.py").read_text()
    assert apply_fixes(tmp_path, analyze_repo(tmp_path)) == []
    assert (tmp_path / "server.py").read_text() == original


def test_none_default_fix_handles_non_ascii_before_annotation(tmp_path):
    # AST col_offset is in UTF-8 bytes; a non-ASCII character earlier on the
    # line would shift a naive character-based splice.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def run(café: str = None) -> str:
            \"\"\"Run the thing.

            Args:
                café: Café name.
            \"\"\"
            try:
                return café or ""
            except ValueError as e:
                return str(e)
        """)

    apply_fixes(tmp_path, analyze_repo(tmp_path))
    assert "def run(café: str | None = None) -> str:" in (tmp_path / "server.py").read_text()


def test_partial_args_section_gets_missing_entries(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import Context, FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def run(city: str, days: int, units: str, ctx: Context) -> str:
            \"\"\"Get the forecast.

            Args:
                city: City name, e.g. "Paris".
                    Continued on a second line.

            Returns:
                The forecast.
            \"\"\"
            try:
                return city
            except ValueError as e:
                return str(e)
        """)

    changed = apply_fixes(tmp_path, analyze_repo(tmp_path))
    assert changed == ["server.py"]

    fixed = (tmp_path / "server.py").read_text()
    assert (
        "            Continued on a second line.\n"
        "        days: TODO: describe this parameter.\n"
        "        units: TODO: describe this parameter.\n"
        "\n"
        "    Returns:"
    ) in fixed
    assert "ctx: TODO" not in fixed  # injected by FastMCP, never in the schema

    after = analyze_repo(tmp_path).tools[0]
    assert not any(i.check == "param_docs" for i in after.issues)


def test_partial_sphinx_docs_left_alone(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def run(city: str, days: int) -> str:
            \"\"\"Get the forecast.

            :param city: City name.
            \"\"\"
            try:
                return city
            except ValueError as e:
                return str(e)
        """)

    original = (tmp_path / "server.py").read_text()
    assert apply_fixes(tmp_path, analyze_repo(tmp_path)) == []
    assert (tmp_path / "server.py").read_text() == original


def test_diff_flag_prints_patch_and_changes_nothing(tmp_path):
    import subprocess
    import sys

    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def run(name: str = None) -> str:
            \"\"\"Run the thing.

            Args:
                name: Name.
            \"\"\"
            try:
                return name or ""
            except ValueError as e:
                return str(e)
        """)
    original = (tmp_path / "server.py").read_text()

    result = subprocess.run(
        [sys.executable, "-m", "mcp_doctor.cli", str(tmp_path), "--diff"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "--- a/server.py" in result.stdout
    assert "+def run(name: str | None = None) -> str:" in result.stdout
    assert "1 file(s) would change." in result.stderr
    assert (tmp_path / "server.py").read_text() == original


def test_partial_args_section_with_blank_lines_appends_after_last_entry(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def run(city: str, units: str, days: int) -> str:
            \"\"\"Get the forecast.

            Args:
                city: City name.

                units: "metric" or "imperial".

            Returns:
                The forecast.
            \"\"\"
            try:
                return city
            except ValueError as e:
                return str(e)
        """)

    apply_fixes(tmp_path, analyze_repo(tmp_path))
    assert (
        '        units: "metric" or "imperial".\n'
        "        days: TODO: describe this parameter.\n"
        "\n"
        "    Returns:"
    ) in (tmp_path / "server.py").read_text()


def test_param_mentioned_in_unparsed_form_is_not_duplicated(tmp_path):
    # sandraschi/inkscape-mcp documents some params as "color_from / color_to: ...",
    # which the analyzer can't parse; appending a stub would duplicate them.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def animate(target_id: str, color_from: str = "", color_to: str = "") -> str:
            \"\"\"Animate an element.

            Args:
                target_id: Element to animate.
                color_from / color_to: animate_color endpoints.
            \"\"\"
            try:
                return target_id
            except ValueError as e:
                return str(e)
        """)

    original = (tmp_path / "server.py").read_text()
    assert apply_fixes(tmp_path, analyze_repo(tmp_path)) == []
    assert (tmp_path / "server.py").read_text() == original
