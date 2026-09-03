"""Security checks, separate from the spec-conformance/quality checks in
``analyzer.py``.

These scan for patterns that matter specifically because an MCP tool is
invoked autonomously by an LLM, not a human reviewing a diff: a tool
description that itself reads as an instruction to the model ("tool
poisoning" / prompt injection via metadata an agent can't distinguish from
real conversation context), and the usual dangerous-primitive risks
(arbitrary code execution, unvalidated outbound requests, unsafe
deserialization) that are worse here because the input triggering them can
originate from model-generated tool-call arguments, not just a human user.

Same design constraints as the rest of the analyzer: line-regex over raw
source text (not per-language AST), cross-language by construction, no new
dependencies, and honest about which checks are precise vs. heuristic.
"""

from __future__ import annotations

import re
from pathlib import Path

from .analyzer import RepoIssue, ToolFinding, ToolIssue, _is_test_file

# --- Prompt injection / tool poisoning ---------------------------------
#
# A tool description is metadata an agent typically trusts the same way it
# trusts system instructions — it's shown to the model as "here's what this
# tool does," not as untrusted user input. A description that itself issues
# directives (to ignore prior instructions, to always take some additional
# action, to impersonate a system message) is a real, documented MCP threat
# category ("tool poisoning"), not a hypothetical.
_INJECTION_PHRASES = [
    re.compile(r"ignore (all |any )?(previous|prior|above) instructions", re.IGNORECASE),
    re.compile(r"disregard (all |any )?(previous|prior|above)", re.IGNORECASE),
    re.compile(r"you must always", re.IGNORECASE),
    re.compile(r"^\s*system\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"do not (tell|inform|mention (this|it) to) the user", re.IGNORECASE),
    re.compile(r"before (calling|using|responding).{0,40}always (call|use|run)", re.IGNORECASE),
]

# A legitimate tool description is a sentence or two. A description running
# past this is unusual and a common way to smuggle instructions an agent
# will read in full but a human skimming a tool list will not.
_SUSPICIOUS_DESCRIPTION_LENGTH = 500


def scan_prompt_injection(tools: list[ToolFinding]) -> None:
    """Mutates each ``ToolFinding.issues`` in place, same as every per-tool
    check in ``analyzer.py`` — these findings are attributed to a specific
    tool, so they belong on that tool's own issue list, not a separate
    repo-level list."""
    for t in tools:
        text = t.description_text
        if not text:
            continue
        for pattern in _INJECTION_PHRASES:
            if pattern.search(text):
                t.issues.append(ToolIssue(
                    t.name, t.file, t.line, "prompt_injection",
                    "Tool description contains language that reads as an instruction to the "
                    "model ('tool poisoning') rather than a description of what the tool does — "
                    "an agent can't distinguish this from a legitimate system instruction.",
                    "error", "security",
                ))
                break
        if len(text) > _SUSPICIOUS_DESCRIPTION_LENGTH:
            t.issues.append(ToolIssue(
                t.name, t.file, t.line, "prompt_injection",
                f"Description is unusually long ({len(text)} chars) for a tool description — "
                "worth a manual read, since this is a common way to smuggle hidden instructions "
                "past a human skimming the tool list.",
                "warning", "security",
            ))


# --- Dangerous dynamic execution ---------------------------------------

_DANGEROUS_EXEC_PATTERNS = [
    re.compile(r"\beval\s*\("),
    re.compile(r"\bexec\s*\("),
    re.compile(r"\bos\.system\s*\("),
    re.compile(r"\bsubprocess\.(Popen|call|run|check_output)\s*\("),
    re.compile(r"\bchild_process\.exec(Sync)?\s*\("),
    re.compile(r"\bexec\.Command\s*\("),  # Go os/exec
]


def scan_dangerous_exec(files: list[Path]) -> list[RepoIssue]:
    issues = []
    for f in files:
        if _is_test_file(f):
            continue
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if any(p.search(line) for p in _DANGEROUS_EXEC_PATTERNS):
                issues.append(RepoIssue(
                    "dangerous_exec",
                    f"{f.name}:{i} calls a dynamic-execution/shell primitive (eval/exec/subprocess/"
                    "os.system/child_process.exec) — if any part of the command or code string can "
                    "trace back to a tool argument, this is arbitrary code execution triggered by "
                    "model output.",
                    "error", "security",
                ))
    return issues


# --- SSRF-prone outbound requests ---------------------------------------
#
# Heuristic, not precise: flags an outbound HTTP call whose URL argument is a
# bare variable rather than a string literal. A literal URL can't be an SSRF
# vector; a variable *might* be tool-input-derived, but might just as well be
# a validated config value — this check can't tell the difference from text
# alone, so it's a warning to go look, not a confirmed finding. Documented
# as heuristic in the README, same honesty standard as every other check.
_HTTP_CALL_WITH_VAR_ARG = [
    re.compile(r"\brequests\.(get|post|put|delete|head|patch)\s*\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s*[,)]"),
    re.compile(r"\burllib\.request\.urlopen\s*\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s*[,)]"),
    re.compile(r"\bfetch\s*\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s*[,)]"),
    re.compile(r"\baxios\.(get|post|put|delete|head)\s*\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s*[,)]"),
    re.compile(r"\bhttp\.Get\s*\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\)"),
]


def scan_ssrf(files: list[Path]) -> list[RepoIssue]:
    issues = []
    for f in files:
        if _is_test_file(f):
            continue
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if any(p.search(line) for p in _HTTP_CALL_WITH_VAR_ARG):
                issues.append(RepoIssue(
                    "ssrf",
                    f"{f.name}:{i} makes an outbound HTTP request with a non-literal URL — worth "
                    "checking whether that value can originate from a tool argument (possible SSRF) "
                    "or is a fixed/validated config value (heuristic — flags variable URLs generally, "
                    "doesn't trace where the value actually comes from).",
                    "warning", "security",
                ))
    return issues


# --- Unsafe deserialization (Python) -------------------------------------

_UNSAFE_DESERIALIZATION_PATTERNS = [
    re.compile(r"\bpickle\.loads?\s*\("),
    re.compile(r"\bmarshal\.loads?\s*\("),
]
_YAML_LOAD = re.compile(r"\byaml\.load\s*\(")
_YAML_SAFE_LOADER = re.compile(r"Loader\s*=\s*yaml\.SafeLoader")


def scan_unsafe_deserialization(py_files: list[Path]) -> list[RepoIssue]:
    issues = []
    for f in py_files:
        if _is_test_file(f):
            continue
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if any(p.search(line) for p in _UNSAFE_DESERIALIZATION_PATTERNS):
                issues.append(RepoIssue(
                    "unsafe_deserialization",
                    f"{f.name}:{i} uses pickle/marshal to deserialize data — unpickling untrusted "
                    "bytes (including anything derived from a tool argument) can execute arbitrary "
                    "code.",
                    "error", "security",
                ))
            elif _YAML_LOAD.search(line) and not _YAML_SAFE_LOADER.search(line):
                issues.append(RepoIssue(
                    "unsafe_deserialization",
                    f"{f.name}:{i} calls yaml.load() without Loader=yaml.SafeLoader — the default "
                    "loader can construct arbitrary Python objects from the YAML content.",
                    "error", "security",
                ))
    return issues
