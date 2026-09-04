"""Static analysis of MCP (Model Context Protocol) server implementations.

Walks a Python codebase, finds tool definitions authored with the FastMCP
decorator style (``@mcp.tool()``), FastMCP's direct-call style
(``provider.tool(some_func, name="...", description="...")`` — one of
FastMCP's own documented calling patterns for ``.tool()``, distinct from
using it as a decorator; verified real on ``qdrant/mcp-server-qdrant``, the
official Qdrant MCP server), the low-level SDK style
(``Tool(name=..., description=..., inputSchema=...)``), or a class-based
registry (``class XyzTool(Tool):`` with an ``apply()`` method as the handler
— verified against ``oraios/serena``, 28k+ stars: the tool name comes from
the class name itself, stripped of a trailing "Tool" and snake_cased, and
the description is the class's own docstring rather than any decorator
argument), and scores them
against a set of conformance and quality checks that matter for an agent
actually calling the tool at runtime: does it have a description an LLM
can act on, are parameters documented and typed, does it handle errors
instead of leaking stack traces back to the model, is it documented for
humans in the README.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

SECRET_PATTERN = re.compile(
    r"""(api[_-]?key|secret|token|password|access[_-]?key)\s*=\s*["'](?=[^"']*\d)[A-Za-z0-9_\-/+]{12,}["']""",
    re.IGNORECASE,
)

FASTMCP_DECORATOR_NAMES = {"tool"}

# Spec: https://modelcontextprotocol.io/specification/2026-07-28/server/tools#tool-names
# "Tool names SHOULD be between 1 and 128 characters... allowed characters: A-Z, a-z,
# 0-9, _, -, . ... SHOULD NOT contain spaces, commas... SHOULD be unique within a server."
VALID_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


@dataclass
class ToolIssue:
    tool: str
    file: str
    line: int
    check: str
    message: str
    severity: str  # "error" | "warning"
    category: str = "quality"  # "quality" | "security"


@dataclass
class ToolFinding:
    name: str
    file: str
    line: int
    has_description: bool
    description_len: int
    param_count: int
    typed_param_count: int
    has_docstring_params: bool
    has_try_except: bool
    has_bare_except: bool
    description_text: str = ""
    issues: list[ToolIssue] = field(default_factory=list)


@dataclass
class RepoIssue:
    check: str
    message: str
    severity: str
    category: str = "quality"  # "quality" | "security"


def _grade_for_percent(pct: float) -> str:
    if pct >= 90:
        return "A"
    if pct >= 80:
        return "B"
    if pct >= 70:
        return "C"
    if pct >= 60:
        return "D"
    return "F"


@dataclass
class Report:
    tools: list[ToolFinding]
    repo_issues: list[RepoIssue]
    score: int
    max_score: int
    security_score: int = 0
    security_max_score: int = 1

    @property
    def grade(self) -> str:
        if self.max_score == 0:
            return "N/A"
        return _grade_for_percent(self.score / self.max_score * 100)

    @property
    def percent(self) -> int:
        if self.max_score == 0:
            return 0
        return round(self.score / self.max_score * 100)

    @property
    def security_grade(self) -> str:
        if self.security_max_score == 0:
            return "N/A"
        return _grade_for_percent(self.security_score / self.security_max_score * 100)

    @property
    def security_percent(self) -> int:
        if self.security_max_score == 0:
            return 0
        return round(self.security_score / self.security_max_score * 100)


_ARGS_HEADING = re.compile(r"^#{0,6}\s*\*{0,2}(Args|Arguments|Params|Parameters)\*{0,2}:\*{0,2}\s*$")
_END_HEADING = re.compile(r"^#{0,6}\s*\*{0,2}(Returns|Raises|Yields|Examples?)\*{0,2}:?\*{0,2}\s*$")


def _get_docstring_sections(docstring: str | None) -> set[str]:
    if not docstring:
        return set()
    params = set()
    in_args = False
    for line in docstring.splitlines():
        stripped = line.strip()
        if _ARGS_HEADING.match(stripped):
            in_args = True
            continue
        if in_args:
            if not stripped or _END_HEADING.match(stripped):
                in_args = False
                continue
            # allow a leading bullet marker ("- confirm: ..." / "* confirm: ...")
            m = re.match(r"^[-*]?\s*\**([A-Za-z_][A-Za-z0-9_]*)\**\s*(\(.*\))?\s*:", stripped)
            if m:
                params.add(m.group(1))
    return params


_SPHINX_PARAM = re.compile(r":param\s+([^:]+):")


def _get_sphinx_documented_params(docstring: str | None) -> set[str]:
    """reST/Sphinx-style `:param name: ...` or `:param type name: ...` lines,
    anywhere in the docstring (no dedicated heading, unlike the Google-style
    `Args:` section `_get_docstring_sections` looks for) — verified against
    `oraios/serena`'s own tool docstrings, which use this convention
    exclusively. Takes the last whitespace-separated token before the colon
    so an optional leading type doesn't get mistaken for the name."""
    if not docstring:
        return set()
    names = set()
    for m in _SPHINX_PARAM.finditer(docstring):
        tokens = m.group(1).split()
        if tokens:
            names.add(tokens[-1])
    return names


