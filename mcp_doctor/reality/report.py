"""Turns a raw RealityCheckReport into a scored, human- or JSON-readable
report.

The score covers only the checks confident enough to be scored: a
disguised refusal, empty content on a claimed success, and a violation of
the tool's own declared output schema. The echo/relevance signal is
reported separately as "worth investigating" and never folded into the
score — a genuine, on-topic answer legitimately doesn't have to repeat the
input back verbatim, so a mismatch there is a prompt to look, not proof of
a bug. Same "don't overclaim a heuristic" split as mcp-fuzz's
valid_call_errored handling.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcp_doctor.reality.engine import RealityCheckReport


@dataclass
class ToolReport:
    name: str
    tested: bool
    skip_reason: str | None
    refusal_in_disguise: str | None = None
    empty_content: bool = False
    schema_violation: str | None = None
    echo_mismatch_inputs: list[str] | None = None
    flagged: bool = False  # any scored issue found


@dataclass
class Report:
    server_command: str
    connect_error: str | None
    terminated_early: str | None
    tools: list[ToolReport]
    tested_count: int
    skipped_count: int
    checkable_count: int  # tested and not an honest isError, i.e. had content to check
    flagged_count: int
    sanity_percent: float | None
    grade: str | None


def _grade_for_percent(pct: float) -> str:
    if pct >= 97:
        return "A"
    if pct >= 90:
        return "B"
    if pct >= 75:
        return "C"
    if pct >= 50:
        return "D"
    return "F"


def build_report(raw: RealityCheckReport) -> Report:
    tool_reports: list[ToolReport] = []
    tested_count = 0
    skipped_count = 0
    checkable_count = 0
    flagged_count = 0

    for r in raw.results:
        if not r.tested:
            skipped_count += 1
            tool_reports.append(ToolReport(name=r.tool_name, tested=False, skip_reason=r.skip_reason))
            continue

        tested_count += 1

        if r.skip_reason is not None:
            # Either an honest isError=true, or the call didn't complete at
            # all — nothing here to sanity-check either way.
            tool_reports.append(ToolReport(name=r.tool_name, tested=True, skip_reason=r.skip_reason))
            continue

        checkable_count += 1
        flagged = bool(r.refusal_in_disguise or r.empty_content or r.schema_violation)
        if flagged:
            flagged_count += 1
        tool_reports.append(ToolReport(
            name=r.tool_name,
            tested=True,
            skip_reason=None,
            refusal_in_disguise=r.refusal_in_disguise,
            empty_content=r.empty_content,
            schema_violation=r.schema_violation,
            echo_mismatch_inputs=r.echo_mismatch_inputs,
            flagged=flagged,
        ))

    if checkable_count > 0:
        percent = 100.0 * (1 - flagged_count / checkable_count)
        grade = _grade_for_percent(percent)
    else:
        percent = None
        grade = None

    return Report(
        server_command=raw.server_command,
        connect_error=raw.connect_error,
        terminated_early=raw.terminated_early,
        tools=tool_reports,
        tested_count=tested_count,
        skipped_count=skipped_count,
        checkable_count=checkable_count,
        flagged_count=flagged_count,
        sanity_percent=percent,
        grade=grade,
    )


def render_text(report: Report) -> str:
    lines: list[str] = []
    if report.connect_error:
        lines.append(f"Failed to connect: {report.connect_error}")
        return "\n".join(lines)

    lines.append(f"mcp-doctor fuzz --truth: {report.server_command}")
    if report.terminated_early:
        lines.append(f"WARNING: run ended early — {report.terminated_early}")
    lines.append("")
    if report.sanity_percent is not None:
        lines.append(
            f"Response sanity: {report.sanity_percent:.0f}% ({report.grade}) "
            f"— {report.flagged_count} flagged of {report.checkable_count} checkable response(s)"
        )
    else:
        lines.append("Response sanity: n/a (no tool had a checkable successful response)")
    lines.append(f"Tested {report.tested_count} tool(s), skipped {report.skipped_count} (not read-only)")
    lines.append("")

    for tool in report.tools:
        if not tool.tested:
            lines.append(f"  [skip] {tool.name} — {tool.skip_reason}")
            continue
        if tool.skip_reason is not None:
            lines.append(f"  [n/a]  {tool.name} — {tool.skip_reason}")
            continue
        flags = []
        if tool.refusal_in_disguise:
            flags.append(f'refusal in disguise: "{tool.refusal_in_disguise}"')
        if tool.empty_content:
            flags.append("empty content on success")
        if tool.schema_violation:
            flags.append(f"output schema violation: {tool.schema_violation}")
        marker = "FAIL" if tool.flagged else "ok"
        summary = f" — {'; '.join(flags)}" if flags else ""
        lines.append(f"  [{marker}]  {tool.name}{summary}")
        if tool.echo_mismatch_inputs:
            joined = ", ".join(f'"{s}"' for s in tool.echo_mismatch_inputs)
            lines.append(
                f"      note: response never mentions {joined} "
                "(worth a look, not scored — a genuine answer doesn't have to echo the input)"
            )

    return "\n".join(lines)


def to_dict(report: Report) -> dict:
    return {
        "server_command": report.server_command,
        "connect_error": report.connect_error,
        "terminated_early": report.terminated_early,
        "tested_count": report.tested_count,
        "skipped_count": report.skipped_count,
        "checkable_count": report.checkable_count,
        "flagged_count": report.flagged_count,
        "sanity_percent": report.sanity_percent,
        "grade": report.grade,
        "tools": [
            {
                "name": t.name,
                "tested": t.tested,
                "skip_reason": t.skip_reason,
                "refusal_in_disguise": t.refusal_in_disguise,
                "empty_content": t.empty_content,
                "schema_violation": t.schema_violation,
                "echo_mismatch_inputs": t.echo_mismatch_inputs,
                "flagged": t.flagged,
            }
            for t in report.tools
        ],
    }
