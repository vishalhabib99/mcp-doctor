"""Compares a current Report's per-tool parameter signatures against a
previously saved `--json` run to catch a breaking schema change an agent
that already learned the old shape wouldn't expect: a tool that's gone, a
parameter that's gone, or a parameter that's newly required.

Deliberately doesn't fold into the quality/security scores: those are
properties of a single snapshot, this is a property of *two* snapshots
compared — a fundamentally different kind of question, with its own
`--fail-on-breaking-change` exit code instead of `--fail-under`.

Python-only for now, same incremental language-by-language pattern as the
rest of this analyzer: a TS/Go tool's `param_names`/`required_param_names`
are empty in both the baseline and the current run, so the comparison is
naturally a no-op there rather than a false positive. Type changes aren't
checked either — presence and required-ness alone already catch the two
most common breaking-change classes (a removed parameter, a
newly-required one) without needing to resolve and compare arbitrary
cross-language type annotations.

Coverage is symmetric within Python too now: both the decorator/class-based
styles and the raw `Tool(inputSchema=...)` constructor style populate
`param_names`/`required_param_names` (analyzer.py resolves the latter
statically from the schema dict, falling back to empty when a property name
isn't resolvable). A one-sided real bug lived here before that fix: baseline
captured with one style and current captured with the other style could
report every unchanged shared parameter as `param_removed`, because "empty"
was silently conflated with "genuinely no parameters" instead of "coverage
unknown" (reported by Edward Izgorodin, github.com/modelcontextprotocol/
modelcontextprotocol#3322).
"""

from __future__ import annotations

from dataclasses import dataclass

from .analyzer import ToolFinding


@dataclass
class SchemaChange:
    tool: str
    change: str  # "tool_removed" | "param_removed" | "param_newly_required"
    detail: str
    severity: str  # "error" (breaking) | "info" (safe addition, not currently emitted)


def compute_schema_diff(baseline: dict, current_tools: list[ToolFinding]) -> list[SchemaChange]:
    changes: list[SchemaChange] = []
    baseline_tools = {t["name"]: t for t in baseline.get("tools", [])}
    current_by_name = {t.name: t for t in current_tools}

    for name, base_tool in baseline_tools.items():
        if name not in current_by_name:
            changes.append(SchemaChange(
                name, "tool_removed",
                "This tool existed in the baseline and is gone now — an agent that already "
                "learned it can no longer call it.",
                "error",
            ))
            continue

        current = current_by_name[name]
        base_params = set(base_tool.get("param_names", []))
        current_params = set(current.param_names)
        base_required = set(base_tool.get("required_param_names", []))
        current_required = set(current.required_param_names)

        for removed in sorted(base_params - current_params):
            changes.append(SchemaChange(
                name, "param_removed",
                f"Parameter '{removed}' existed in the baseline and is gone now — a caller "
                "still passing it will get rejected.",
                "error",
            ))

        for newly_required in sorted(current_required - base_required):
            changes.append(SchemaChange(
                name, "param_newly_required",
                f"Parameter '{newly_required}' is now required but wasn't in the baseline "
                "(either brand new, or previously optional) — a caller built against the old "
                "schema may not supply it.",
                "error",
            ))

    return changes