def _field_call_has_description(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)
    if name != "Field":
        return False
    desc = _kwarg_str(node, "description")
    return bool(desc and desc.strip())


def _annotated_elts_have_description(elts: list[ast.expr], alias_registry: dict[str, bool]) -> bool:
    for e in elts:
        if _field_call_has_description(e):
            return True
        if isinstance(e, ast.Name) and alias_registry.get(e.id):
            return True
    return False


def _annotation_base_name(annotation: ast.expr | None) -> str | None:
    if annotation is None:
        return None
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return annotation.attr
    return None


def _is_context_param(arg: ast.arg) -> bool:
    """A `Context`-typed parameter (`ctx: Context`, `mcp.server.fastmcp.Context`,
    `Context | None`, `Optional[Context]`) is injected by FastMCP at call time
    and stripped from the tool's exposed schema before it's ever built — same
    treatment as `self`/`cls`, not something an agent ever sees or documents.
    Name-based, like everywhere else here: doesn't verify the annotation
    actually resolves to fastmcp's Context class."""
    annotation = arg.annotation
    if _annotation_base_name(annotation) == "Context":
        return True
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return (
            _annotation_base_name(annotation.left) == "Context"
            or _annotation_base_name(annotation.right) == "Context"
        )
    if isinstance(annotation, ast.Subscript):
        base_name = _annotation_base_name(annotation.value)
        if base_name == "Optional":
            return _annotation_base_name(annotation.slice) == "Context"
        if base_name == "Union":
            elts = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
            return any(_annotation_base_name(e) == "Context" for e in elts)
    return False


def _param_documented_via_field(
    arg: ast.arg, default: ast.expr | None, alias_registry: dict[str, bool] | None = None
) -> bool:
    """Pydantic-style per-parameter docs: Annotated[T, Field(description=...)],
    `x: T = Field(description=...)`, or a type alias (possibly imported from another
    file) that itself resolves to one of those forms, e.g. `x: SomeFieldAlias = None`
    where `SomeFieldAlias = Annotated[str, Field(description=...)]` elsewhere.
    """
    alias_registry = alias_registry or {}
    annotation = arg.annotation
    if isinstance(annotation, ast.Name) and alias_registry.get(annotation.id):
        return True
    if isinstance(annotation, ast.Subscript):
        base = annotation.value
        base_name = base.attr if isinstance(base, ast.Attribute) else (base.id if isinstance(base, ast.Name) else None)
        if base_name == "Annotated":
            sl = annotation.slice
            elts = sl.elts if isinstance(sl, ast.Tuple) else [sl]
            if _annotated_elts_have_description(elts, alias_registry):
                return True
    return _field_call_has_description(default) if default is not None else False


