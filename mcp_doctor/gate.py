"""Runtime counterpart to the description-quality and annotation-
contradiction checks in `analyzer.py`: applies the same rules to a tool's
live `tools/list` metadata — its name, description, and annotations
exactly as a client actually receives them — instead of parsing the
server's Python/TS/Go source.

Deliberately dependency-free and connection-free, unlike the sibling
gates in `mcp-fuzz`/`mcp-reality-check`: this package has never made a
live call to anything, and this feature doesn't change that. Pass in the
tool metadata your own MCP client already fetched (a plain dict shaped
like the raw `tools/list` JSON, or an SDK object such as `mcp.types.Tool`
via duck typing) — nothing here opens a connection, times anything, or
needs the `mcp` package installed at all.

Also narrower than the full static analyzer, honestly: the read-only/
mutation-signal-mismatch check (does this tool's *own code* actually
write despite claiming `readOnlyHint: true`) has no live counterpart —
an agent discovering tools at runtime has no access to server source,
only what the server declares about itself. What IS checkable from
declared metadata alone: is the tool documented well enough for an agent
to decide whether to call it, and is its own self-declared annotations
internally consistent.

The annotation_contradiction rule itself is deliberately NOT a direct
port of the static check's "destructiveHint present at all, regardless
of value" rule — dogfooding against the real official
`@modelcontextprotocol/server-memory` reference server found that rule
miscalibrated for live data before it ever shipped: that server declares
all four ToolAnnotations fields explicitly on every tool as a matter of
good, complete practice (read_graph: readOnlyHint=true, destructiveHint
=false — sensible and consistent), which the presence-based rule flagged
as contradictory anyway. Presence is a fair proxy for "leftover" in
source code, where an author who didn't think about a field usually
doesn't write it — but at the wire level, a careful implementer setting
every field explicitly is common and not a bug. What's still a genuine,
low-false-positive signal: `destructiveHint` explicitly `true` alongside
`readOnlyHint: true` — an actual conflicting *value*, not mere
co-presence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .analyzer import description_display_width

VAGUE_DESCRIPTION_MIN_CHARS = 10


@dataclass
class RegistrationIssue:
    check: str
    message: str
    severity: str  # "error" | "warning"
    category: str = "quality"  # "quality" | "security"


@dataclass
class RegistrationResult:
    tool_name: str
    issues: list[RegistrationIssue] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return bool(self.issues)


def _get(obj, *names: str):
    """Reads the first of `names` present on `obj`, whether `obj` is a
    plain dict (raw tools/list JSON, camelCase keys) or an SDK object with
    snake_case attributes."""
    if isinstance(obj, dict):
        for name in names:
            if name in obj:
                return obj[name]
        return None
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


def check_tool_registration(name: str, description: str | None, annotations=None) -> RegistrationResult:
    """Checks one tool's metadata exactly as it arrives from a real
    `tools/list` call — before any tool call, unlike `mcp_reality_check.
    gate`/`mcp_fuzz.gate`, which judge what happens *after* a call. Call
    once per tool at discovery time, before deciding whether to expose it
    to an agent."""
    result = RegistrationResult(name)
    text = (description or "").strip()

    if not text:
        result.issues.append(RegistrationIssue(
            "description",
            "Tool has no description. An agent cannot decide when to call this.",
            "error",
        ))
    elif description_display_width(text) < VAGUE_DESCRIPTION_MIN_CHARS:
        result.issues.append(RegistrationIssue(
            "description",
            f"Description is only {len(text)} chars — likely just restates the name.",
            "warning",
        ))

    if annotations is not None:
        read_only = _get(annotations, "read_only_hint", "readOnlyHint")
        destructive = _get(annotations, "destructive_hint", "destructiveHint")
        if read_only is True and destructive is True:
            result.issues.append(RegistrationIssue(
                "annotation_contradiction",
                "Declared readOnlyHint: true and destructiveHint: true — the spec defines "
                "destructiveHint as meaningful only when readOnlyHint is false, so this combination "
                "is self-contradictory. Common when a new tool is cloned from an existing write-tool's "
                "annotation block and only readOnlyHint gets flipped to true, leaving a stale "
                "destructiveHint: true behind.",
                "warning", "security",
            ))

    return result


def check_tools_distinguishable(tools) -> dict[str, list[RegistrationIssue]]:
    """Cross-tool check on a whole live tools/list (SDK Tool objects or raw dicts): tools whose
    descriptions are indistinguishable. Returns {tool name: issues}; call once at discovery,
    next to check_tool_registration."""
    from .lookalike import find_indistinguishable_tools, message

    pairs = find_indistinguishable_tools([(_get(t, "name"), _get(t, "description")) for t in tools])
    found: dict[str, list[RegistrationIssue]] = {}
    for a, b in pairs:
        for this, other in ((a, b), (b, a)):
            found.setdefault(this, []).append(RegistrationIssue("indistinguishable_description", message(other), "warning"))
    return found
