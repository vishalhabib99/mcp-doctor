"""Auto-fix for the subset of mcp-doctor findings that are safe to apply
mechanically, without human judgment:

- a bare ``except:`` narrowed to ``except Exception:``
- a missing ``Args:`` docstring section stubbed in for a tool whose params
  have *no* documentation at all (docstring or ``Field(description=...)``)

Deliberately does not touch: missing/short descriptions (can't fabricate
real intent), untyped parameters (can't infer real types), wrapping a
function body in try/except (too invasive to do safely), or a docstring
that already documents *some* but not all params (merging into an existing
Args: section correctly is a bigger job than this pass takes on).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .analyzer import (
    Report,
    ToolFinding,
    _field_call_has_description,
    _get_docstring_sections,
    _get_numpy_documented_params,
    _get_sphinx_documented_params,
)

_DOCSTRING_QUOTE = re.compile(r'^(\s*)(\'\'\'|""")(.*)\2\s*\n?$')
_CLOSING_QUOTE_ONLY = re.compile(r'^(\s*)(\'\'\'|""")\s*\n?$')
_CLOSING_QUOTE_TRAILING = re.compile(r'^(.*?)(\'\'\'|""")\s*\n?$')


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _fix_bare_except_in_source(source: str, tree: ast.Module) -> tuple[str, int]:
    lines = source.splitlines(keepends=True)
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            if handler.type is not None:
                continue
            idx = handler.lineno - 1
            new_line = re.sub(r"except\s*:", "except Exception:", lines[idx], count=1)
            if new_line != lines[idx]:
                lines[idx] = new_line
                count += 1
    return "".join(lines), count


def _find_function_at_line(tree: ast.Module, lineno: int) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno == lineno:
            return node
    return None


def _fully_undocumented_args(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str] | None:
    """Names of every param, if none of them have any documentation at all
    (an Args:, :param: or NumPy Parameters section, or Field(description=...)); None if some do
    (partial docs — too risky to merge, so this fix skips it)."""
    all_args = fn.args.args
    args = [a for a in all_args if a.arg not in ("self", "cls")]
    if not args:
        return None

    docstring = ast.get_docstring(fn)
    doc_params = (
        _get_docstring_sections(docstring)
        | _get_sphinx_documented_params(docstring)
        | _get_numpy_documented_params(docstring)
    )

    defaults_by_arg = dict(zip(all_args[len(all_args) - len(fn.args.defaults):], fn.args.defaults))
    field_documented = {a.arg for a in args if _field_call_has_description(defaults_by_arg.get(a))}

    if doc_params | field_documented:
        return None

    return [a.arg for a in args]


def _apply_args_stub(lines: list[str], fn: ast.FunctionDef | ast.AsyncFunctionDef, arg_names: list[str]) -> bool:
    if not fn.body:
        return False
    doc_expr = fn.body[0]
    if not (isinstance(doc_expr, ast.Expr) and isinstance(doc_expr.value, ast.Constant) and isinstance(doc_expr.value.value, str)):
        return False  # no real docstring to extend — won't fabricate one from nothing

    start_idx = doc_expr.lineno - 1
    end_idx = doc_expr.end_lineno - 1
    indent = _indent_of(lines[start_idx])

    args_block = [f"{indent}Args:\n"] + [f"{indent}    {a}: TODO: describe this parameter.\n" for a in arg_names]

    if start_idx == end_idx:
        m = _DOCSTRING_QUOTE.match(lines[start_idx])
        if not m:
            return False
        quote, summary = m.group(2), m.group(3)
        new_lines = [f"{indent}{quote}{summary}\n", "\n", *args_block, f"{indent}{quote}\n"]
        lines[start_idx:start_idx + 1] = new_lines
        return True

    closing_line = lines[end_idx]
    m = _CLOSING_QUOTE_ONLY.match(closing_line)
    if m:
        new_lines = ["\n", *args_block, closing_line]
        lines[end_idx:end_idx + 1] = new_lines
        return True

    m2 = _CLOSING_QUOTE_TRAILING.match(closing_line)
    if not m2:
        return False
    trailing_text, quote = m2.group(1), m2.group(2)
    new_lines = [f"{trailing_text}\n", "\n", *args_block, f"{indent}{quote}\n"]
    lines[end_idx:end_idx + 1] = new_lines
    return True


def apply_fixes(root: Path, report: Report) -> list[str]:
    """Apply the safe subset of fixes in place. Returns the relative paths
    of files that were changed."""
    changed: list[str] = []

    by_file: dict[str, list[ToolFinding]] = {}
    for t in report.tools:
        by_file.setdefault(t.file, []).append(t)

    for rel_file, findings in by_file.items():
        path = root / rel_file
        if not path.exists() or path.suffix != ".py":
            continue
        original = path.read_text()
        try:
            tree = ast.parse(original, filename=str(path))
        except SyntaxError:
            continue

        source = original
        file_changed = False

        new_source, n = _fix_bare_except_in_source(source, tree)
        if n:
            source = new_source
            file_changed = True

        needs_stub = sorted(
            (t for t in findings if t.param_count > 0 and not t.has_docstring_params),
            key=lambda t: t.line,
            reverse=True,
        )
        if needs_stub:
            lines = source.splitlines(keepends=True)
            tree2 = ast.parse(source, filename=str(path))
            for t in needs_stub:
                fn = _find_function_at_line(tree2, t.line)
                if fn is None:
                    continue
                arg_names = _fully_undocumented_args(fn)
                if not arg_names:
                    continue
                if _apply_args_stub(lines, fn, arg_names):
                    file_changed = True
            source = "".join(lines)

        if file_changed and source != original:
            path.write_text(source)
            changed.append(rel_file)

    return changed
