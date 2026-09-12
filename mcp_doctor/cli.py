from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .analyzer import analyze_repo
from .report import render_json, render_text
from .schema_diff import compute_schema_diff


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp-doctor",
        description="Audit an MCP server implementation for spec conformance and quality issues.",
    )
    parser.add_argument("path", nargs="?", default=".", help="Path to the repo/server to audit (default: current directory)")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON instead of text")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color in text output")
    parser.add_argument(
        "--fail-under",
        type=int,
        default=0,
        help="Exit with status 1 if score percent is below this threshold (for CI)",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Apply safe, mechanical fixes in place (bare except, missing Args: stubs), then re-report",
    )
    parser.add_argument(
        "--diff-against",
        metavar="PATH",
        default=None,
        help="Compare this run's tool signatures against a previously saved --json report at "
        "PATH, flagging a removed tool, a removed parameter, or a parameter that's newly "
        "required — breaking changes an agent that already learned the old schema wouldn't "
        "expect. Python tools only, for now.",
    )
    parser.add_argument(
        "--fail-on-breaking-change",
        action="store_true",
        help="With --diff-against: exit with status 1 if any breaking schema change is found",
    )
    args = parser.parse_args(argv)

    root = Path(args.path).resolve()
    if not root.exists():
        print(f"error: path does not exist: {root}", file=sys.stderr)
        return 2

    report = analyze_repo(root)

    if args.fix:
        from .fix import apply_fixes

        changed = apply_fixes(root, report)
        if changed:
            print(f"Fixed {len(changed)} file(s): {', '.join(changed)}", file=sys.stderr)
            report = analyze_repo(root)
        else:
            print("Nothing to fix.", file=sys.stderr)

    schema_changes = []
    if args.diff_against:
        baseline_path = Path(args.diff_against)
        if not baseline_path.exists():
            print(f"error: --diff-against path does not exist: {baseline_path}", file=sys.stderr)
            return 2
        baseline = json.loads(baseline_path.read_text())
        schema_changes = compute_schema_diff(baseline, report.tools)

    diff_kwargs = {"schema_changes": schema_changes} if args.diff_against else {}
    if args.json:
        print(render_json(report, **diff_kwargs))
    else:
        print(render_text(
            report,
            use_color=not args.no_color and sys.stdout.isatty(),
            diff_against_label=args.diff_against or "",
            **diff_kwargs,
        ))

    if args.fail_under and report.percent < args.fail_under:
        return 1
    if args.fail_on_breaking_change and schema_changes:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
