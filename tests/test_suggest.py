import json
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

from mcp_doctor.suggest import MARKER, build, commentable_lines, file_suggestions, review_payload, summary_markdown


def test_commentable_lines_reads_added_and_context_lines_of_every_hunk():
    patch = dedent("""\
        @@ -1,3 +1,4 @@
         a
        -b
        +b2
        +b3
         c
        @@ -10,2 +11,2 @@ def f():
         x
        +y
        \\ No newline at end of file
        """)
    assert commentable_lines(patch) == {1, 2, 3, 4, 11, 12}


def test_replace_inside_diff_becomes_a_suggestion():
    original = "def run(\n    name: str = None,\n) -> str:\n"
    fixed = "def run(\n    name: str | None = None,\n) -> str:\n"
    found, outside = file_suggestions("server.py", original, fixed, allowed={1, 2, 3})
    assert outside == 0
    assert len(found) == 1
    s = found[0]
    assert (s.path, s.start_line, s.line) == ("server.py", 2, 2)
    assert MARKER in s.body
    assert "```suggestion\n    name: str | None = None,\n```" in s.body


def test_insertion_attaches_to_the_line_above_and_repeats_it():
    original = '    Args:\n        city: City.\n    """\n'
    fixed = '    Args:\n        city: City.\n        days: TODO: describe this parameter.\n    """\n'
    found, outside = file_suggestions("server.py", original, fixed, allowed={1, 2, 3})
    assert outside == 0
    s = found[0]
    assert (s.start_line, s.line) == (2, 2)
    assert "```suggestion\n        city: City.\n        days: TODO: describe this parameter.\n```" in s.body


def test_hunk_outside_the_pr_diff_is_counted_not_suggested():
    original = "a\nname: str = None\n"
    fixed = "a\nname: str | None = None\n"
    found, outside = file_suggestions("server.py", original, fixed, allowed={1})
    assert found == []
    assert outside == 1


def test_build_maps_scan_subdirectory_to_repo_paths(tmp_path):
    server = tmp_path / "server" / "app.py"
    server.parent.mkdir()
    server.write_text(dedent("""\
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
        """))
    pr_files = [{"filename": "server/app.py", "patch": "@@ -0,0 +1,14 @@\n" + "".join(f"+{l}\n" for l in range(14))}]

    result = build(tmp_path / "server", pr_files, repo_root=tmp_path)

    assert result["files"] == 1
    assert result["outside_diff"] == 0
    [s] = result["suggestions"]
    assert (s["path"], s["line"]) == ("server/app.py", 5)
    assert "def run(name: str | None = None) -> str:" in s["body"]
    assert "+++ b/server/app.py" in result["patch"]
    # Planning a suggestion never writes the file.
    assert "name: str = None" in server.read_text()


def test_cli_prints_json(tmp_path):
    (tmp_path / "files.json").write_text("[]")
    out = subprocess.run(
        [sys.executable, "-m", "mcp_doctor.suggest", ".", str(tmp_path / "files.json")],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert out.returncode == 0
    assert json.loads(out.stdout) == {"suggestions": 0, "outside_diff": 0, "files": 0}


def test_review_payload_skips_suggestions_already_posted():
    result = {"suggestions": [
        {"path": "a.py", "start_line": 2, "line": 2, "body": "B1"},
        {"path": "a.py", "start_line": 5, "line": 7, "body": "B2"},
    ]}
    payload = review_payload(result, existing=[{"path": "a.py", "line": 2, "body": "B1"}], commit_id="abc")
    assert payload["commit_id"] == "abc"
    assert payload["event"] == "COMMENT"
    assert payload["comments"] == [
        {"path": "a.py", "line": 7, "side": "RIGHT", "body": "B2", "start_line": 5, "start_side": "RIGHT"},
    ]
    assert review_payload(result, existing=[
        {"path": "a.py", "line": 2, "body": "B1"}, {"path": "a.py", "line": 7, "body": "B2"},
    ], commit_id="abc") is None


def test_summary_markdown_counts_and_truncates_patch():
    assert summary_markdown({"files": 0, "suggestions": [], "outside_diff": 0, "patch": ""}) == ""
    md = summary_markdown({"files": 2, "suggestions": [{}], "outside_diff": 3, "patch": "x" * 50_000})
    assert "1 suggested inline" in md and "3 on lines outside" in md
    assert "truncated" in md
    assert len(md) < 45_000


def test_cli_writes_review_and_summary(tmp_path):
    (tmp_path / "app.py").write_text(dedent("""\
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
        """))
    (tmp_path / "files.ndjson").write_text(json.dumps({"filename": "app.py", "patch": "@@ -4,2 +4,2 @@\n x\n+y\n"}) + "\n")
    out = subprocess.run(
        [sys.executable, "-m", "mcp_doctor.suggest", ".", "files.ndjson", "--commit", "sha1",
         "--review-out", "review.json", "--summary-out", "summary.md"],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert out.returncode == 0, out.stderr
    review = json.loads((tmp_path / "review.json").read_text())
    assert [c["line"] for c in review["comments"]] == [5]
    assert "1 suggested inline" in (tmp_path / "summary.md").read_text()