def _collect_field_aliases(tree: ast.Module, registry: dict[str, bool]) -> None:
    """Find module-level `Name = Annotated[T, Field(description=...)]` assignments
    so parameters annotated with the alias elsewhere (even in another file) are
    recognized as documented."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if not names:
                continue
            documented = False
            if isinstance(value, ast.Subscript):
                base = value.value
                base_name = base.attr if isinstance(base, ast.Attribute) else (base.id if isinstance(base, ast.Name) else None)
                if base_name == "Annotated":
                    sl = value.slice
                    elts = sl.elts if isinstance(sl, ast.Tuple) else [sl]
                    documented = _annotated_elts_have_description(elts, registry)
            elif _field_call_has_description(value):
                documented = True
            if documented:
                for n in names:
                    registry[n] = True


def _find_decorator_call(dec: ast.expr, names: set[str]) -> ast.Call | None:
    node = dec
    if isinstance(node, ast.Call):
        func = node.func
    else:
        func = node
    attr_name = None
    if isinstance(func, ast.Attribute):
        attr_name = func.attr
    elif isinstance(func, ast.Name):
        attr_name = func.id
    if attr_name in names:
        return node if isinstance(node, ast.Call) else None
    return None


def _kwarg_str(call: ast.Call, key: str) -> str | None:
    for kw in call.keywords:
        if kw.arg == key and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _kwarg_str_list(call: ast.Call, key: str) -> set[str]:
    for kw in call.keywords:
        if kw.arg == key and isinstance(kw.value, (ast.List, ast.Tuple, ast.Set)):
            return {
                elt.value for elt in kw.value.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            }
    return set()


def _contains_try_except(node: ast.AST) -> tuple[bool, bool]:
    has_try = False
    has_bare = False
    for n in ast.walk(node):
        if isinstance(n, ast.Try):
            has_try = True
            for handler in n.handlers:
                if handler.type is None:
                    has_bare = True
    return has_try, has_bare


def _direct_call_names(node: ast.AST, import_aliases: dict[str, str] | None = None) -> set[str]:
    """Direct calls inside a node's subtree, by the name they'd be `def`-ed
    under: bare-name calls (`foo(...)`) by their name, and attribute/method
    calls (`self.foo(...)`, `client.request(...)`) by the attribute name —
    a real, common delegation shape (a service/context object's method doing
    the actual work) that's just as legitimate a place for error handling to
    live as a bare function. Bare names are additionally resolved through
    `from x import y as z`-style aliases back to the real function name (e.g.
    `_compare_strategies()` in the caller resolves to `compare_strategies`,
    the name it's actually defined under) so registry lookups keyed by def
    name still hit. Both forms are name-only, not type- or import-resolved —
    a method call is matched against ANY def/method in the repo with that
    name, so a very common method name (`get`, `run`, `close`) can collide
    with an unrelated definition; an accepted risk, the same simplification
    already applied to bare-name collisions across files."""
    names = {
        n.func.id for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    if import_aliases:
        names = {import_aliases.get(n, n) for n in names}
    method_names = {
        n.func.attr for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    return names | method_names


def _build_import_aliases(trees: list[tuple[str, ast.Module]]) -> dict[str, str]:
    """Repo-wide map of `from x import y as z` -> {z: y}, so a call site using
    the alias can be resolved back to the name a function is actually
    `def`-ed under. Same name-only simplification as everywhere else here —
    doesn't check that the alias's source module is the one that really
    defines `y`."""
    aliases: dict[str, str] = {}
    for _, tree in trees:
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                for alias in n.names:
                    if alias.asname and alias.asname != alias.name:
                        aliases[alias.asname] = alias.name
    return aliases


def _build_error_handling_registry(
    trees: list[tuple[str, ast.Module]], import_aliases: dict[str, str]
) -> dict[str, bool]:
    """Repo-wide, name-based (not import-resolved — same simplification as the
    Field alias registry) map of function name -> whether it handles errors
    itself or by delegating to another locally-defined function that does,
    transitively. A tool that only calls a helper with its own try/except
    two or three calls deep (a real pattern seen dogfooding — e.g. a tool
    calling a service function that calls a network-request function with
    the actual try/except) shouldn't be flagged as having no error handling.

    Resolution is by function name only, not by which module it's imported
    from, so two same-named functions in different files are not
    distinguished — an accepted risk, consistent with the alias registry."""
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for _, tree in trees:
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions[n.name] = n

    memo: dict[str, bool] = {}

    def resolve(name: str, depth: int = 0) -> bool:
        if name in memo:
            return memo[name]
        if depth > 5:
            return False
        node = functions.get(name)
        if node is None:
            return False
        memo[name] = False  # cycle guard: assume unhandled while resolving
        has_own, _ = _contains_try_except(node)
        result = has_own or any(
            resolve(callee, depth + 1) for callee in _direct_call_names(node, import_aliases)
        )
        memo[name] = result
        return result

    for name in list(functions):
        resolve(name)
    # A tool calling the alias directly (e.g. `_compare_strategies()` for a
    # function actually `def`-ed as `compare_strategies`) should still hit —
    # make the registry itself alias-aware rather than requiring every call
    # site to resolve through import_aliases too.
    for alias_name, real_name in import_aliases.items():
        if real_name in memo:
            memo.setdefault(alias_name, memo[real_name])
    return memo


def _analyze_function_as_tool(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    file: str,
    description_override: str | None = None,
    alias_registry: dict[str, bool] | None = None,
    name_override: str | None = None,
    excluded_arg_names: set[str] | None = None,
    error_handling_registry: dict[str, bool] | None = None,
) -> ToolFinding:
    tool_name = name_override or fn.name
    docstring = ast.get_docstring(fn)
    description = (
        description_override if description_override is not None
        else (docstring.splitlines()[0].strip() if docstring else "")
    )
    all_args = fn.args.args
    excluded = excluded_arg_names or set()
    # FastMCP's `exclude_args=[...]` removes a param from the exposed tool
    # schema entirely — an agent can never pass it, so it isn't part of what
    # needs documenting, typing, or counting as a schema parameter at all.
    # A `Context`-typed parameter gets the same treatment automatically,
    # without needing exclude_args: FastMCP injects it at call time and
    # strips it from the schema before the schema is ever built (verified
    # against fastmcp's own function_parsing.py, without_injected_parameters).
    args = [
        a for a in all_args
        if a.arg not in ("self", "cls") and a.arg not in excluded and not _is_context_param(a)
    ]
    typed = sum(1 for a in args if a.annotation is not None)
    doc_params = _get_docstring_sections(docstring) | _get_sphinx_documented_params(docstring)

    defaults_by_arg = dict(zip(all_args[len(all_args) - len(fn.args.defaults):], fn.args.defaults))
    field_documented_names = {
        a.arg for a in args if _param_documented_via_field(a, defaults_by_arg.get(a), alias_registry)
    }
    documented_count = len(doc_params | field_documented_names)

    has_try, has_bare = _contains_try_except(fn)
    if not has_try and error_handling_registry:
        # No try/except in this function's own body, but it may delegate its
        # real work to a locally-defined helper (possibly several calls deep)
        # that already handles errors — see _build_error_handling_registry.
        has_try = any(
            error_handling_registry.get(callee, False) for callee in _direct_call_names(fn)
        )

    finding = ToolFinding(
        name=tool_name,
        file=file,
        line=fn.lineno,
        has_description=bool(description.strip()),
        description_len=len(description.strip()),
        param_count=len(args),
        typed_param_count=typed,
        has_docstring_params=documented_count >= len(args) and len(args) > 0,
        has_try_except=has_try,
        has_bare_except=has_bare,
        description_text=description,
    )

    if not finding.has_description:
        finding.issues.append(ToolIssue(
            tool_name, file, fn.lineno, "description",
            "Tool has no description. An agent cannot decide when to call this.",
            "error",
        ))
    elif finding.description_len < 10:
        finding.issues.append(ToolIssue(
            tool_name, file, fn.lineno, "description",
            f"Description is only {finding.description_len} chars — likely just restates the name.",
            "warning",
        ))

    if args and typed < len(args):
        finding.issues.append(ToolIssue(
            tool_name, file, fn.lineno, "types",
            f"{len(args) - typed}/{len(args)} parameters have no type annotation.",
            "warning",
        ))

    if args and not finding.has_docstring_params:
        finding.issues.append(ToolIssue(
            tool_name, file, fn.lineno, "param_docs",
            "Parameters aren't documented — no Args:/:param: docstring section and no per-parameter "
            "Field(description=...) — the model only sees names, not intent.",
            "warning",
        ))

    if not has_try:
        finding.issues.append(ToolIssue(
            tool_name, file, fn.lineno, "error_handling",
            "No try/except in this function's own body. FastMCP still catches an unhandled "
            "exception here and returns a structured error rather than a raw traceback, but "
            "the model only sees the generic exception text — a tool-level catch that raises "
            "a specific, actionable message gives the model something it can act on. (Delegation "
            "to a locally-defined Python helper with its own try/except is already resolved "
            "before this warning fires; a helper outside this repo, or a TS/JS helper, isn't — "
            "see Known Limitations.)",
            "warning",
        ))
    if has_bare:
        finding.issues.append(ToolIssue(
            tool_name, file, fn.lineno, "bare_except",
            "Bare 'except:' swallows all errors including cancellation — catch specific exceptions.",
            "error",
        ))

    return finding


def _find_fastmcp_tools(
    tree: ast.Module,
    file: str,
    alias_registry: dict[str, bool] | None = None,
    error_handling_registry: dict[str, bool] | None = None,
) -> list[ToolFinding]:
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            call = _find_decorator_call(dec, FASTMCP_DECORATOR_NAMES)
            if call is None:
                # bare @mcp.tool with no parens still counts
                attr = dec.attr if isinstance(dec, ast.Attribute) else (dec.id if isinstance(dec, ast.Name) else None)
                if attr not in FASTMCP_DECORATOR_NAMES:
                    continue
                findings.append(_analyze_function_as_tool(
                    node, file, alias_registry=alias_registry, error_handling_registry=error_handling_registry
                ))
                break
            description_override = _kwarg_str(call, "description")
            name_override = _kwarg_str(call, "name")
            excluded_args = _kwarg_str_list(call, "exclude_args")
            findings.append(_analyze_function_as_tool(
                node, file, description_override, alias_registry, name_override,
                excluded_args, error_handling_registry,
            ))
            break
    return findings


def _bare_direct_call_finding(name: str, description: str, file: str, line: int) -> ToolFinding:
    """A ToolFinding for a `.tool(func, name=..., ...)` direct call whose
    `func` couldn't be traced back to a real function definition (see
    `_resolve_direct_call_function`) — only name/description are checked,
    same partial-coverage stance used elsewhere in this codebase when no
    handler is available to inspect. `has_try_except=True` here means "not
    inspected", not "verified present" — the point is to avoid a false
    negative on an axis we genuinely can't check, not to claim it's fine."""
    finding = ToolFinding(
        name=name,
        file=file,
        line=line,
        has_description=bool(description.strip()),
        description_len=len(description.strip()),
        param_count=0,
        typed_param_count=0,
        has_docstring_params=False,
        has_try_except=True,
        has_bare_except=False,
        description_text=description,
    )
    if not finding.has_description:
        finding.issues.append(ToolIssue(
            name, file, line, "description",
            "Tool has no description. An agent cannot decide when to call this.",
            "error",
        ))
    elif finding.description_len < 10:
        finding.issues.append(ToolIssue(
            name, file, line, "description",
            f"Description is only {finding.description_len} chars — likely just restates the name.",
            "warning",
        ))
    return finding


def _resolve_direct_call_function(
    name: str, scope: ast.AST
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Resolves `name` to a function definition within `scope`, only when it
    unambiguously refers to one: either `name` is itself a function def
    somewhere in scope, or there is exactly one simple `name = other_name`
    assignment in scope and `other_name` is a function def. Any additional
    assignment to `name` (e.g. `name = wrap_something(name)`, a common way
    to conditionally post-process a tool function before registering it —
    verified against `qdrant/mcp-server-qdrant`) makes which function
    actually gets registered depend on runtime config; correctly left
    unresolved rather than guessing which branch runs."""
    local_funcs = {
        n.name: n for n in ast.walk(scope)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not scope
    }
    if name in local_funcs:
        return local_funcs[name]
    assigns = [
        n for n in ast.walk(scope)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)
    ]
    if len(assigns) != 1:
        return None
    value = assigns[0].value
    if isinstance(value, ast.Name):
        return local_funcs.get(value.id)
    return None


