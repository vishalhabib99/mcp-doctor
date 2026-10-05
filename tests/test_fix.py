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


def test_skips_partially_documented_params(tmp_path):
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

    report = analyze_repo(tmp_path)
    original = (tmp_path / "server.py").read_text()
    changed = apply_fixes(tmp_path, report)

    assert changed == []
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
