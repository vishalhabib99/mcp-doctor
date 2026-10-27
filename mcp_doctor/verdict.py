"""`mcp-doctor verdict`: one release decision, SHIP, FIX-FIRST or BLOCK, from the static scan and,
when the server can be launched, the fuzz and --truth runs (formerly mcp-trust-check).

The combined score is an average, and an average can hide the one failure that matters:
a server that crashes on a single bad input can still average 95%. The decision works the
other way round. It runs a short list of explicit rules over the raw findings, and the
worst finding wins. Every rule that fires is listed as a reason, so the verdict is never
a black box.

Next to the verdict sits a confidence level, the same split TypeSafe's Jev makes between an
answer and whether to act on it. Jev learns its confidence; here it's how much of the server
was actually exercised, because a SHIP after calling 8 of 30 tools says little about the other
22. It's a level with its reasons, not a probability: a number like 0.82 would need past
releases with known outcomes to calibrate against, and there aren't any.

Same discipline as the rest of the trilogy: no LLM, no weights, deterministic. The static path needs
no MCP SDK; the runtime path imports it only when --run or --url is given.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

SHIP = "SHIP"
FIX_FIRST = "FIX-FIRST"
BLOCK = "BLOCK"

HIGH = "HIGH"
MEDIUM = "MEDIUM"
LOW = "LOW"

# Share of the server's tools that fuzz actually called. Explicit, not fitted to anything.
HIGH_COVERAGE = 0.8
MEDIUM_COVERAGE = 0.5


@dataclass
class Decision:
    verdict: str
    blockers: list[str] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    confidence: str = LOW
    confidence_reasons: list[str] = field(default_factory=list)
    coverage_percent: float | None = None

    @property
    def needs_human_review(self) -> bool:
        # Only a confident SHIP is safe to act on without a person looking.
        return not (self.verdict == SHIP and self.confidence == HIGH)


def _doctor_issues(doctor: dict) -> list[dict]:
    issues = list(doctor.get("repo_issues", []))
    for tool in doctor.get("tools", []):
        issues.extend(tool.get("issues", []))
    return issues


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def decide(doctor: dict | None, fuzz: dict | None, reality: dict | None) -> Decision:
    blockers: list[str] = []
    fixes: list[str] = []
    notes: list[str] = []

    if doctor is not None:
        issues = _doctor_issues(doctor)
        sec_errors = [i for i in issues if i.get("category") == "security" and i.get("severity") == "error"]
        sec_warnings = [i for i in issues if i.get("category") == "security" and i.get("severity") != "error"]
        quality_errors = [
            i for i in issues
            if i.get("category") != "security" and i.get("severity") == "error" and i.get("check") != "sdk_v2"
        ]
        sdk_v2 = [i for i in issues if i.get("check") == "sdk_v2"]
        if sec_errors:
            checks = sorted({i["check"] for i in sec_errors})
            blockers.append(f"static scan: {_count(len(sec_errors), 'security error')} ({', '.join(checks)})")
        if sec_warnings:
            fixes.append(f"static scan: {_count(len(sec_warnings), 'security warning')}")
        if quality_errors:
            fixes.append(f"static scan: {_count(len(quality_errors), 'spec/documentation error')}")
        if sdk_v2:
            fixes.append("static scan: a fresh install gets mcp 2.x, which removed an import the server uses (pin 'mcp<2' or migrate)")

    if fuzz is not None:
        if fuzz.get("connect_error"):
            blockers.append(f"fuzz: server never connected ({fuzz['connect_error']})")
        if fuzz.get("crash_count", 0) > 0:
            blockers.append(f"fuzz: {_count(fuzz['crash_count'], 'crash')} on bad input (the server process died)")
        if fuzz.get("terminated_early"):
            blockers.append(f"fuzz: run ended early ({fuzz['terminated_early']})")
        if fuzz.get("timeout_count", 0) > 0:
            fixes.append(f"fuzz: {_count(fuzz['timeout_count'], 'timeout')} on bad input")
        hidden = (fuzz.get("hidden_errors") or {}).get("tools") or []
        if hidden:
            # The call completed, but the agent got no reason to act on (mcp 2.x drops an unexpected exception's text).
            fixes.append(f"fuzz: {_count(len(hidden), 'tool')} answered an error with only \"Error executing tool <name>\" "
                         f"({', '.join(t['name'] for t in hidden)}); raise ToolError with a message")
        concurrency = (fuzz.get("concurrency") or {}).get("flagged_tools") or []
        if concurrency:
            fixes.append(f"fuzz: {_count(len(concurrency), 'tool')} misbehaved under concurrent calls")
        stale = (fuzz.get("sequence") or {}).get("stale_after_delete_count", 0)
        if stale:
            fixes.append(f"fuzz: {_count(stale, 'resource')} still readable after delete")
        orphans = (fuzz.get("cross_resource") or {}).get("orphaned_child_count", 0)
        if orphans:
            fixes.append(f"fuzz: {_count(orphans, 'child resource')} left orphaned after parent delete")

        # Single-sample measurements: worth a look, never a rule (same caveat fuzz documents).
        slow = (fuzz.get("latency") or {}).get("slow_tools") or []
        if slow:
            notes.append("Worth a manual look, one call each: slow " + ", ".join(
                f"{t['name']} ({t['duration_ms']:.0f}ms)" for t in slow) + ".")
        bloated = (fuzz.get("response_size") or {}).get("bloated_tools") or []
        if bloated:
            notes.append("Worth a manual look, one call each: large responses from " + ", ".join(
                f"{t['name']} ({t['response_chars']:,} chars)" for t in bloated) + ".")

    if reality is not None:
        if reality.get("connect_error"):
            blockers.append(f"fuzz --truth: server never connected ({reality['connect_error']})")
        tools = reality.get("tools", [])
        flagged = [t for t in tools if t.get("refusal_in_disguise")]
        errors = [t["name"] for t in flagged if str(t["refusal_in_disguise"]).startswith("error payload: ")]
        refusals = [t["name"] for t in flagged if t["name"] not in errors]
        if refusals:
            # An agent reads a disguised refusal as success and acts on it: the worst silent failure.
            blockers.append(f"fuzz --truth: disguised refusal from {', '.join(refusals)}")
        if errors:
            blockers.append(f"fuzz --truth: error returned with isError false from {', '.join(errors)}")
        empty = [t["name"] for t in tools if t.get("empty_content")]
        if empty:
            fixes.append(f"fuzz --truth: empty content from {', '.join(empty)}")
        schema = [t["name"] for t in tools if t.get("schema_violation")]
        if schema:
            fixes.append(f"fuzz --truth: output breaks its own schema in {', '.join(schema)}")
        echo = [t["name"] for t in tools if t.get("echo_mismatch_inputs")]
        if echo:
            # fuzz --truth reports this separately and never scores it: a substring match can't
            # tell a correct paraphrase from a wrong answer. Same weight here, a note, not a rule.
            notes.append(f"Worth a manual look, not scored: output never mentions its input in {', '.join(echo)}.")

    if fuzz is None and reality is None:
        notes.append(
            "Runtime checks were not run, so this decision covers static analysis only. "
            "A SHIP here means the source looks clean, not that the server behaves."
        )

    verdict = BLOCK if blockers else FIX_FIRST if fixes else SHIP
    confidence, reasons, coverage = _confidence(verdict, fuzz, reality)
    return Decision(
        verdict=verdict,
        blockers=blockers,
        fixes=fixes,
        notes=notes,
        confidence=confidence,
        confidence_reasons=reasons,
        coverage_percent=coverage,
    )


def _confidence(verdict: str, fuzz: dict | None, reality: dict | None) -> tuple[str, list[str], float | None]:
    if verdict == BLOCK:
        # A blocker was observed directly. Tools that weren't tested can't un-crash a server.
        return HIGH, ["A blocker was observed directly; untested tools can only add to it."], None

    if fuzz is None and reality is None:
        return LOW, ["Runtime checks didn't run, so no tool was actually called."], None

    reasons: list[str] = []
    coverage = None
    level = HIGH
    if fuzz is not None:
        tested = fuzz.get("tested_count", 0)
        total = tested + fuzz.get("skipped_count", 0)
        coverage = round(100 * tested / total, 1) if total else 0.0
        share = tested / total if total else 0.0
        level = HIGH if share >= HIGH_COVERAGE else MEDIUM if share >= MEDIUM_COVERAGE else LOW
        reasons.append(f"fuzz called {tested} of {total} tools ({coverage:.0f}%).")
        if fuzz.get("skipped_count", 0):
            reasons.append(
                "Skipped tools aren't annotated read-only. To cover them, run with "
                "--include-destructive against a sandbox."
            )
    if reality is not None and reality.get("checkable_count", 0) == 0:
        # No successful response came back, so truthfulness was never actually checked.
        if level == HIGH:
            level = MEDIUM
        reasons.append("fuzz --truth got no successful response to check.")
    return level, reasons, coverage


def render_markdown(d: Decision) -> str:
    icon = {SHIP: "✅", FIX_FIRST: "🟡", BLOCK: "⛔"}[d.verdict]
    lines = [f"### Release decision: {icon} {d.verdict}"]
    if d.blockers:
        lines.append("\n**Blockers** (any one of these blocks release):")
        lines.extend(f"- {b}" for b in d.blockers)
    if d.fixes:
        lines.append("\n**Fix first** (shippable once addressed, or knowingly accepted):")
        lines.extend(f"- {f}" for f in d.fixes)
    if not d.blockers and not d.fixes:
        lines.append("\nNo rule fired.")
    review = "yes" if d.needs_human_review else "no"
    lines.append(f"\n**Confidence: {d.confidence}** · needs human review: {review}")
    lines.extend(f"- {r}" for r in d.confidence_reasons)
    for n in d.notes:
        lines.append(f"\n> {n}")
    return "\n".join(lines) + "\n"


def _grade(pct: float) -> str:
    return "A" if pct >= 90 else "B" if pct >= 80 else "C" if pct >= 70 else "D" if pct >= 60 else "F"


def _pairs(values: list[str], flag: str) -> dict[str, str]:
    out = {}
    for pair in values:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"{flag} expects KEY=VALUE, got {pair!r}")
        out[key] = value
    return out


def main(argv: list[str]) -> int:
    import argparse
    import json
    import shlex

    from .analyzer import analyze_repo
    from .report import render_json

    parser = argparse.ArgumentParser(
        prog="mcp-doctor verdict",
        description="One release decision for an MCP server: SHIP, FIX-FIRST or BLOCK, with every reason "
        "listed and a confidence level. Static scan always; add --run or --url to also launch the "
        "server and fuzz it (needs the runtime extra).",
    )
    parser.add_argument("path", nargs="?", default=None,
                        help="the server's source for the static scan (default: current directory; with --url, skipped unless given)")
    parser.add_argument("--run", default=None, help='the command that starts the server over stdio, e.g. "python server.py"')
    parser.add_argument("--url", default=None, help="a running server's Streamable HTTP URL, instead of --run")
    parser.add_argument("--header", action="append", default=[], metavar="KEY=VALUE", help="HTTP header for --url (repeatable)")
    parser.add_argument("--env", action="append", default=[], metavar="KEY=VALUE", help="environment variable for --run (repeatable)")
    parser.add_argument("--include-destructive", action="store_true", help="also call tools not marked readOnlyHint")
    parser.add_argument("--timeout", type=float, default=15.0, help="seconds per tool call before it counts as a hang")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--fail-on", choices=[BLOCK, FIX_FIRST], default=None,
                        help="exit 1 on this verdict or worse (FIX-FIRST also fails on BLOCK)")
    args = parser.parse_args(argv)
    if args.run and args.url:
        parser.error("use --run or --url, not both")
    if args.header and not args.url:
        parser.error("--header requires --url")
    if args.env and not args.run:
        parser.error("--env requires --run")

    # Bug #116: with --url the server's code is usually not here, and scanning the
    # current directory graded whatever repo the command ran in.
    path = args.path if args.path is not None else (None if args.url else ".")
    doctor = json.loads(render_json(analyze_repo(Path(path)))) if path is not None else None
    fuzz = reality = None
    if args.run or args.url:
        try:
            import asyncio

            from .fuzz.engine import run_fuzz
            from .fuzz.report import build_report as build_fuzz, to_dict as fuzz_dict
            from .reality.engine import run_reality_check
            from .reality.report import build_report as build_reality, to_dict as reality_dict
        except ImportError:
            print("--run/--url need the MCP SDK: pip install 'mcp-server-lint[runtime]'", file=sys.stderr)
            return 2
        if args.url:
            target = {"url": args.url, "headers": _pairs(args.header, "--header") or None}
            raw = asyncio.run(run_fuzz(**target, include_destructive=args.include_destructive, timeout=args.timeout))
            raw_truth = asyncio.run(run_reality_check(**target, include_destructive=args.include_destructive, timeout=args.timeout))
        else:
            command, *rest = shlex.split(args.run)
            env = _pairs(args.env, "--env") or None
            raw = asyncio.run(run_fuzz(command=command, args=rest, env=env,
                                       include_destructive=args.include_destructive, timeout=args.timeout))
            raw_truth = asyncio.run(run_reality_check(command, rest, env=env,
                                                      include_destructive=args.include_destructive, timeout=args.timeout))
        fuzz = fuzz_dict(build_fuzz(raw))
        reality = reality_dict(build_reality(raw_truth))

    d = decide(doctor, fuzz, reality)
    scores = {"static": doctor.get("percent")} if doctor is not None else {}
    if fuzz is not None:
        scores["fuzz"] = fuzz.get("crash_resilience_percent")
    if reality is not None:
        scores["truth"] = reality.get("sanity_percent")
    if args.json:
        print(json.dumps({
            "verdict": d.verdict, "blockers": d.blockers, "fixes": d.fixes, "notes": d.notes,
            "confidence": d.confidence, "confidence_reasons": d.confidence_reasons,
            "needs_human_review": d.needs_human_review, "coverage_percent": d.coverage_percent,
            "scores": scores, "markdown": render_markdown(d),
        }, indent=2))
    else:
        parts = [f"{k} {v:.0f}% ({_grade(v)})" for k, v in scores.items() if isinstance(v, (int, float))]
        print(f"mcp-doctor verdict: {d.verdict} ({d.confidence} confidence) · " + " · ".join(parts))
        if fuzz is None:
            print("Static scan only. Add --run \"<command>\" or --url to also fuzz the running server.")
        if doctor is None:
            print("No static scan: pass the server's source path to include it.")
        print()
        print(render_markdown(d))
    if args.fail_on == BLOCK and d.verdict == BLOCK:
        return 1
    if args.fail_on == FIX_FIRST and d.verdict in (BLOCK, FIX_FIRST):
        return 1
    return 0