def _enclosing_scope(tree: ast.Module, node: ast.AST) -> ast.AST:
    """Best-effort nearest enclosing function containing `node` (or the
    module itself), found via line-range containment — `ast` doesn't wire up
    parent pointers on its own."""
    best = tree
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(n, "end_lineno", None) or n.lineno
        if n.lineno <= node.lineno <= end and (best is tree or n.lineno > best.lineno):
            best = n
    return best


def _find_direct_call_tools(
    tree: ast.Module,
    file: str,
    alias_registry: dict[str, bool] | None = None,
    error_handling_registry: dict[str, bool] | None = None,
) -> list[ToolFinding]:
    """FastMCP's `.tool()` also supports a direct call form — the function
    passed as a positional argument rather than used as a decorator:
    `provider.tool(some_func, name="...", description="...")`, confirmed
    directly against FastMCP's own docstring for this method ("direct
    function call" is one of its documented calling patterns) — and real on
    `qdrant/mcp-server-qdrant`, the official Qdrant MCP server, where both of
    its tools are registered this way and neither was detected before this.
    Only resolves a tool whose `name=` is a literal string; see
    `_resolve_direct_call_function` for when the registered function itself
    can (and can't) be resolved for full param/error-handling analysis."""
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attr = func.attr if isinstance(func, ast.Attribute) else None
        if attr not in FASTMCP_DECORATOR_NAMES:
            continue
        if not node.args or not isinstance(node.args[0], ast.Name):
            continue
        name_override = _kwarg_str(node, "name")
        if name_override is None:
            continue
        description_override = _kwarg_str(node, "description") or ""
        scope = _enclosing_scope(tree, node)
        fn_node = _resolve_direct_call_function(node.args[0].id, scope)
        if fn_node is not None:
            excluded_args = _kwarg_str_list(node, "exclude_args")
            findings.append(_analyze_function_as_tool(
                fn_node, file, description_override or None, alias_registry, name_override,
                excluded_args, error_handling_registry,
            ))
        else:
            findings.append(_bare_direct_call_finding(name_override, description_override, file, node.lineno))
    return findings


