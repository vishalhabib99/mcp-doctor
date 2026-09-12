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
