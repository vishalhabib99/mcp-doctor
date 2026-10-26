"""The report leads with findings that will break an agent (null defaults,
stdout on stdio, mcp 2.x start-up), before the grades."""

import json
import sys
from pathlib import Path

import yaml

from mcp_doctor.analyzer import analyze_repo
from mcp_doctor.breakages import CHECKED, KINDS, find
from mcp_doctor.report import render_json, render_text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "leaderboard"))

from scan_request import BREAKAGES_CHECKED, render_comment, summarize  # noqa: E402

SERVER = '''from mcp.server.fastmcp import FastMCP
mcp = FastMCP("x")

@mcp.tool()
def search(query: str, limit: int = None) -> str:
    """Search notes.

    Args:
        query: text to find.
        limit: max results.
    """
    print("searching")
    return query

if __name__ == "__main__":
    mcp.run()
'''


def _broken(tmp_path: Path, requirements: str = "mcp>=1.6\n") -> Path:
    (tmp_path / "server.py").write_text(SERVER)
    (tmp_path / "requirements.txt").write_text(requirements)
    return tmp_path


def test_finds_each_kind_with_location(tmp_path):
    found = find(analyze_repo(_broken(tmp_path)))
    by_check = {b.check: b for b in found}
    assert set(by_check) == {"none_default_type", "stdout_write", "sdk_v2"}
    assert by_check["none_default_type"].tool == "search(limit)"
    assert by_check["stdout_write"].tool == "search"
    assert by_check["stdout_write"].line == 12
    assert (by_check["sdk_v2"].file, by_check["sdk_v2"].line) == ("server.py", 1)


def test_capped_mcp_is_not_a_breakage(tmp_path):
    found = find(analyze_repo(_broken(tmp_path, "mcp>=1.6,<2\n")))
    assert "sdk_v2" not in {b.check for b in found}


def test_text_leads_with_breakages_before_grades(tmp_path):
    text = render_text(analyze_repo(_broken(tmp_path)), use_color=False)
    lines = text.splitlines()
    assert lines[1] == "Will break an agent: 3"
    assert text.index("Will break an agent") < text.index("Quality:")


def test_zero_says_what_was_checked():
    text = render_text(analyze_repo(ROOT / "examples" / "good_server"), use_color=False)
    assert text.splitlines()[1] == f"No known agent-breaking issues (checked: {CHECKED})"


def test_json_adds_keys_and_keeps_the_rest(tmp_path):
    payload = json.loads(render_json(analyze_repo(_broken(tmp_path))))
    assert payload["breakage_count"] == 3
    assert {b["check"] for b in payload["breakages"]} == set(KINDS)
    assert set(payload["breakages"][0]) == {"check", "tool", "file", "line", "breaks", "fix", "evidence", "detail"}
    for key in ("percent", "grade", "graded", "security_grade", "tools", "repo_issues"):
        assert key in payload


def test_scan_bot_reply_leads_with_breakages(tmp_path):
    report = json.loads(render_json(analyze_repo(_broken(tmp_path))))
    req = {"owner": "o", "repo": "r", "subdir": None, "leaderboard": False}
    body = render_comment(req, summarize(report))
    assert body.index("**Will break an agent: 3**") < body.index("| | Grade | Score |")
    assert "`T | None`" in body  # list items aren't table cells: no \| escaping


def test_scan_bot_checked_list_matches_package():
    assert BREAKAGES_CHECKED == CHECKED


def test_action_exposes_breakages_and_fail_on():
    action = yaml.safe_load((ROOT / "action.yml").read_text())
    assert "breakages" in action["outputs"]
    assert "breakages" in action["inputs"]["fail-on"]["description"]
    steps = {s.get("name"): s for s in action["runs"]["steps"]}
    assert steps["Enforce breakage gate"]["if"] == "${{ steps.audit.outputs.breakages_failed == '1' }}"
    assert "breakages_failed=1" in steps["Run audit"]["run"]


