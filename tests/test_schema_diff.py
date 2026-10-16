from pathlib import Path
from textwrap import dedent

from mcp_doctor.analyzer import analyze_repo
from mcp_doctor.schema_diff import compute_schema_diff


def write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(dedent(content))
    return p


def _baseline(tools: list[dict]) -> dict:
    return {"tools": tools}


def test_removed_tool_is_flagged(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def still_here(x: str) -> str:
            \"\"\"Still here.

            Args:
                x: a value.
            \"\"\"
            return x
        """)
    report = analyze_repo(tmp_path)
    baseline = _baseline([
        {"name": "still_here", "param_names": ["x"], "required_param_names": ["x"]},
        {"name": "gone_now", "param_names": ["y"], "required_param_names": ["y"]},
    ])

    changes = compute_schema_diff(baseline, report.tools)
    assert [c.change for c in changes] == ["tool_removed"]
    assert changes[0].tool == "gone_now"
    assert changes[0].severity == "error"


def test_removed_parameter_is_flagged(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def get_item(item_id: str) -> str:
            \"\"\"Gets an item.

            Args:
                item_id: the item id.
            \"\"\"
            return item_id
        """)
    report = analyze_repo(tmp_path)
    baseline = _baseline([
        {"name": "get_item", "param_names": ["item_id", "region"], "required_param_names": ["item_id", "region"]},
    ])

    changes = compute_schema_diff(baseline, report.tools)
    assert len(changes) == 1
    assert changes[0].change == "param_removed"
    assert "region" in changes[0].detail


def test_newly_required_parameter_is_flagged(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def get_item(item_id: str, region: str) -> str:
            \"\"\"Gets an item.

            Args:
                item_id: the item id.
                region: the region.
            \"\"\"
            return item_id
        """)
    report = analyze_repo(tmp_path)
    # region existed in the baseline but was optional there.
    baseline = _baseline([
        {"name": "get_item", "param_names": ["item_id", "region"], "required_param_names": ["item_id"]},
    ])

    changes = compute_schema_diff(baseline, report.tools)
    assert len(changes) == 1
    assert changes[0].change == "param_newly_required"
    assert "region" in changes[0].detail


def test_new_optional_parameter_is_not_flagged(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def get_item(item_id: str, verbose: bool = False) -> str:
            \"\"\"Gets an item.

            Args:
                item_id: the item id.
                verbose: whether to include extra detail.
            \"\"\"
            return item_id
        """)
    report = analyze_repo(tmp_path)
    baseline = _baseline([
        {"name": "get_item", "param_names": ["item_id"], "required_param_names": ["item_id"]},
    ])

    changes = compute_schema_diff(baseline, report.tools)
    assert changes == []


def test_new_tool_is_not_flagged(tmp_path):
    # A tool present now but absent from the baseline is a new capability,
    # not a breaking change — nothing an old caller relied on stopped
    # working.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def brand_new(x: str) -> str:
            \"\"\"Brand new.

            Args:
                x: a value.
            \"\"\"
            return x
        """)
    report = analyze_repo(tmp_path)
    baseline = _baseline([])

    changes = compute_schema_diff(baseline, report.tools)
    assert changes == []


def test_cross_style_baseline_is_not_a_false_positive(tmp_path):
    # Reported by Edward Izgorodin (modelcontextprotocol/modelcontextprotocol
    # discussion #3322): a baseline captured from a FastMCP-decorated
    # function, diffed against a current run captured from the raw
    # Tool(inputSchema=...) constructor style, must not report the shared
    # required 'key' param as removed just because the two styles are
    # resolved through different code paths.
    write(tmp_path, "server.py", """
        from mcp.types import Tool
        lookup_tool = Tool(
            name="lookup",
            description="Look up a synthetic value.",
            inputSchema={
                "type": "object",
                "properties": {"key": {"type": "string", "description": "Key to look up."}},
                "required": ["key"],
            },
        )
        """)
    report = analyze_repo(tmp_path)
    baseline = _baseline([
        {"name": "lookup", "param_names": ["key"], "required_param_names": ["key"]},
    ])

    assert compute_schema_diff(baseline, report.tools) == []


def test_raw_schema_tool_catches_a_real_removed_parameter(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.types import Tool
        lookup_tool = Tool(
            name="lookup",
            description="Look up a synthetic value.",
            inputSchema={
                "type": "object",
                "properties": {"key": {"type": "string", "description": "Key to look up."}},
                "required": ["key"],
            },
        )
        """)
    report = analyze_repo(tmp_path)
    baseline = _baseline([
        {"name": "lookup", "param_names": ["key", "region"], "required_param_names": ["key", "region"]},
    ])

    changes = compute_schema_diff(baseline, report.tools)
    assert len(changes) == 1
    assert changes[0].change == "param_removed"
    assert "region" in changes[0].detail


def test_raw_schema_tool_with_unresolvable_property_stays_a_no_op(tmp_path):
    # A dynamic property key (not a string literal) can't be attributed a
    # name, so param_names would be incomplete — required_param_names must
    # be dropped too rather than risk misreading a still-present property as
    # newly required.
    write(tmp_path, "server.py", """
        from mcp.types import Tool
        _extra_key = "extra"
        lookup_tool = Tool(
            name="lookup",
            description="Look up a synthetic value.",
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Key to look up."},
                    _extra_key: {"type": "string"},
                },
                "required": ["key"],
            },
        )
        """)
    report = analyze_repo(tmp_path)
    baseline = _baseline([
        {"name": "lookup", "param_names": ["key"], "required_param_names": ["key"]},
    ])

    assert compute_schema_diff(baseline, report.tools) == []


