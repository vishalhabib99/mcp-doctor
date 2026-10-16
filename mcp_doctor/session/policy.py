"""A policy the agent's operator writes: which tools the agent may call, with which arguments,
and which calls need a person's approval first. Enforced by GuardedSession BEFORE the call is
made, so a denied call never reaches the server.

This is the runtime-authorization piece the trilogy used to decline because "out of scope"
means something different on every server. Here the operator defines scope explicitly, so
nothing is guessed.

    {
      "allow_tools": ["read_graph", "search_nodes"],   # optional; if set, anything else is denied
      "deny_tools": ["delete_entities"],               # always denied, even if allowed above
      "require_approval": ["create_entities"],         # called only with approved=True
      "require_approval_for_destructive": true,        # any tool not annotated readOnlyHint=true
      "arguments": {                                   # per-tool argument rules
        "search_nodes": {"query": {"pattern": "[A-Za-z0-9 ]{1,64}"}},
        "set_mode": {"mode": {"enum": ["safe", "dry-run"]}}
      },
      "pii": ["card", "ssn", "iban"]                   # detectors run on every response
    }

Unknown keys are an error, never ignored: a misspelled "deny_tool" that silently allowed
everything would be the worst way for a policy to fail.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from mcp_doctor.session.pii import ALL_DETECTORS, DEFAULT_DETECTORS

_TOP_KEYS = {"allow_tools", "deny_tools", "require_approval", "require_approval_for_destructive", "arguments", "pii"}
_RULE_KEYS = {"pattern", "enum"}


class PolicyError(ValueError):
    pass


@dataclass
class ArgRule:
    pattern: re.Pattern | None = None
    enum: list | None = None


@dataclass
class Policy:
    allow_tools: set[str] | None = None
    deny_tools: set[str] = field(default_factory=set)
    require_approval: set[str] = field(default_factory=set)
    require_approval_for_destructive: bool = False
    arguments: dict[str, dict[str, ArgRule]] = field(default_factory=dict)
    pii: tuple[str, ...] = DEFAULT_DETECTORS

    @classmethod
    def from_dict(cls, data: dict) -> "Policy":
        if not isinstance(data, dict):
            raise PolicyError("policy must be a JSON object")
        unknown = set(data) - _TOP_KEYS
        if unknown:
            raise PolicyError(f"unknown policy key(s) {sorted(unknown)}; allowed: {sorted(_TOP_KEYS)}")

        def names(key: str) -> set[str]:
            value = data.get(key, [])
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise PolicyError(f"'{key}' must be a list of tool names")
            return set(value)

        flag = data.get("require_approval_for_destructive", False)
        if not isinstance(flag, bool):
            raise PolicyError("'require_approval_for_destructive' must be true or false")

        arguments: dict[str, dict[str, ArgRule]] = {}
        raw_args = data.get("arguments", {})
        if not isinstance(raw_args, dict):
            raise PolicyError("'arguments' must map tool names to {argument: rule}")
        for tool, rules in raw_args.items():
            if not isinstance(rules, dict):
                raise PolicyError(f"arguments for '{tool}' must map argument names to rules")
            arguments[tool] = {}
            for arg, rule in rules.items():
                if not isinstance(rule, dict) or not rule:
                    raise PolicyError(f"rule for {tool}.{arg} must be an object with 'pattern' and/or 'enum'")
                bad = set(rule) - _RULE_KEYS
                if bad:
                    raise PolicyError(f"unknown rule key(s) {sorted(bad)} for {tool}.{arg}; allowed: {sorted(_RULE_KEYS)}")
                pattern = None
                if "pattern" in rule:
                    try:
                        pattern = re.compile(rule["pattern"])
                    except (re.error, TypeError) as exc:
                        raise PolicyError(f"invalid pattern for {tool}.{arg}: {exc}") from exc
                enum = rule.get("enum")
                if enum is not None and not isinstance(enum, list):
                    raise PolicyError(f"'enum' for {tool}.{arg} must be a list")
                arguments[tool][arg] = ArgRule(pattern=pattern, enum=enum)

        pii = data.get("pii", list(DEFAULT_DETECTORS))
        if not isinstance(pii, list) or set(pii) - set(ALL_DETECTORS):
            raise PolicyError(f"'pii' must be a list drawn from {list(ALL_DETECTORS)} (use [] to turn detection off)")

        return cls(
            allow_tools=names("allow_tools") if "allow_tools" in data else None,
            deny_tools=names("deny_tools"),
            require_approval=names("require_approval"),
            require_approval_for_destructive=flag,
            arguments=arguments,
            pii=tuple(pii),
        )

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        try:
            data = json.loads(Path(path).read_text())
        except json.JSONDecodeError as exc:
            raise PolicyError(f"{path} is not valid JSON: {exc}") from exc
        return cls.from_dict(data)

    def check(self, tool: str, arguments: dict, read_only: bool, approved: bool) -> tuple[str | None, list[str]]:
        """Before the call. Returns (None, []) to allow, ("BLOCK", reasons) to deny, or
        ("ESCALATE", reasons) when a person has to approve first."""
        if tool in self.deny_tools:
            return "BLOCK", [f"policy: '{tool}' is in deny_tools"]
        if self.allow_tools is not None and tool not in self.allow_tools:
            return "BLOCK", [f"policy: '{tool}' is not in allow_tools"]

        violations: list[str] = []
        for arg, rule in self.arguments.get(tool, {}).items():
            if arg not in arguments:
                continue  # the tool's own schema decides whether an argument is required
            value = arguments[arg]
            if rule.enum is not None and value not in rule.enum:
                violations.append(f"policy: {tool}.{arg}={value!r} is not one of {rule.enum}")
            if rule.pattern is not None and not rule.pattern.fullmatch(str(value)):
                violations.append(f"policy: {tool}.{arg} doesn't match {rule.pattern.pattern!r}")
        if violations:
            return "BLOCK", violations

        if not approved:
            if tool in self.require_approval:
                return "ESCALATE", [f"policy: '{tool}' requires approval (call again with approved=True)"]
            if self.require_approval_for_destructive and not read_only:
                return "ESCALATE", [
                    f"policy: '{tool}' isn't annotated readOnlyHint=true, and destructive tools require "
                    "approval (call again with approved=True)"
                ]
        return None, []