def test_text_caps_the_list(tmp_path):
    params = ", ".join(f"p{i}: int = None" for i in range(1))
    body = "from mcp.server.fastmcp import FastMCP\nmcp = FastMCP('x')\n"
    for i in range(12):
        body += f"\n@mcp.tool()\ndef t{i}({params}) -> str:\n    \"\"\"Tool {i} does a thing.\n\n    Args:\n        p0: a number.\n    \"\"\"\n    return ''\n"
    (tmp_path / "server.py").write_text(body)
    text = render_text(analyze_repo(tmp_path), use_color=False)
    assert text.splitlines()[1] == "Will break an agent: 12"
    assert "...and 2 more (all listed below and in --json)." in text


def test_archived_code_does_not_lead(tmp_path):
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "server.py").write_text(SERVER)
    (tmp_path / "requirements.txt").write_text("mcp>=1.6,<2\n")
    report = analyze_repo(tmp_path)
    assert any(i.check == "none_default_type" for t in report.tools for i in t.issues)
    assert find(report) == []


def test_disabled_tools_are_not_tools(tmp_path):
    (tmp_path / "compat.py").write_text('DISABLED = {} if V3 else {"enabled": False}\n')
    (tmp_path / "server.py").write_text(
        "from fastmcp import FastMCP\nfrom compat import DISABLED\nmcp = FastMCP('x')\n\n"
        "@mcp.tool(**DISABLED)\ndef hidden(a: str = None) -> str:\n    \"\"\"Hidden tool.\"\"\"\n    return ''\n\n"
        "@mcp.tool(enabled=False)\ndef off(a: str = None) -> str:\n    \"\"\"Off tool.\"\"\"\n    return ''\n\n"
        "@mcp.tool()\ndef shown(a: str) -> str:\n    \"\"\"Shown tool.\"\"\"\n    return a\n\nmcp.run()\n"
    )
    assert [t.name for t in analyze_repo(tmp_path).tools] == ["shown"]


def test_stdout_is_fine_in_a_file_served_over_sse(tmp_path):
    (tmp_path / "other.py").write_text("loop = object()\nloop.run()\n")  # a GLib loop, not FastMCP
    (tmp_path / "server.py").write_text(
        "from mcp.server.fastmcp import FastMCP\nmcp = FastMCP('x')\n\n"
        "@mcp.tool()\ndef t(a: str) -> str:\n    \"\"\"Tool.\n\n    Args:\n        a: text.\n    \"\"\"\n    print(a)\n    return a\n\n"
        "mcp.run(transport=\"sse\")\n"
    )
    report = analyze_repo(tmp_path)
    assert not any(i.check == "stdout_write" for t in report.tools for i in t.issues)


def test_requirement_spec_is_one_line(tmp_path):
    (tmp_path / "server.py").write_text(SERVER)
    (tmp_path / "requirements.txt").write_text("mcp>=1.8.1\r\npydantic>=2.11.4\r\nfoo<2\r\n")
    found = [b for b in find(analyze_repo(tmp_path)) if b.check == "sdk_v2"]
    assert len(found) == 1  # foo<2 on a later line doesn't cap mcp
    assert "'mcp>=1.8.1'" in found[0].detail and "pydantic" not in found[0].detail


def test_bare_run_in_the_same_file_keeps_stdout_check(tmp_path):
    (tmp_path / "server.py").write_text(
        "import sys\nfrom mcp.server.fastmcp import FastMCP\nmcp = FastMCP('x')\n\n"
        "@mcp.tool()\ndef t(a: str) -> str:\n    \"\"\"Tool.\n\n    Args:\n        a: text.\n    \"\"\"\n    print(a)\n    return a\n\n"
        "if '--http' in sys.argv:\n    mcp.run(transport=\"sse\")\nelse:\n    mcp.run()\n"
    )
    report = analyze_repo(tmp_path)
    assert any(i.check == "stdout_write" for t in report.tools for i in t.issues)


def test_one_function_on_two_servers_is_one_breakage(tmp_path):
    (tmp_path / "tools.py").write_text("async def quote(symbol: str = None) -> str:\n    \"\"\"Quote.\n\n    Args:\n        symbol: ticker.\n    \"\"\"\n    return ''\n")
    (tmp_path / "server.py").write_text(
        "from mcp.server.fastmcp import FastMCP\nfrom tools import quote\n"
        "a = FastMCP('a')\nb = FastMCP('b')\na.tool()(quote)\nb.tool()(quote)\na.run()\n"
    )
    report = analyze_repo(tmp_path)
    assert len([i for t in report.tools for i in t.issues if i.check == "none_default_type"]) == 2
    assert len(find(report)) == 1


