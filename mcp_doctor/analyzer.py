"""Static analysis of MCP (Model Context Protocol) server implementations.

Walks a Python codebase, finds tool definitions authored with either the
FastMCP decorator style (``@mcp.tool()``) or the low-level SDK style
(``Tool(name=..., description=..., inputSchema=...)``), and scores them
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
    issues: list[ToolIssue] = field(default_factory=list)


@dataclass
class RepoIssue:
    check: str
    message: str
    severity: str


@dataclass
class Report:
    tools: list[ToolFinding]
    repo_issues: list[RepoIssue]
    score: int
    max_score: int

    @property
    def grade(self) -> str:
        if self.max_score == 0:
            return "N/A"
        pct = self.score / self.max_score * 100
        if pct >= 90:
            return "A"
        if pct >= 80:
            return "B"
        if pct >= 70:
            return "C"
        if pct >= 60:
            return "D"
        return "F"

    @property
    def percent(self) -> int:
        if self.max_score == 0:
            return 0
        return round(self.score / self.max_score * 100)


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
    """Bare-name function calls (`foo(...)`, not `self.foo(...)`) directly
    inside a node's subtree, resolved through `from x import y as z`-style
    aliases back to the real function name (e.g. `_compare_strategies()` in
    the caller resolves to `compare_strategies`, the name it's actually
    defined under) so registry lookups keyed by def name still hit."""
    names = {
        n.func.id for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    if not import_aliases:
        return names
    return {import_aliases.get(n, n) for n in names}


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
    description = description_override or (docstring.splitlines()[0].strip() if docstring else "")
    all_args = fn.args.args
    excluded = excluded_arg_names or set()
    # FastMCP's `exclude_args=[...]` removes a param from the exposed tool
    # schema entirely — an agent can never pass it, so it isn't part of what
    # needs documenting, typing, or counting as a schema parameter at all.
    args = [a for a in all_args if a.arg not in ("self", "cls") and a.arg not in excluded]
    typed = sum(1 for a in args if a.annotation is not None)
    doc_params = _get_docstring_sections(docstring)

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
            "Parameters aren't documented — no Args: docstring section and no per-parameter "
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


_JS_TEST_SUFFIXES = (".test.ts", ".test.tsx", ".test.js", ".test.jsx", ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx")


def _is_test_file(path: Path) -> bool:
    name = path.name
    if name.startswith("test_") or name.endswith("_test.py"):
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
                ))
    return issues


def analyze_repo(root: Path) -> Report:
    # Lazy import: ts_analyzer imports ToolFinding/ToolIssue from this module,
    # so importing it at module load time would be circular.
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
        tools.extend(_find_lowlevel_tools(tree, rel))

    ts_tools, ts_unparseable = find_ts_tools(root)
    tools.extend(ts_tools)
    unparseable.extend(ts_unparseable)

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
        or (root / "tests").is_dir()
        or any(p.name == "__tests__" for p in root.rglob("__tests__"))
    )
    if not has_tests:
        repo_issues.append(RepoIssue("tests", "No test files found.", "warning"))

    has_py_packaging = (root / "pyproject.toml").exists() or (root / "requirements.txt").exists() or (root / "setup.py").exists()
    has_js_packaging = (root / "package.json").exists()
    if not has_py_packaging and not has_js_packaging:
        repo_issues.append(RepoIssue("packaging", "No pyproject.toml/requirements.txt/setup.py/package.json — dependencies aren't pinned.", "warning"))

    ts_js_files = [
        p for p in root.rglob("*")
        if p.suffix in (".ts", ".tsx", ".js", ".jsx")
        and "/node_modules/" not in str(p) and "/.git/" not in str(p)
        and not _is_test_file(p)
    ]
    repo_issues.extend(_scan_secrets(py_files + ts_js_files))

    score = 0
    max_score = 0

    for t in tools:
        max_score += 10
        score += 10
        for issue in t.issues:
            score -= 3 if issue.severity == "error" else 1

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
    for i in repo_issues:
        if i.check in ("secrets", "parse_error"):
            score -= 5
        elif i.check == "tool_name":
            score -= 2

    score = max(0, score)
    max_score = max(max_score, 1)

    return Report(tools=tools, repo_issues=repo_issues, score=score, max_score=max_score)
