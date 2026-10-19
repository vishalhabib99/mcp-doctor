"""Draft parameter descriptions for ``--fix --llm`` in place of the
``TODO: describe this parameter.`` placeholders.

What the code states for certain is written by rule: the values of a
``Literal[...]`` or a same-file ``Enum``, and a constant default. A local
model (Ollama, nothing leaves the machine) reads only the tool function's
source and writes what the parameter is for. When an agent would need a
format the code doesn't show (an id's shape, a date format, JSON keys,
units), the entry keeps a TODO for the maintainer rather than a guess.

Measured before building (2026-10-15, qwen3:8b, 20 real undocumented
params): 18/20 correct against the code, 20/20 parse and clear the finding.
Every draft is still a suggestion: --diff shows it, a person commits it.
"""

from __future__ import annotations

import ast
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_MODEL = "qwen3:8b"
FORMAT_TODO = "TODO: state the expected format."
_MAX_SOURCE_CHARS = 6000
_MAX_DESCRIPTION_CHARS = 220

_PROMPT = """You write one parameter description for an MCP tool's docstring (a Google-style Args: entry).
Describe ONLY what the function source shows: what the parameter is for, and its format or shape when the code constrains it.
Do not invent behavior. Do not list allowed values or the default; those are added separately.
Answer with JSON only: {{"description": "<one line, max 30 words, no parameter name prefix>", "format_needed": <true|false>}}
Set format_needed to true only when an agent calling this tool would need a specific format, shape or unit (an id format, a date format, JSON keys, units) that the source does NOT show.

Function source:
```python
{source}
```
Parameter: {param} (annotation: {annotation})"""


class LLMUnavailable(RuntimeError):
    pass


def _ollama_host() -> str:
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    return host if "://" in host else f"http://{host}"


