"""Live counterpart to what `action.yml`/`combine.py` already do for the
batch/static case: run mcp-doctor's checks always, mcp-fuzz's and
mcp-reality-check's on every real call — but for an agent's actual
session, not a CI run.

`GuardedSession` wraps a real `ClientSession` and composes all three
trilogy tools' live gates at their natural point: mcp-doctor's
registration checks once at discovery (`list_tools`), mcp-fuzz's latency/
size gate and mcp-reality-check's correctness checks together on every
real call (`call_tool`) — **one** real network call per tool call, not
three, by construction. `mcp_doctor.fuzz.gate.guarded_call` and `mcp_reality_
check.gate.LatencyGate.timed_call` (well, the reverse — see each
package's own gate module) each make their own call-and-check round
trip; calling both here would mean two real network calls per logical
call, which for a non-idempotent or destructive tool isn't just
wasteful, it's actively wrong. Instead, this makes the call exactly once
and fans the one real response out to each sibling package's own already-
pure, already-tested per-response functions (`LatencyGate.record`,
`mcp_doctor.reality.checks.*`) — the same functions their own wrappers
call internally, reused here instead of duplicated.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from mcp import ClientSession, types

from mcp_doctor.gate import RegistrationResult, check_tool_registration, check_tools_distinguishable
from mcp_doctor.fuzz.gate import MIN_CALLS_FOR_LATENCY_OUTLIER, LatencyGate
from mcp_doctor.reality.checks import (
    check_echo_mismatch,
    check_empty_content,
    check_output_schema,
    check_refusal_in_disguise,
    response_text_from_content,
)
from mcp_doctor.reality.generator import string_argument_values

from mcp_doctor.session.audit import AuditLog
from mcp_doctor.session.pii import DEFAULT_DETECTORS, find_pii
from mcp_doctor.session.policy import Policy

DEFAULT_TIMEOUT_SECONDS = 15.0

# Per-call decision, the live counterpart of decision.py's SHIP / FIX-FIRST / BLOCK. What the agent
# should do with this one response: use it, hand it to a person, or stop.
ACT = "ACT"
ESCALATE = "ESCALATE"
BLOCK = "BLOCK"

HIGH = "HIGH"
MEDIUM = "MEDIUM"
LOW = "LOW"
_RANK = {LOW: 0, MEDIUM: 1, HIGH: 2}


def _field(model, snake_name: str, camel_name: str):
    """Same cross-`mcp`-version compat helper duplicated in each sibling
    package — kept local rather than imported from one of them, since none
    of the three treats it as public API."""
    if hasattr(model, snake_name):
        return getattr(model, snake_name)
    return getattr(model, camel_name)


@dataclass
class GuardedCallResult:
    tool_name: str
    # "ok" | "crash" | "timeout"
    outcome: str
    detail: str = ""
    is_error: bool = False
    response_text: str = ""
    duration_ms: float = 0.0
    response_chars: int = 0
    # correctness (mcp-reality-check)
    refusal_in_disguise: str | None = None
    empty_content: bool = False
    schema_violation: str | None = None
    echo_mismatch_inputs: list[str] | None = None
    # latency/size (mcp-fuzz)
    slow_reasons: list[str] = field(default_factory=list)
    bloated_reasons: list[str] = field(default_factory=list)
    # PII found in the response (masked, e.g. "card ************4242")
    pii_findings: list[str] = field(default_factory=list)
    # False when the policy stopped the call before it reached the server
    called: bool = True
    # Lowest confidence at which an ACT may go ahead without a person (GuardedSession(act_threshold=...))
    act_threshold: str = MEDIUM

    @property
    def flagged(self) -> bool:
        return bool(
            self.refusal_in_disguise or self.empty_content or self.schema_violation
            or self.slow_reasons or self.bloated_reasons
        )

    # Filled in by GuardedSession.call_tool: ACT / ESCALATE / BLOCK, how sure, and why.
    decision: str = ACT
    confidence: str = HIGH
    reasons: list[str] = field(default_factory=list)

    @property
    def should_act(self) -> bool:
        """True for an ACT whose confidence is at least act_threshold. Like Jev, the check
        reports how sure it is and the operator decides how sure is sure enough.

        The default, MEDIUM, is looser than the release decision's needs-human-review on
        purpose: live, MEDIUM mostly means "no speed baseline yet" for a tool's first few
        calls, and escalating every early call would make the gate useless. The correctness
        checks run either way. A strict deployment can pass act_threshold=HIGH."""
        return self.decision == ACT and _RANK[self.confidence] >= _RANK[self.act_threshold]


def decide_call(
    result: GuardedCallResult,
    prior_calls: int,
    known_tool: bool,
    registration_flagged: bool,
) -> tuple[str, str, list[str]]:
    """Pure function: one call's findings in, (decision, confidence, reasons) out. The same
    split TypeSafe's Jev makes between an answer and whether to act on it, from rules, not a
    model. Confidence is how much this call could actually be checked, never a probability."""
    blockers: list[str] = []
    if result.outcome == "crash":
        blockers.append(f"crashed ({result.detail})")
    elif result.outcome == "timeout":
        blockers.append(f"timed out ({result.detail})")
    if result.refusal_in_disguise:
        # The agent would read a failure as success and build on it.
        blockers.append(f"disguised refusal: {result.refusal_in_disguise}")
    if blockers:
        return BLOCK, HIGH, blockers

    reasons: list[str] = []
    if result.empty_content:
        reasons.append("empty content on a successful call")
    if result.schema_violation:
        reasons.append(f"output breaks its own schema: {result.schema_violation}")
    reasons.extend(f"slow: {r}" for r in result.slow_reasons)
    reasons.extend(f"large response: {r}" for r in result.bloated_reasons)
    if result.pii_findings:
        # The data already left the server; a person decides whether the agent may use it.
        reasons.append(f"PII in response: {', '.join(result.pii_findings)}")
    decision = ESCALATE if reasons else ACT
    if result.is_error:
        reasons.append("the tool returned an honest error (isError), safe for the agent to handle")

    confidence = HIGH
    if not known_tool:
        confidence = LOW
        reasons.append("tool wasn't in tools/list (call list_tools first), so its output schema and registration weren't checked")
    else:
        if prior_calls < MIN_CALLS_FOR_LATENCY_OUTLIER:
            confidence = MEDIUM
            reasons.append(
                f"{prior_calls} earlier call(s) to this tool, so there's no speed/size baseline yet "
                f"(needs {MIN_CALLS_FOR_LATENCY_OUTLIER})"
            )
        if registration_flagged:
            confidence = MEDIUM
            reasons.append("mcp-doctor flagged this tool at registration (vague or look-alike description, or an annotation conflict)")
    return decision, confidence, reasons


class GuardedSession:
    """One `ClientSession` per instance — `LatencyGate`'s per-tool history
    accumulates across every `call_tool` made through this wrapper, same
    as using `mcp_doctor.fuzz.gate.LatencyGate` directly."""

    def __init__(
        self,
        session: ClientSession,
        policy: Policy | None = None,
        audit_log: AuditLog | str | None = None,
        act_threshold: str = MEDIUM,
    ):
        """`policy` is enforced before every call (see mcp_doctor.session.policy); without one, all
        calls go through and responses are scanned with the default PII detectors. `audit_log`
        is an AuditLog or a path; every call, including ones the policy stopped, gets a
        hash-chained record. `act_threshold` (HIGH / MEDIUM / LOW, default MEDIUM) is the lowest
        confidence at which `should_act` lets an ACT through."""
        if act_threshold not in _RANK:
            raise ValueError(f"act_threshold must be one of {list(_RANK)}, got {act_threshold!r}")
        self.act_threshold = act_threshold
        self._session = session
        self.policy = policy
        self._pii_detectors = policy.pii if policy is not None else DEFAULT_DETECTORS
        self.audit_log = AuditLog(audit_log) if isinstance(audit_log, (str, Path)) else audit_log
        self._latency_gate = LatencyGate()
        self._tools_by_name: dict[str, types.Tool] = {}
        self.registration_results: dict[str, RegistrationResult] = {}
        self._call_counts: dict[str, int] = {}

    async def list_tools(self):
        """Calls the real `tools/list` and runs mcp-doctor's registration
        checks on every tool once, at discovery — before any tool is ever
        called. Results are cached on `.registration_results`, keyed by
        tool name, and reused by `call_tool` below for output-schema
        checking without needing a second `tools/list` round trip."""
        result = await self._session.list_tools()
        self._tools_by_name = {t.name: t for t in result.tools}
        self.registration_results = {
            t.name: check_tool_registration(t.name, t.description, t.annotations)
            for t in result.tools
        }
        # Cross-tool: descriptions an agent can't tell apart. Flags both tools, so every call to
        # either one drops to MEDIUM confidence.
        for name, issues in check_tools_distinguishable(result.tools).items():
            self.registration_results[name].issues.extend(issues)
        return result

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        approved: bool = False,
    ) -> GuardedCallResult:
        """Calls `tool_name` via the wrapped session exactly once, and
        checks the one real response for both latency/size (mcp-fuzz) and
        correctness (mcp-reality-check) issues. Use in place of a bare
        `session.call_tool(...)`. If a policy is set, it's checked first and
        a denied or unapproved call is never sent (`called` is False).
        `approved=True` means a person approved this call."""
        if self.policy is not None:
            verdict, policy_reasons = self.policy.check(
                tool_name, arguments, read_only=self._is_read_only(tool_name), approved=approved
            )
            if verdict is not None:
                stopped = GuardedCallResult(tool_name, "not_called", called=False, act_threshold=self.act_threshold)
                stopped.decision, stopped.confidence, stopped.reasons = verdict, HIGH, policy_reasons
                self._audit(stopped, arguments)
                return stopped

        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(tool_name, arguments), timeout=timeout
            )
        except asyncio.TimeoutError:
            return self._decide(GuardedCallResult(tool_name, "timeout", detail=f"no response within {timeout}s"), arguments)
        except Exception as exc:
            return self._decide(GuardedCallResult(tool_name, "crash", detail=f"{type(exc).__name__}: {exc}"), arguments)
        duration_ms = (time.monotonic() - started) * 1000

        is_error = bool(_field(result, "is_error", "isError")) if isinstance(result, types.CallToolResult) else False
        response_text = response_text_from_content(result.content) if hasattr(result, "content") else ""

        guarded = GuardedCallResult(
            tool_name, "ok", is_error=is_error, response_text=response_text,
            duration_ms=duration_ms, response_chars=len(response_text),
        )

        prior_calls = self._call_counts.get(tool_name, 0)
        self._call_counts[tool_name] = prior_calls + 1
        latency_result = self._latency_gate.record(tool_name, duration_ms, len(response_text))
        guarded.slow_reasons = latency_result.slow_reasons
        guarded.bloated_reasons = latency_result.bloated_reasons

        if not is_error:
            guarded.refusal_in_disguise = check_refusal_in_disguise(response_text)
            guarded.empty_content = check_empty_content(response_text)
            guarded.echo_mismatch_inputs = check_echo_mismatch(response_text, string_argument_values(arguments))

            tool = self._tools_by_name.get(tool_name)
            if tool is not None:
                output_schema = _field(tool, "output_schema", "outputSchema")
                structured = _field(result, "structured_content", "structuredContent")
                guarded.schema_violation = check_output_schema(structured, output_schema)

        # Scanned even on isError: an error message can leak a record just as well.
        scanned = response_text
        structured = _field(result, "structured_content", "structuredContent") if hasattr(result, "content") else None
        if structured is not None:
            scanned += "\n" + json.dumps(structured, default=str)
        # Deduped: servers often send the same answer twice (text content and structuredContent).
        guarded.pii_findings = list(dict.fromkeys(str(f) for f in find_pii(scanned, self._pii_detectors)))

        return self._decide(guarded, arguments, prior_calls)

    def _is_read_only(self, tool_name: str) -> bool:
        # A tool that never went through list_tools counts as not read-only: with
        # require_approval_for_destructive, an unknown tool needs approval rather than slipping through.
        tool = self._tools_by_name.get(tool_name)
        annotations = tool.annotations if tool is not None else None
        if annotations is None:
            return False
        return bool(_field(annotations, "read_only_hint", "readOnlyHint"))

    def _decide(self, guarded: GuardedCallResult, arguments: dict, prior_calls: int | None = None) -> GuardedCallResult:
        name = guarded.tool_name
        guarded.act_threshold = self.act_threshold
        registration = self.registration_results.get(name)
        guarded.decision, guarded.confidence, guarded.reasons = decide_call(
            guarded,
            prior_calls=self._call_counts.get(name, 0) if prior_calls is None else prior_calls,
            known_tool=name in self._tools_by_name,
            registration_flagged=bool(registration and registration.flagged),
        )
        self._audit(guarded, arguments)
        return guarded

    def _audit(self, guarded: GuardedCallResult, arguments: dict) -> None:
        if self.audit_log is None:
            return
        self.audit_log.write(
            tool=guarded.tool_name, arguments=arguments, called=guarded.called,
            outcome=guarded.outcome, decision=guarded.decision, confidence=guarded.confidence,
            reasons=guarded.reasons, duration_ms=guarded.duration_ms,
        )