def test_import_guard_that_only_raises_is_not_a_fallback(tmp_path):
    (tmp_path / "server.py").write_text(
        "def build():\n    try:\n        from mcp.server.fastmcp import FastMCP\n"
        "    except ImportError as e:\n        raise RuntimeError('install mcp') from e\n    return FastMCP('x')\n"
    )
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = ["mcp>=1.27.0"]\n')
    assert [b.check for b in find(analyze_repo(tmp_path))] == ["sdk_v2"]


def test_import_guard_with_a_fallback_is_still_a_shim(tmp_path):
    (tmp_path / "server.py").write_text(
        "try:\n    from mcp.server.fastmcp import FastMCP\nexcept ImportError:\n"
        "    from mcp.server.mcpserver import MCPServer as FastMCP\n"
    )
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = ["mcp>=1.27.0"]\n')
    assert find(analyze_repo(tmp_path)) == []


def test_ts_stdout_write_survives_in_a_pure_typescript_repo(tmp_path):
    # Bug #135: the analyzer's Python-only stdio gate dropped every TypeScript
    # stdout_write finding when a repo had no Python files at all.
    (tmp_path / "package.json").write_text('{"name":"x","dependencies":{"@modelcontextprotocol/sdk":"^1.20.0"}}')
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.ts").write_text(
        'import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";\n'
        'import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";\n'
        'import { z } from "zod";\n'
        'const server = new McpServer({ name: "x", version: "1.0.0" });\n'
        'server.tool("ping", "Check the server is alive.", { msg: z.string().describe("m") }, async ({ msg }) => {\n'
        '  console.log("got", msg);\n'
        '  return { content: [{ type: "text", text: msg }] };\n'
        '});\n'
        'await server.connect(new StdioServerTransport());\n'
    )
    report = analyze_repo(tmp_path)
    assert [(b.check, b.tool, b.line) for b in find(report)] == [("stdout_write", "ping", 6)]


def test_ts_stdout_write_is_judged_per_package(tmp_path):
    # inkeep/agents, preset-io/agor: one package's stdio server must not make
    # another package's HTTP route's console.log a stdio breakage.
    stdio = (
        'import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";\n'
        'import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";\n'
        'import { z } from "zod";\n'
        'const server = new McpServer({ name: "a", version: "1.0.0" });\n'
        'server.tool("quiet", "Check the server is alive.", { msg: z.string().describe("m") }, async ({ msg }) => {\n'
        '  return { content: [{ type: "text", text: msg }] };\n'
        '});\n'
        'await server.connect(new StdioServerTransport());\n'
    )
    http = (
        'import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";\n'
        'import { z } from "zod";\n'
        'const server = new McpServer({ name: "b", version: "1.0.0" });\n'
        'server.tool("loud", "Log and echo.", { msg: z.string().describe("m") }, async ({ msg }) => {\n'
        '  console.log("got", msg);\n'
        '  return { content: [{ type: "text", text: msg }] };\n'
        '});\n'
        'export default server;\n'
    )
    for pkg, src in (("packages/cli", stdio), ("packages/web", http)):
        (tmp_path / pkg / "src").mkdir(parents=True)
        (tmp_path / pkg / "package.json").write_text('{"name":"%s"}' % pkg.split("/")[1])
        (tmp_path / pkg / "src" / "index.ts").write_text(src)
    assert [b for b in find(analyze_repo(tmp_path)) if b.check == "stdout_write"] == []
    # The same console.log inside the stdio package is a breakage.
    (tmp_path / "packages/cli/src/index.ts").write_text(stdio.replace(
        'async ({ msg }) => {\n', 'async ({ msg }) => {\n  console.log("got", msg);\n'))
    assert [(b.check, b.tool) for b in find(analyze_repo(tmp_path))] == [("stdout_write", "quiet")]
