from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .analyzer import analyze_repo
from .report import render_json, render_text
from .schema_diff import compute_schema_diff


def _print_skipped(skipped: list[str]) -> None:
    if not skipped:
        return
    print(
        f"Left {len(skipped)} None-default parameter(s) as they are: each takes JSON text, and on the "
        "official MCP SDK `str | None` makes the server JSON-decode the value, so a '{...}' or '[...]' "
        "string that works today would be rejected. Fix these by hand:",
        file=sys.stderr,
    )
    for item in skipped:
        print(f"  {item}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["init-action"]:
        from .init_action import main as init_action_main

        return init_action_main(argv[1:])
    if argv[:1] == ["fuzz"]:
        # Runtime checks (formerly mcp-fuzz and mcp-reality-check) launch the server and call
        # it, so they need the MCP SDK, kept optional to keep the static scan light.
        try:
            import mcp  # noqa: F401
        except ImportError:
            print(
                "mcp-doctor fuzz needs the MCP SDK (Python 3.10+). Install it with:\n"
                "  pip install 'mcp-server-lint[runtime]'\n"
                "or run it without installing:\n"
                "  uvx --from 'mcp-server-lint[runtime]' mcp-doctor fuzz -- python server.py",
                file=sys.stderr,
            )
            return 2
        from .fuzz.cli import main as fuzz_main

        return fuzz_main(argv[1:])
    if argv[:1] == ["verdict"]:
        from .verdict import main as verdict_main

        return verdict_main(argv[1:])
    if argv[:1] == ["guard"]:
        from .guard import main as guard_main

        return guard_main(argv[1:])

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
        help="Apply safe, mechanical fixes in place (None defaults typed nullable, missing Args: "
        "entries, bare except), then re-report",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Print the fixes --fix would make as a unified diff (pipe to `git apply`) and change nothing",
    )
    parser.add_argument(
        "--fix-only",
        metavar="KINDS",
        default=None,
        help="With --fix or --diff: make only these kinds of fix, comma-separated "
        "(none-default, bare-except, args-stub), e.g. one kind per upstream PR",
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

    only = None
    if args.fix_only is not None:
        from .fix import FIX_KINDS

        only = frozenset(k.strip() for k in args.fix_only.split(",") if k.strip())
        unknown = sorted(only - set(FIX_KINDS))
        if unknown or not only:
            print(f"error: --fix-only takes {', '.join(FIX_KINDS)}; got {args.fix_only!r}", file=sys.stderr)
            return 2
        if not (args.fix or args.diff):
            print("error: --fix-only needs --fix or --diff", file=sys.stderr)
            return 2

    root = Path(args.path).resolve()
    if not root.exists():
        print(f"error: path does not exist: {root}", file=sys.stderr)
        return 2

    report = analyze_repo(root)

    if args.diff:
        import difflib

        from .fix import plan_fixes

        skipped: list[str] = []
        planned = plan_fixes(root, report, only=only, skipped=skipped)
        for rel_file, (original, fixed) in planned.items():
            sys.stdout.writelines(difflib.unified_diff(
                original.splitlines(keepends=True), fixed.splitlines(keepends=True),
                fromfile=f"a/{rel_file}", tofile=f"b/{rel_file}",
            ))
        print(f"{len(planned)} file(s) would change." if planned else "Nothing to fix.", file=sys.stderr)
        _print_skipped(skipped)
        return 0

    if args.fix:
        from .fix import apply_fixes

        skipped = []
        changed = apply_fixes(root, report, only=only, skipped=skipped)
        if changed:
            print(f"Fixed {len(changed)} file(s): {', '.join(changed)}", file=sys.stderr)
            report = analyze_repo(root)
        else:
            print("Nothing to fix.", file=sys.stderr)
        _print_skipped(skipped)

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

    if args.fail_under and not report.graded:
        # Warn, don't fail: 0 tools is the honest answer for servers that name
        # tools at runtime (git-mcp, dbhub), but a silent pass is not (bug #102).
        print(f"warning: --fail-under {args.fail_under} not applied: 0 tools found, so there's no quality score to gate on", file=sys.stderr)
    elif args.fail_under and report.percent < args.fail_under:
        return 1
    if args.fail_on_breaking_change and schema_changes:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
