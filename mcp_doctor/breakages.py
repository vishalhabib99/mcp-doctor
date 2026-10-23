"""Findings that will break an agent calling the server, shown before the grades.

Only checks whose failure is deterministic or measured, and has been confirmed
on real servers, are here. Checked at the commit before each of 41 fixes that
maintainers merged, the grade was an A for ~80% of them, so the report leads
with these instead. Track records are counted by hand at release from merged
upstream fixes; they are never computed at scan time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .analyzer import Report


@dataclass(frozen=True)
class BreakageKind:
    check: str
    breaks: str
    fix: str
    evidence: str


KINDS: dict[str, BreakageKind] = {
    k.check: k
    for k in (
        BreakageKind(
            "none_default_type",
            "a client that sends the advertised null default gets a validation error, and the tool never runs",
            "type it `T | None` (`mcp-doctor --fix` does this)",
            "fixed by maintainers in 20 repos",
        ),
        BreakageKind(
            "stdout_write",
            "stray stdout on stdio corrupts the JSON-RPC stream: calls time out (Python) or the client logs parse errors (Node)",
            "write to stderr or use logging",
            "fixed by maintainers in 3 repos",
        ),
        BreakageKind(
            "sdk_v2",
            "a fresh install gets mcp 2.x and the server fails to start",
            "pin `mcp<2`, or migrate the removed import",
            "removed in mcp 2.x; fails on import",
        ),
    )
}

# Named in the zero-breakage line, so "none found" says what was looked for.
CHECKED = "null defaults, stdout on stdio, mcp 2.x start-up"


# Code kept for reference, not served: a finding there breaks nothing an agent
# calls (contextform/freecad-mcp's archive/mcp_server_enhanced.py). It stays a
# finding under the grade; it just doesn't lead the report.
_NOT_SERVED = re.compile(r"(^|/)(archived?|deprecated|legacy|old|backups?)/", re.I)


@dataclass
class Breakage:
    check: str
    tool: str
    file: str
    line: int
    breaks: str
    fix: str
    evidence: str
    detail: str


def find(report: Report) -> list[Breakage]:
    out = _find(report)
    # One function registered on two servers (stdio and HTTP) is one fix, not two.
    seen: set[tuple[str, str, int, str]] = set()
    unique = []
    for b in out:
        key = (b.check, b.file, b.line, b.tool)
        if key not in seen:
            seen.add(key)
            unique.append(b)
    return unique


def _find(report: Report) -> list[Breakage]:
    out: list[Breakage] = []
    for t in report.tools:
        for i in t.issues:
            kind = KINDS.get(i.check)
            if kind and not _NOT_SERVED.search(i.file):
                tool = t.name
                if i.check == "none_default_type":
                    # "a, b defaults to None but ..." names the parameters.
                    tool = f"{t.name}({i.message.split(' defaults to None', 1)[0]})"
                out.append(Breakage(i.check, tool, i.file, i.line, kind.breaks, kind.fix, kind.evidence, i.message))
    for i in report.repo_issues:
        kind = KINDS.get(i.check)
        if kind and not _NOT_SERVED.search(i.message.split(" ", 1)[0]):
            # sdk_v2 messages start with "path:line `import`"; keep that as the location.
            where = i.message.split(" ", 1)[0]
            file, _, line = where.rpartition(":")
            out.append(Breakage(
                i.check, "", file or where, int(line) if line.isdigit() else 0,
                kind.breaks, kind.fix, kind.evidence, i.message,
            ))
    return out


def headline(breakages: list[Breakage]) -> str:
    if not breakages:
        return f"No known agent-breaking issues (checked: {CHECKED})"
    return f"Will break an agent: {len(breakages)}"


MAX_LINES = 10


def lines(breakages: list[Breakage], limit: int | None = MAX_LINES) -> list[str]:
    """One line per breakage, then its fix and track record. The full list is in --json."""
    out = []
    shown = breakages if limit is None else breakages[:limit]
    for b in shown:
        where = f"{b.file}:{b.line}" if b.line else b.file
        who = f"{b.tool} ({where})" if b.tool else where
        out.append(f"{who}: {b.breaks}.")
        out.append(f"    Fix: {b.fix}. ({b.evidence})")
    if len(shown) < len(breakages):
        out.append(f"...and {len(breakages) - len(shown)} more (all listed below and in --json).")
    return out