def test_complete_baseline_vs_incomplete_current_is_not_a_false_positive(tmp_path):
    # Reported by Edward Izgorodin (modelcontextprotocol/modelcontextprotocol
    # discussion #3322) after the first fix above shipped: a baseline
    # captured when every property name *was* resolvable (both "key" and
    # "extra" present) diffed against a current run where "extra" is a
    # dynamic dict key must not report "extra" as removed just because the
    # current run's param_names is a known-incomplete subset.
    write(tmp_path, "server.py", """
        from mcp.types import Tool
        _extra_key = "extra"
        lookup_tool = Tool(
            name="lookup",
            description="Look up a synthetic value.",
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Key to look up."},
                    _extra_key: {"type": "string", "description": "Extra key to look up."},
                },
                "required": ["key", "extra"],
            },
        )
        """)
    report = analyze_repo(tmp_path)
    assert report.tools[0].param_names_complete is False
    baseline = _baseline([
        {"name": "lookup", "param_names": ["key", "extra"], "required_param_names": ["key", "extra"]},
    ])

    assert compute_schema_diff(baseline, report.tools) == []


def test_decorator_style_removal_still_caught_after_completeness_gating(tmp_path):
    # The new param_names_complete gate only applies to the *current* run
    # (decorator/class-based tools always resolve every arg, so it's always
    # True there) — a real removed parameter must still be caught, and a
    # baseline dict predating this field (no "param_names_complete" key at
    # all) must not disable that detection.
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def get_item() -> str:
            \"\"\"Gets an item.\"\"\"
            return "x"
        """)
    report = analyze_repo(tmp_path)
    baseline = _baseline([
        {"name": "get_item", "param_names": ["item_id"], "required_param_names": ["item_id"]},
    ])

    changes = compute_schema_diff(baseline, report.tools)
    assert len(changes) == 1
    assert changes[0].change == "param_removed"


def test_identical_schema_produces_no_changes(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def get_item(item_id: str) -> str:
            \"\"\"Gets an item.

            Args:
                item_id: the item id.
            \"\"\"
            return item_id
        """)
    report = analyze_repo(tmp_path)
    baseline = _baseline([
        {"name": "get_item", "param_names": ["item_id"], "required_param_names": ["item_id"]},
    ])

    assert compute_schema_diff(baseline, report.tools) == []


def _snapshot(tmp_path, analyze_repo, render_json):
    import json as _json
    return _json.loads(render_json(analyze_repo(tmp_path)))


def test_ts_and_go_breaking_changes_are_caught(tmp_path):
    import pytest

    from mcp_doctor.analyzer import analyze_repo
    from mcp_doctor.report import render_json
    from mcp_doctor.ts_analyzer import TS_AVAILABLE

    if not TS_AVAILABLE:
        pytest.skip("tree_sitter not installed")
    ts = tmp_path / "server.ts"
    go = tmp_path / "main.go"
    ts.write_text('server.registerTool("search", { description: "Search the index", inputSchema: '
                  '{ q: z.string().describe("query"), limit: z.number().optional().describe("max") } }, async () => ({ content: [] }));\n')
    go.write_text('package main\nfunc main() {\n  s.AddTool(mcp.NewTool("echo", mcp.WithDescription("Echo text back"), '
                  'mcp.WithString("text", mcp.Required(), mcp.Description("t")), mcp.WithString("lang", mcp.Description("l"))), h)\n}\n')
    baseline = _snapshot(tmp_path, analyze_repo, render_json)
    assert compute_schema_diff(baseline, analyze_repo(tmp_path).tools) == []

    # limit becomes required, q is removed; Go: lang becomes required
    ts.write_text('server.registerTool("search", { description: "Search the index", inputSchema: '
                  '{ limit: z.number().describe("max") } }, async () => ({ content: [] }));\n')
    go.write_text(go.read_text().replace('mcp.WithString("lang", mcp.Description("l"))', 'mcp.WithString("lang", mcp.Required(), mcp.Description("l"))'))
    changes = {(c.tool, c.change, c.detail.split("'")[1]) for c in compute_schema_diff(baseline, analyze_repo(tmp_path).tools)}
    assert changes == {
        ("search", "param_removed", "q"),
        ("search", "param_newly_required", "limit"),
        ("echo", "param_newly_required", "lang"),
    }


def test_baseline_from_before_ts_go_tracking_is_not_compared(tmp_path):
    from mcp_doctor.analyzer import analyze_repo

    (tmp_path / "server.ts").write_text('server.registerTool("search", { description: "Search the index", inputSchema: '
                                        '{ q: z.string().describe("query") } }, async () => ({ content: [] }));\n')
    legacy = {"tools": [{"name": "search", "file": "server.ts", "param_names": [], "required_param_names": []}]}
    assert compute_schema_diff(legacy, analyze_repo(tmp_path).tools) == []
