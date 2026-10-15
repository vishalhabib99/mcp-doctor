"""`mcp-doctor init-action`: write the GitHub Action workflow into a repo, so
turning on PR checks is one command instead of copying YAML from the README."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

WORKFLOW_REL = Path(".github") / "workflows" / "mcp-doctor.yml"
COMMENT_WORKFLOW_REL = Path(".github") / "workflows" / "mcp-doctor-comment.yml"

# A fork's PR gets a read-only token, so the scan can't comment there. This
# second workflow runs after the scan with a write token, never checks out the
# PR's code, and posts the report the scan saved.
COMMENT_WORKFLOW = """name: mcp-doctor comment
on:
  workflow_run:
    workflows: ["mcp-doctor"]
    types: [completed]
permissions:
  actions: read          # to download the report the scan saved
  pull-requests: write   # to post it on a pull request from a fork
jobs:
  comment:
    if: github.event.workflow_run.event == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: vishalhabib99/mcp-doctor/comment@v1
"""


def render_workflow(path: str = ".", fail_under: int = 0) -> str:
    return f"""name: mcp-doctor
on: pull_request
permissions:
  contents: read
  pull-requests: write   # for the report comment and fix suggestions
jobs:
  mcp-doctor:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: vishalhabib99/mcp-doctor@v1
        with:
          path: {path}
          fail-under: {fail_under}   # 0 = report only, never fails the PR
"""


def find_repo_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp-doctor init-action",
        description="Add the mcp-doctor GitHub Action to a repo: every pull request gets a report "
        "comment and the safe fixes as one-click suggestions, including pull requests from forks.",
    )
    parser.add_argument("path", nargs="?", default=".", help="The MCP server's directory (default: current directory). "
                        "The workflow goes in the enclosing git repo's root, pointed at this path.")
    parser.add_argument("--fail-under", type=int, default=0,
                        help="Fail the PR check below this score percent (default: 0, report only)")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing workflow file")
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="Print the workflow to stdout and write nothing")
    args = parser.parse_args(argv)

    target = Path(args.path).resolve()
    if not target.is_dir():
        print(f"error: not a directory: {target}", file=sys.stderr)
        return 2

    root = find_repo_root(target)
    if root is None:
        print(f"warning: no git repo found at or above {target}; writing the workflow there", file=sys.stderr)
        root = target
    rel = target.relative_to(root).as_posix()
    files = {
        WORKFLOW_REL: render_workflow(path=rel if rel != "." else ".", fail_under=args.fail_under),
        COMMENT_WORKFLOW_REL: COMMENT_WORKFLOW,
    }

    if args.print_only:
        sys.stdout.write("\n".join(f"# {rel_path.as_posix()}\n{text}" for rel_path, text in files.items()))
        return 0

    written = []
    for rel_path, text in files.items():
        out = root / rel_path
        if out.exists() and not args.force:
            print(f"Kept the existing {rel_path.as_posix()} (use --force to overwrite)", file=sys.stderr)
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        written.append(rel_path.as_posix())
        print(f"Wrote {rel_path.as_posix()}")
    if not written:
        print("error: both workflows already exist (use --force to overwrite)", file=sys.stderr)
        return 1

    # Show the grade PRs will start from, so the first report comment isn't a surprise.
    from .analyzer import analyze_repo

    report = analyze_repo(target)
    if not report.graded:
        print("Note: mcp-doctor found 0 tools here, so PR reports will say \"Not graded\". "
              "If the server lives in a subdirectory, rerun with that path.")
    else:
        print(f"Current quality: {report.percent}% ({report.grade}), {len(report.tools)} tool(s).")
    print(f"Next: git add {' '.join(written)} && git commit -m \"Add mcp-doctor PR check\"")
    return 0
