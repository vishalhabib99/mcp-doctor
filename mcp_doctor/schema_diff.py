"""Compares a current Report's per-tool parameter signatures against a
previously saved `--json` run to catch a breaking schema change an agent
that already learned the old shape wouldn't expect: a tool that's gone, a
parameter that's gone, or a parameter that's newly required.

Deliberately doesn't fold into the quality/security scores: those are
properties of a single snapshot, this is a property of *two* snapshots
compared — a fundamentally different kind of question, with its own
`--fail-on-breaking-change` exit code instead of `--fail-under`.

Python, TypeScript and Go since v1.27.0, Rust since v1.30.0. A tool's parameters are compared only
when both runs actually read them (`params_tracked`): a TS/Go tool in a
baseline written before v1.27.0, or one whose schema couldn't be resolved,
is skipped rather than compared as if it had no parameters. Type changes aren't
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

That fix still leaves param_names itself an incomplete subset whenever a
property name isn't a string literal (e.g. a variable used as a dict key) —
a genuinely-present parameter the analyzer just couldn't attribute a name
to. Diffing against a baseline that *did* see that name then reports it as
"removed": a second, related false positive Edward found once the first was
fixed. Guarded the same way required-ness already was: current.param_names
must be a complete snapshot before a name absent from it is trusted as
actually gone. A legacy baseline predating the `param_names_complete` field
defaults to complete, matching its pre-existing (correct) behavior.
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
        base_tracked = base_tool.get("params_tracked")
        if base_tracked is None:  # baseline from before v1.27.0: only Python was read
            base_tracked = not str(base_tool.get("file", "")).endswith(
                (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs"))
        if not (base_tracked and current.params_tracked):
            continue
        base_params = set(base_tool.get("param_names", []))
        current_params = set(current.param_names)
        base_required = set(base_tool.get("required_param_names", []))
        current_required = set(current.required_param_names)

        if current.param_names_complete:
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
