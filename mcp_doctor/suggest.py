"""Turn --fix's planned changes into GitHub pull-request review suggestions.

GitHub only accepts a review comment on a line inside the PR's diff, so each
fix hunk is either a one-click ``suggestion`` (every line it replaces sits in
a diff hunk of the new file) or goes into the summary comment's patch.
Line numbers are the PR head's, because the fixes are planned on the head
checkout.
"""

from __future__ import annotations

import difflib
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .analyzer import analyze_repo
from .fix import plan_fixes

MARKER = "<!-- mcp-doctor-fix -->"

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Suggestion:
    path: str
    start_line: int  # 1-based, inclusive, on the PR head
    line: int
    body: str


def commentable_lines(patch: str) -> set[int]:
    """New-file line numbers a review comment can attach to: every line of
    every hunk in a GitHub pull-request file ``patch`` (added or context)."""
    lines: set[int] = set()
    new_line = 0
    for row in patch.splitlines():
        m = _HUNK_HEADER.match(row)
        if m:
            new_line = int(m.group(1))
            continue
        if row.startswith("-") or row.startswith("\\"):
            continue
        lines.add(new_line)
        new_line += 1
    return lines


def _suggestion_body(new_lines: list[str]) -> str:
    replacement = "".join(new_lines)
    if not replacement.endswith("\n"):
        replacement += "\n"
    return (
        f"{MARKER}\n**mcp-doctor fix** (safe, mechanical; see `mcp-doctor --diff`):\n\n"
        f"```suggestion\n{replacement}```\n"
    )


def file_suggestions(path: str, original: str, fixed: str, allowed: set[int]) -> tuple[list[Suggestion], int]:
    """Suggestions for one file, plus how many fix hunks couldn't be
    suggested inline because they touch a line outside the PR's diff."""
    old = original.splitlines(keepends=True)
    new = fixed.splitlines(keepends=True)
    suggestions: list[Suggestion] = []
    skipped = 0
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            # A pure insertion (new Args: entries) attaches to the line above,
            # which the suggestion repeats before the inserted lines.
            if i1 == 0:
                skipped += 1
                continue
            start, end = i1, i1
            replacement = [old[i1 - 1], *new[j1:j2]]
        else:
            start, end = i1 + 1, i2
            replacement = new[j1:j2]
        if all(n in allowed for n in range(start, end + 1)):
            suggestions.append(Suggestion(path, start, end, _suggestion_body(replacement)))
        else:
            skipped += 1
    return suggestions, skipped


def build(root: Path, pr_files: list[dict], repo_root: Path | None = None) -> dict:
    """``pr_files`` is GitHub's ``GET /pulls/{n}/files`` response, whose
    paths are relative to ``repo_root``; ``root`` is the scanned path, which
    can be a subdirectory of it."""
    repo_root = (repo_root or root).resolve()
    root = root.resolve()
    planned = plan_fixes(root, analyze_repo(root))
    allowed_by_path = {f["filename"]: commentable_lines(f.get("patch", "")) for f in pr_files}
    suggestions: list[Suggestion] = []
    outside = 0
    patch = []
    for scan_rel, (original, fixed) in planned.items():
        rel_file = (root / scan_rel).relative_to(repo_root).as_posix()
        patch.extend(difflib.unified_diff(
            original.splitlines(keepends=True), fixed.splitlines(keepends=True),
            fromfile=f"a/{rel_file}", tofile=f"b/{rel_file}",
        ))
        found, skipped = file_suggestions(rel_file, original, fixed, allowed_by_path.get(rel_file, set()))
        suggestions.extend(found)
        outside += skipped
    return {
        "suggestions": [asdict(s) for s in suggestions],
        "outside_diff": outside,
        "files": len(planned),
        "patch": "".join(patch),
    }


def _read_json_or_ndjson(path: Path) -> list[dict]:
    text = path.read_text().strip()
    if not text:
        return []
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def review_payload(result: dict, existing: list[dict], commit_id: str) -> dict | None:
    """A ``POST /pulls/{n}/reviews`` body with the suggestions not already
    posted on an earlier run (same path, line and body), or None."""
    seen = {(c.get("path"), c.get("line"), c.get("body")) for c in existing}
    comments = []
    for s in result["suggestions"]:
        if (s["path"], s["line"], s["body"]) in seen:
            continue
        comment = {"path": s["path"], "line": s["line"], "side": "RIGHT", "body": s["body"]}
        if s["start_line"] != s["line"]:
            comment.update(start_line=s["start_line"], start_side="RIGHT")
        comments.append(comment)
    if not comments:
        return None
    return {
        "commit_id": commit_id,
        "event": "COMMENT",
        "body": f"{MARKER}\nmcp-doctor can fix {len(comments)} issue(s) on lines this PR touches. "
                "Each suggestion below applies with one click.",
        "comments": comments,
    }


_PATCH_LIMIT = 40_000  # a PR comment holds 65,536 characters, and the report goes in it too


def summary_markdown(result: dict) -> str:
    if not result["files"]:
        return ""
    inline = len(result["suggestions"])
    lines = [
        "",
        f"#### Safe fixes: {result['files']} file(s)",
        "",
        f"- {inline} suggested inline on lines this PR changes (one click each)",
        f"- {result['outside_diff']} on lines outside this PR's diff",
        "",
        "Apply them all locally: `pip install mcp-server-lint && mcp-doctor . --fix` "
        "(or preview with `--diff`).",
    ]
    patch = result["patch"]
    if patch:
        if len(patch) > _PATCH_LIMIT:
            patch = patch[:_PATCH_LIMIT] + "\n... (truncated; run `mcp-doctor . --diff` for the full patch)\n"
        lines += ["", "<details><summary>Patch</summary>", "", "```diff", patch.rstrip("\n"), "```", "", "</details>"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m mcp_doctor.suggest",
        description="Plan --fix's changes as pull-request review suggestions. Run from the repo root.",
    )
    parser.add_argument("path", help="Scanned path, relative to the repo root")
    parser.add_argument("pr_files", help="GET /pulls/{n}/files response (JSON array or one object per line)")
    parser.add_argument("--existing", help="Earlier review comments (JSON array or one per line), to skip repeats")
    parser.add_argument("--commit", default="", help="PR head SHA, for the review payload")
    parser.add_argument("--review-out", help="Write the review payload here (deleted if there's nothing new)")
    parser.add_argument("--summary-out", help="Write the summary comment section here")
    args = parser.parse_args(argv)

    result = build(Path(args.path), _read_json_or_ndjson(Path(args.pr_files)), repo_root=Path.cwd())
    if args.review_out:
        existing = _read_json_or_ndjson(Path(args.existing)) if args.existing else []
        payload = review_payload(result, existing, args.commit)
        out = Path(args.review_out)
        if payload:
            out.write_text(json.dumps(payload))
        elif out.exists():
            out.unlink()
    if args.summary_out:
        Path(args.summary_out).write_text(summary_markdown(result))
    print(json.dumps({k: (len(v) if k == "suggestions" else v) for k, v in result.items() if k != "patch"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