def _find_lowlevel_tools(tree: ast.Module, file: str) -> list[ToolFinding]:
    """Find Tool(name=..., description=..., inputSchema=...) constructor calls."""
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name_id = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)
        if name_id != "Tool":
            continue
        name = _kwarg_str(node, "name")
        if name is None:
            # A dynamic name (e.g. `Tool(name=tool.name, ...)` inside a loop over
            # a registry of tool objects — a real, common pattern for class-based
            # tool frameworks) can't be attributed to a single finding. Skip it
            # rather than emit a misleading "<unnamed>" report, matching the TS
            # analyzer's handling of an equally dynamic tool name.
            continue
        description = _kwarg_str(node, "description") or ""
        schema_kw = next((kw for kw in node.keywords if kw.arg == "inputSchema"), None)
        param_count = 0
        typed_param_count = 0
        has_docstring_params = True
        if schema_kw is not None and isinstance(schema_kw.value, ast.Dict):
            for k, v in zip(schema_kw.value.keys, schema_kw.value.values):
                if isinstance(k, ast.Constant) and k.value == "properties" and isinstance(v, ast.Dict):
                    param_count = len(v.keys)
                    for pk, pv in zip(v.keys, v.values):
                        if isinstance(pv, ast.Dict):
                            has_desc = any(
                                isinstance(pk2, ast.Constant) and pk2.value == "description"
                                for pk2 in pv.keys
                            )
                            has_type = any(
                                isinstance(pk2, ast.Constant) and pk2.value == "type"
                                for pk2 in pv.keys
                            )
                            if has_desc:
                                typed_param_count += 1
                            if not has_type:
                                has_docstring_params = False

        finding = ToolFinding(
            name=name,
            file=file,
            line=node.lineno,
            has_description=bool(description.strip()),
            description_len=len(description.strip()),
            param_count=param_count,
            typed_param_count=typed_param_count,
            has_docstring_params=typed_param_count >= param_count and param_count > 0,
            has_try_except=True,  # not attributable to a single function body here
            has_bare_except=False,
            description_text=description,
        )
        if not finding.has_description:
            finding.issues.append(ToolIssue(
                name, file, node.lineno, "description",
                "Tool has no description. An agent cannot decide when to call this.",
                "error",
            ))
        elif finding.description_len < 10:
            finding.issues.append(ToolIssue(
                name, file, node.lineno, "description",
                f"Description is only {finding.description_len} chars — likely just restates the name.",
                "warning",
            ))
        if param_count and typed_param_count < param_count:
            finding.issues.append(ToolIssue(
                name, file, node.lineno, "param_docs",
                f"{param_count - typed_param_count}/{param_count} input schema properties have no description.",
                "warning",
            ))
        findings.append(finding)
    return findings