def _post(path: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        _ollama_host() + path, json.dumps(body).encode(), {"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def check_ollama(model: str) -> None:
    """Fail early, with how to fix it, when Ollama isn't running or lacks the model."""
    try:
        req = urllib.request.Request(_ollama_host() + "/api/tags")
        with urllib.request.urlopen(req, timeout=5) as resp:
            names = {m.get("name", "") for m in json.load(resp).get("models", [])}
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise LLMUnavailable(
            f"--llm needs a local Ollama server at {_ollama_host()} ({e}). "
            f"Start it with `ollama serve`, then `ollama pull {model}`; set OLLAMA_HOST for another address."
        ) from None
    if model not in names and f"{model}:latest" not in names:
        raise LLMUnavailable(f"Ollama doesn't have {model}. Run `ollama pull {model}`, or pick one with --llm-model.")


def _unwrap(annotation: ast.expr | None) -> list[ast.expr]:
    """The member types of an annotation, through Optional/Union/Annotated/`X | None`."""
    if annotation is None:
        return []
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _unwrap(annotation.left) + _unwrap(annotation.right)
    if isinstance(annotation, ast.Subscript):
        base = annotation.value
        name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
        sl = annotation.slice
        elts = list(sl.elts) if isinstance(sl, ast.Tuple) else [sl]
        if name == "Annotated":
            return _unwrap(elts[0])
        if name in ("Optional", "Union"):
            return [m for e in elts for m in _unwrap(e)]
    return [annotation]


def _literal_values(annotation: ast.expr | None, tree: ast.Module) -> list[object]:
    values: list[object] = []
    for member in _unwrap(annotation):
        if isinstance(member, ast.Subscript):
            base = member.value
            name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
            if name == "Literal":
                sl = member.slice
                for e in (sl.elts if isinstance(sl, ast.Tuple) else [sl]):
                    if not isinstance(e, ast.Constant):
                        return []
                    values.append(e.value)
        elif isinstance(member, ast.Name):
            values.extend(_enum_values(member.id, tree))
    return values


def _enum_values(class_name: str, tree: ast.Module) -> list[object]:
    """Values of an Enum class defined in the same file; empty if it isn't one."""
    for node in tree.body:
        if not (isinstance(node, ast.ClassDef) and node.name == class_name):
            continue
        bases = {b.attr if isinstance(b, ast.Attribute) else getattr(b, "id", "") for b in node.bases}
        if not bases & {"Enum", "StrEnum", "IntEnum"}:
            return []
        values = []
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
                values.append(stmt.value.value)
            elif isinstance(stmt, ast.Assign):
                return []  # auto() or a computed value: the code doesn't state it
        return values
    return []


def _default_of(fn: ast.FunctionDef | ast.AsyncFunctionDef, arg_name: str) -> ast.expr | None:
    pos = fn.args.posonlyargs + fn.args.args
    defaults = dict(zip([a.arg for a in pos[len(pos) - len(fn.args.defaults):]], fn.args.defaults))
    defaults.update({a.arg: d for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults) if d is not None})
    return defaults.get(arg_name)


def _arg(fn: ast.FunctionDef | ast.AsyncFunctionDef, arg_name: str) -> ast.arg | None:
    for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs:
        if a.arg == arg_name:
            return a
    return None


def code_facts(fn: ast.FunctionDef | ast.AsyncFunctionDef, arg_name: str, tree: ast.Module) -> str:
    """The sentences the code itself proves: allowed values and a constant default."""
    a = _arg(fn, arg_name)
    parts = []
    values = _literal_values(a.annotation if a else None, tree)
    if values:
        parts.append("One of " + ", ".join(repr(v) for v in values) + ".")
    default = _default_of(fn, arg_name)
    if isinstance(default, ast.Constant) and default.value is not None and not (
        isinstance(default.value, str) and not default.value
    ):
        parts.append(f"Defaults to {default.value!r}.")
    return " ".join(parts)


def _clean(text: str, arg_name: str) -> str | None:
    """One safe docstring line, or None when the draft can't be used as is."""
    text = " ".join(str(text).split())
    text = re.sub(rf"^`?{re.escape(arg_name)}`?\s*(\([^)]*\))?\s*:\s*", "", text)
    text = text.strip("\"'` ")
    if not text or "TODO" in text:
        return None
    if '"""' in text or "'''" in text or "\\" in text:
        return None  # would end the docstring or need escaping
    if len(text) > _MAX_DESCRIPTION_CHARS:
        text = text[:_MAX_DESCRIPTION_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    if text[0].islower():
        text = text[0].upper() + text[1:]
    return text if text.endswith((".", "...", "?", "!")) else text + "."


@dataclass
class Drafter:
    """Writes Args: entry text for ``fix.plan_fixes``; keeps counts for the summary."""

    model: str = DEFAULT_MODEL
    timeout: float = 300.0
    drafted: int = 0
    format_todos: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def _ask(self, source: str, arg_name: str, annotation: str) -> dict:
        body = {
            "model": self.model,
            "prompt": _PROMPT.format(source=source, param=arg_name, annotation=annotation),
            "stream": False,
            "think": False,
            "format": "json",
            "options": {"temperature": 0, "num_ctx": 8192},
        }
        return json.loads(_post("/api/generate", body, self.timeout).get("response") or "{}")

    def describe(
        self, fn: ast.FunctionDef | ast.AsyncFunctionDef, arg_name: str, source: str, tree: ast.Module,
    ) -> str | None:
        """Entry text for one parameter, or None to keep the plain TODO placeholder."""
        label = f"{fn.name}.{arg_name}"
        a = _arg(fn, arg_name)
        annotation = ast.unparse(a.annotation) if a is not None and a.annotation is not None else "none"
        if re.search(r"\bContext\b", annotation):
            return None  # injected by the SDK, never in the agent's schema: not worth a model call
        fn_source = (ast.get_source_segment(source, fn) or "")[:_MAX_SOURCE_CHARS]
        try:
            answer = self._ask(fn_source, arg_name, annotation)
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.failed.append(f"{label} ({e})")
            return None
        text = _clean(answer.get("description", ""), arg_name) if isinstance(answer, dict) else None
        if text is None:
            self.failed.append(f"{label} (no usable draft)")
            return None
        facts = code_facts(fn, arg_name, tree)
        if facts:
            text = f"{text} {facts}"
        elif answer.get("format_needed") is True:
            text = f"{text} {FORMAT_TODO}"
            self.format_todos.append(label)
        self.drafted += 1
        return text
