"""Indistinguishable-description check: fires on identical descriptions only, never on
templated CRUD families or get/set pairs (both were false positives in earlier drafts,
measured on 756 real tools from 18 servers)."""

from pathlib import Path

from mcp_doctor.analyzer import analyze_repo
from mcp_doctor.gate import check_tools_distinguishable
from mcp_doctor.lookalike import find_indistinguishable_tools


def test_identical_descriptions_are_flagged_case_and_punctuation_insensitive():
    # The real case: rhinomcp's modify_objects copy-pasted create_objects' description.
    tools = [
        ("create_objects", "Create multiple objects at once in the Rhino document."),
        ("modify_objects", "create multiple objects at once, in the Rhino document"),
    ]
    assert find_indistinguishable_tools(tools) == [("create_objects", "modify_objects")]


def test_verbs_and_entities_still_distinguish():
    tools = [
        ("confluence_get_page_restrictions", "Get view and edit restrictions for a Confluence page."),
        ("confluence_set_page_restrictions", "Set view and edit restrictions on a Confluence page."),
        ("update_person", "Update an existing person. Only provided fields are changed"),
        ("update_company", "Update an existing company. Only provided fields are changed"),
    ]
    assert find_indistinguishable_tools(tools) == []


def test_missing_descriptions_and_repeated_names_are_left_to_other_checks():
    assert find_indistinguishable_tools([("a", ""), ("b", None), ("c", "  ")]) == []
    assert find_indistinguishable_tools([("a", "Same text."), ("a", "Same text.")]) == []


def test_three_way_duplicate_gives_every_pair():
    tools = [("a", "Do the thing."), ("b", "Do the thing."), ("c", "Do the thing.")]
    assert find_indistinguishable_tools(tools) == [("a", "b"), ("a", "c"), ("b", "c")]


def test_live_gate_flags_both_tools():
    tools = [
        {"name": "create_objects", "description": "Create multiple objects at once in the Rhino document."},
        {"name": "modify_objects", "description": "Create multiple objects at once in the Rhino document."},
        {"name": "delete_objects", "description": "Delete objects from the Rhino document."},
    ]
    found = check_tools_distinguishable(tools)
    assert set(found) == {"create_objects", "modify_objects"}
    assert "modify_objects" in found["create_objects"][0].message


def test_static_audit_flags_both_tools(tmp_path: Path):
    (tmp_path / "server.py").write_text(
        "from mcp.server.fastmcp import FastMCP\n"
        "mcp = FastMCP('x')\n\n"
        "@mcp.tool()\n"
        "def create_objects(objects: list) -> str:\n"
        '    """Create multiple objects at once in the Rhino document."""\n'
        "    return ''\n\n"
        "@mcp.tool()\n"
        "def modify_objects(objects: list) -> str:\n"
        '    """Create multiple objects at once in the Rhino document."""\n'
        "    return ''\n"
    )
    report = analyze_repo(tmp_path)
    flagged = {t.name for t in report.tools for i in t.issues if i.check == "indistinguishable_description"}
    assert flagged == {"create_objects", "modify_objects"}


def test_non_latin_descriptions_are_compared_in_full():
    # Regression: an ASCII-only tokenizer reduced these to "tab note fav liked" and called them equal.
    tools = [
        ("user_profile", "获取指定的小红书用户主页，返回用户基本信息。tab 可选 note、fav、liked"),
        ("get_my_profile", "获取当前登录用户的主页，返回用户基本信息。tab 可选 note、fav、liked"),
    ]
    assert find_indistinguishable_tools(tools) == []
    assert find_indistinguishable_tools([("a", "获取用户主页"), ("b", "获取用户主页。")]) == [("a", "b")]