def _class_bases_include(cls_node: ast.ClassDef, name: str) -> bool:
    for base in cls_node.bases:
        base_name = base.attr if isinstance(base, ast.Attribute) else (base.id if isinstance(base, ast.Name) else None)
        if base_name == name:
            return True
    return False


def _tool_name_from_class_name(class_name: str) -> str:
    """Serena's own convention, verified directly against its
    `Tool.get_name_from_cls`: strip a trailing 'Tool' suffix, then convert
    CamelCase to snake_case (e.g. `ReadFileTool` -> `read_file`)."""
    name = class_name
    if name.endswith("Tool"):
        name = name[:-4]
    return "".join("_" + c.lower() if c.isupper() else c for c in name).lstrip("_")


def _find_class_based_tools(
    tree: ast.Module,
    file: str,
    alias_registry: dict[str, bool] | None = None,
    error_handling_registry: dict[str, bool] | None = None,
) -> list[ToolFinding]:
    """A class-based tool registry: `class XyzTool(Tool):` with an `apply()`
    method as the handler — no decorator, no `Tool(...)` constructor call
    anywhere. Verified against `oraios/serena` (28k+ stars, 0/30+ tools found
    before this): the tool name is derived from the class name itself (see
    `_tool_name_from_class_name`), the description is the class's own
    docstring rather than a decorator argument, and parameters come from
    `apply`'s signature/docstring exactly like any other tool function.
    Matched by base-class name only, like every other name-based registry in
    this module — doesn't verify the ancestor actually resolves to a real
    "Tool" base class."""
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not _class_bases_include(node, "Tool"):
            continue
        apply_fn = next(
            (n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "apply"),
            None,
        )
        if apply_fn is None:
            continue
        class_doc = ast.get_docstring(node)
        findings.append(_analyze_function_as_tool(
            apply_fn, file,
            description_override=class_doc.strip() if class_doc else "",
            alias_registry=alias_registry,
            name_override=_tool_name_from_class_name(node.name),
            error_handling_registry=error_handling_registry,
        ))
    return findings


