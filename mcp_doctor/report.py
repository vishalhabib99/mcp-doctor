"""Render an analyzer.Report as text or JSON."""

from __future__ import annotations

import json
from dataclasses import asdict

from .analyzer import Report

RESET = "\033[0m"
BOLD = "\033[1m"
RED = "\033[31m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
DIM = "\033[2m"


def _color_for_grade(grade: str) -> str:
    return {"A": GREEN, "B": GREEN, "C": YELLOW, "D": YELLOW, "F": RED}.get(grade, "")


def render_text(report: Report, use_color: bool = True) -> str:
    def c(code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if use_color else text

    lines: list[str] = []
    lines.append(c(BOLD, "mcp-doctor report"))
    grade_color = _color_for_grade(report.grade) if use_color else ""
    lines.append(
        f"Quality:  {c(BOLD, str(report.percent) + '%')}  "
        f"Grade: {grade_color}{report.grade}{RESET if use_color else ''}  "
        f"({len(report.tools)} tool(s) found)"
    )
    security_color = _color_for_grade(report.security_grade) if use_color else ""
    lines.append(
        f"Security: {c(BOLD, str(report.security_percent) + '%')}  "
        f"Grade: {security_color}{report.security_grade}{RESET if use_color else ''}"
    )
    lines.append("")

    if not report.tools:
        lines.append(c(YELLOW, "No MCP tools detected. Looked for @mcp.tool()-style decorators and Tool(...) constructors."))

    for t in report.tools:
        header_color = GREEN if not any(i.severity == "error" for i in t.issues) and not t.issues else (RED if any(i.severity == "error" for i in t.issues) else YELLOW)
        status = "OK" if not t.issues else ("FAIL" if any(i.severity == "error" for i in t.issues) else "WARN")
        lines.append(f"  {c(header_color, f'[{status}]')} {c(BOLD, t.name)} {c(DIM, f'({t.file}:{t.line})')}")
        for issue in t.issues:
            color = RED if issue.severity == "error" else YELLOW
            lines.append(f"      {c(color, issue.severity.upper())}  {issue.message}")
    if report.tools:
        lines.append("")

    if report.repo_issues:
        lines.append(c(BOLD, "Repo-level"))
        for issue in report.repo_issues:
            color = RED if issue.severity == "error" else YELLOW
            lines.append(f"  {c(color, issue.severity.upper())}  {issue.message}")

    return "\n".join(lines)


def render_json(report: Report) -> str:
    payload = {
        "score": report.score,
        "max_score": report.max_score,
        "percent": report.percent,
        "grade": report.grade,
        "security_score": report.security_score,
        "security_max_score": report.security_max_score,
        "security_percent": report.security_percent,
        "security_grade": report.security_grade,
        "tools": [
            {
                "name": t.name,
                "file": t.file,
                "line": t.line,
                "issues": [asdict(i) for i in t.issues],
            }
            for t in report.tools
        ],
        "repo_issues": [asdict(i) for i in report.repo_issues],
    }
    return json.dumps(payload, indent=2)