_JS_TEST_SUFFIXES = (".test.ts", ".test.tsx", ".test.js", ".test.jsx", ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx")


def _is_test_file(path: Path) -> bool:
    name = path.name
    if name.startswith("test_") or name.endswith("_test.py") or name.endswith("_test.go"):
        return True
    if name.endswith(_JS_TEST_SUFFIXES):
        return True
    return any(part in ("test", "tests") for part in path.parts)


def _scan_secrets(py_files: list[Path]) -> list[RepoIssue]:
    issues = []
    for f in py_files:
        if _is_test_file(f):
            continue
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if SECRET_PATTERN.search(line):
                issues.append(RepoIssue(
                    "secrets",
                    f"{f.name}:{i} looks like a hardcoded credential.",
                    "error",
                    "security",
                ))
    return issues


def analyze_repo(root: Path) -> Report:
    # Lazy import: ts_analyzer/go_analyzer/security import ToolFinding/ToolIssue
    # from this module, so importing them at module load time would be circular.
    from .go_analyzer import find_go_tools
    from .security import scan_dangerous_exec, scan_prompt_injection, scan_ssrf, scan_unsafe_deserialization
    from .ts_analyzer import find_ts_tools

    py_files = [p for p in root.rglob("*.py") if "/.git/" not in str(p) and "/venv/" not in str(p) and "/node_modules/" not in str(p)]

    trees: list[tuple[str, ast.Module]] = []
    unparseable: list[str] = []
    for f in py_files:
        if _is_test_file(f):
            continue
        try:
            tree = ast.parse(f.read_text(errors="ignore"), filename=str(f))
        except SyntaxError:
            unparseable.append(str(f.relative_to(root)))
            continue
        trees.append((str(f.relative_to(root)), tree))

    # Repo-wide Field(description=...) type-alias registry, e.g.
    # `BestPracticeKeyParam = Annotated[str, Field(description="...")]` defined
    # in one file and imported/used as a parameter annotation in another.
    # Two passes catch a one-level alias-of-alias without full import resolution.
    alias_registry: dict[str, bool] = {}
    for _, tree in trees:
        _collect_field_aliases(tree, alias_registry)
    for _, tree in trees:
        _collect_field_aliases(tree, alias_registry)

    error_handling_registry = _build_error_handling_registry(trees, _build_import_aliases(trees))

    tools: list[ToolFinding] = []
    for rel, tree in trees:
        tools.extend(_find_fastmcp_tools(tree, rel, alias_registry, error_handling_registry))
        tools.extend(_find_direct_call_tools(tree, rel, alias_registry, error_handling_registry))
        tools.extend(_find_lowlevel_tools(tree, rel))
        tools.extend(_find_class_based_tools(tree, rel, alias_registry, error_handling_registry))

    ts_tools, ts_unparseable = find_ts_tools(root)
    tools.extend(ts_tools)
    unparseable.extend(ts_unparseable)

    go_tools, go_unparseable = find_go_tools(root)
    tools.extend(go_tools)
    unparseable.extend(go_unparseable)

    scan_prompt_injection(tools)

    repo_issues: list[RepoIssue] = []

    invalid_names = sorted({t.name for t in tools if not VALID_TOOL_NAME.match(t.name)})
    if invalid_names:
        repo_issues.append(RepoIssue(
            "tool_name",
            f"{len(invalid_names)} tool name(s) violate the spec's Tool Names guidance "
            f"(1-128 chars; only A-Z a-z 0-9 _ - .): {', '.join(invalid_names[:5])}"
            + ("…" if len(invalid_names) > 5 else ""),
            "warning",
        ))
    seen: dict[str, int] = {}
    for t in tools:
        seen[t.name] = seen.get(t.name, 0) + 1
    duplicate_names = sorted(n for n, count in seen.items() if count > 1)
    if duplicate_names:
        repo_issues.append(RepoIssue(
            "tool_name",
            f"{len(duplicate_names)} tool name(s) are declared more than once, violating the "
            f"spec's 'SHOULD be unique within a server' guidance: {', '.join(duplicate_names[:5])}"
            + ("…" if len(duplicate_names) > 5 else ""),
            "warning",
        ))

    if unparseable:
        repo_issues.append(RepoIssue(
            "parse_error",
            f"{len(unparseable)} file(s) could not be parsed and were skipped — results below may be "
            f"incomplete. This usually means the file uses syntax newer than the Python running "
            f"mcp-doctor (e.g. `match` statements need Python >=3.10). Skipped: "
            + ", ".join(unparseable[:5]) + ("…" if len(unparseable) > 5 else ""),
            "error",
        ))

    readme = next((p for p in root.glob("README*")), None)
    readme_text = readme.read_text(errors="ignore") if readme else ""
    if not readme:
        repo_issues.append(RepoIssue("readme", "No README found.", "error"))
    else:
        undocumented = [t.name for t in tools if t.name not in readme_text]
        if undocumented:
            repo_issues.append(RepoIssue(
                "readme",
                f"{len(undocumented)} tool(s) not mentioned in README: {', '.join(undocumented[:5])}"
                + ("…" if len(undocumented) > 5 else ""),
                "warning",
            ))

    if not any(root.glob("LICENSE*")):
        repo_issues.append(RepoIssue("license", "No LICENSE file — undermines adoption.", "warning"))

    has_tests = (
        any(root.rglob("test_*.py"))
        or any(root.rglob("*_test.py"))
        or any(root.rglob("*.test.ts"))
        or any(root.rglob("*.spec.ts"))
        or any(root.rglob("*_test.go"))
        or (root / "tests").is_dir()
        or any(p.name == "__tests__" for p in root.rglob("__tests__"))
    )
    if not has_tests:
        repo_issues.append(RepoIssue("tests", "No test files found.", "warning"))

    has_py_packaging = (root / "pyproject.toml").exists() or (root / "requirements.txt").exists() or (root / "setup.py").exists()
    has_js_packaging = (root / "package.json").exists()
    has_go_packaging = (root / "go.mod").exists()
    if not has_py_packaging and not has_js_packaging and not has_go_packaging:
        repo_issues.append(RepoIssue("packaging", "No pyproject.toml/requirements.txt/setup.py/package.json/go.mod — dependencies aren't pinned.", "warning"))

    ts_js_files = [
        p for p in root.rglob("*")
        if p.suffix in (".ts", ".tsx", ".js", ".jsx")
        and not p.name.endswith(".d.ts")  # ambient type declarations — no executable code, ever
        and "/node_modules/" not in str(p) and "/.git/" not in str(p)
        and not _is_test_file(p)
    ]
    go_files = [
        p for p in root.rglob("*.go")
        if "/vendor/" not in str(p) and "/.git/" not in str(p)
        and not _is_test_file(p)
    ]
    all_files = py_files + ts_js_files + go_files
    repo_issues.extend(_scan_secrets(all_files))
    repo_issues.extend(scan_dangerous_exec(all_files))
    repo_issues.extend(scan_ssrf(all_files))
    repo_issues.extend(scan_unsafe_deserialization(py_files))

    score = 0
    max_score = 0
    security_score = 0
    security_max_score = 0

    for t in tools:
        max_score += 10
        security_max_score += 10
        score += 10
        security_score += 10
        for issue in t.issues:
            penalty = 3 if issue.severity == "error" else 1
            if issue.category == "security":
                security_score -= penalty
            else:
                score -= penalty

    max_score += 10  # readme presence
    if readme:
        score += 10
    max_score += 5  # license
    if not any(i.check == "license" for i in repo_issues):
        score += 5
    max_score += 5  # tests
    if has_tests:
        score += 5
    max_score += 5  # packaging
    if not any(i.check == "packaging" for i in repo_issues):
        score += 5

    # Flat repo-wide security baseline, independent of tool count: the four
    # pattern-scan checks below apply across the whole codebase regardless of
    # how many (if any) tools were found, so — unlike quality, where per-tool
    # findings dominate max_score — a repo with 0 tools would otherwise have
    # no positive security max_score at all and any single finding would
    # floor it straight to a nonsensical 0%.
    security_max_score += 20
    security_score += 20

    # Each repo-level security check's scoring impact is capped at 3
    # occurrences (every instance still appears in repo_issues/JSON output in
    # full) — a systemic pattern across many files (e.g. a proxy server's
    # entire purpose being to forward caller-supplied URLs, which trips the
    # SSRF heuristic on every call site) shouldn't manufacture an artificially
    # catastrophic score just by being repeated, when a human reviewer would
    # weight "this pattern exists" once, not once per line it appears on.
    SECURITY_CHECK_WEIGHT = {"secrets": 5, "dangerous_exec": 5, "unsafe_deserialization": 5, "ssrf": 2}
    SECURITY_CHECK_CAP = 3
    security_check_counts: dict[str, int] = {}
    for i in repo_issues:
        if i.check in SECURITY_CHECK_WEIGHT:
            security_check_counts[i.check] = security_check_counts.get(i.check, 0) + 1
        elif i.check == "parse_error":
            score -= 5
        elif i.check == "tool_name":
            score -= 2
    for check, count in security_check_counts.items():
        security_score -= SECURITY_CHECK_WEIGHT[check] * min(count, SECURITY_CHECK_CAP)

    score = max(0, score)
    max_score = max(max_score, 1)
    security_score = max(0, security_score)
    security_max_score = max(security_max_score, 1)

    return Report(
        tools=tools, repo_issues=repo_issues, score=score, max_score=max_score,
        security_score=security_score, security_max_score=security_max_score,
    )
