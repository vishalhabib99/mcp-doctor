"""Auto-fix for the subset of mcp-doctor findings that are safe to apply
mechanically, without human judgment:

- a bare ``except:`` narrowed to ``except Exception:``
- a missing ``Args:`` docstring section stubbed in for a tool whose params
  have *no* documentation at all (docstring or ``Field(description=...)``)
- the undocumented params of a tool that already has a Google-style
  ``Args:`` section appended to the end of that section
- a param that defaults to ``None`` but is typed as a bare non-nullable type
  (``name: str = None``) re-typed as ``str | None``, or ``Optional[str]`` when
  the file already imports ``Optional`` from typing; inside ``Annotated[...]``
  only the first element is wrapped, so the metadata stays put

Deliberately does not touch: missing/short descriptions (can't fabricate
real intent), untyped parameters (can't infer real types), wrapping a
function body in try/except (too invasive to do safely), or a partial
``:param:``/NumPy docstring (only the Google ``Args:`` layout is merged into).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .analyzer import (
    Report,
    ToolFinding,
    _annotation_base_name,
    _annotation_rejects_none,
    _field_call_has_description,
    _get_docstring_sections,
    _get_numpy_documented_params,
    _get_sphinx_documented_params,
    _is_none_default,
)

_DOCSTRING_QUOTE = re.compile(r'^(\s*)(\'\'\'|""")(.*)\2\s*\n?$')
_CLOSING_QUOTE_ONLY = re.compile(r'^(\s*)(\'\'\'|""")\s*\n?$')
_CLOSING_QUOTE_TRAILING = re.compile(r'^(.*?)(\'\'\'|""")\s*\n?$')
# Same headings analyzer._ARGS_HEADING accepts, minus the Markdown variants:
# only a plain Google-style section is safe to append entries to.
_GOOGLE_ARGS_HEADING = re.compile(r"^\s*(Args|Arguments|Params|Parameters):\s*\n?$")
_PARTIAL_DOCS_MISSING = re.compile(r"^\d+ of \d+ parameters aren't documented: (.+?) — ")


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


def _char_offset(lines: list[str], lineno: int, byte_col: int) -> int:
    """Absolute character offset of an AST (lineno, col_offset) position;
    col_offset is in UTF-8 bytes, so a non-ASCII line needs converting."""
    line = lines[lineno - 1]
    col = len(line.encode("utf-8")[:byte_col].decode("utf-8", errors="ignore"))
    return sum(len(l) for l in lines[: lineno - 1]) + col


def _imports_optional(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom) and node.module == "typing"
        and any(alias.name == "Optional" and alias.asname is None for alias in node.names)
        for node in ast.walk(tree)
    )


def _nullable_target(annotation: ast.expr) -> ast.expr:
    """The part of the annotation to make nullable: the first element of
    `Annotated[T, ...]`, otherwise the whole annotation."""
    if isinstance(annotation, ast.Subscript) and _annotation_base_name(annotation.value) == "Annotated":
        sl = annotation.slice
        return sl.elts[0] if isinstance(sl, ast.Tuple) else sl
    return annotation


def _fix_none_defaults(source: str, tree: ast.Module, findings: list[ToolFinding]) -> tuple[str, int]:
    flagged_lines = {t.line for t in findings if any(i.check == "none_default_type" for i in t.issues)}
    if not flagged_lines:
        return source, 0
    lines = source.splitlines(keepends=True)
    use_optional = _imports_optional(tree)
    spans: list[tuple[int, int]] = []
    for line in flagged_lines:
        fn = _find_function_at_line(tree, line)
        if fn is None:
            continue
        all_args = fn.args.args
        defaults_by_arg = dict(zip(all_args[len(all_args) - len(fn.args.defaults):], fn.args.defaults))
        for a in all_args:
            if a.arg in ("self", "cls"):
                continue
            if not (_is_none_default(defaults_by_arg.get(a)) and _annotation_rejects_none(a.annotation)):
                continue
            target = _nullable_target(a.annotation)
            start = _char_offset(lines, target.lineno, target.col_offset)
            end = _char_offset(lines, target.end_lineno, target.end_col_offset)
            spans.append((start, end))
    for start, end in sorted(set(spans), reverse=True):
        text = source[start:end]
        new = f"Optional[{text}]" if use_optional else f"{text} | None"
        source = source[:start] + new + source[end:]
    return source, len(set(spans))


def _missing_from_partial_docs(finding: ToolFinding) -> list[str]:
    """The params the analyzer named as undocumented on a partially
    documented tool. Read from its param_docs issue so this fix agrees with
    the analyzer on what counts (exclude_args, injected Context, Field docs)."""
    for issue in finding.issues:
        if issue.check != "param_docs":
            continue
        m = _PARTIAL_DOCS_MISSING.match(issue.message)
        if m:
            return [n.strip() for n in m.group(1).split(",") if n.strip()]
    return []


def _append_to_args_section(lines: list[str], fn: ast.FunctionDef | ast.AsyncFunctionDef, arg_names: list[str]) -> bool:
    if not fn.body:
        return False
    doc_expr = fn.body[0]
    if not (isinstance(doc_expr, ast.Expr) and isinstance(doc_expr.value, ast.Constant) and isinstance(doc_expr.value.value, str)):
        return False
    start_idx = doc_expr.lineno - 1
    end_idx = doc_expr.end_lineno - 1
    headings = [i for i in range(start_idx + 1, end_idx) if _GOOGLE_ARGS_HEADING.match(lines[i])]
    if len(headings) != 1:
        return False  # none, or two Args: sections — ambiguous, leave it to a human
    heading_idx = headings[0]
    heading_indent = len(_indent_of(lines[heading_idx]))

    # The section runs until a less-or-equally indented line or the closing
    # quote; a blank line only ends it when what follows is no longer indented
    # under the heading — the same boundary analyzer._get_docstring_sections uses.
    last_idx = heading_idx
    entry_indent = None
    for i in range(heading_idx + 1, end_idx + 1):
        line = lines[i]
        if not line.strip():
            continue
        if len(_indent_of(line)) <= heading_indent:
            break
        if '"""' in line or "'''" in line:
            return False  # an entry that closes the docstring on its own line — not safe to append after
        if entry_indent is None:
            entry_indent = _indent_of(line)
        last_idx = i
    if entry_indent is None:
        return False
    section = "".join(lines[heading_idx + 1:last_idx + 1])
    if any(re.search(rf"\b{re.escape(a)}\b", section) for a in arg_names):
        return False  # already mentioned in some form the analyzer can't parse — a human should merge it

    new_entries = [f"{entry_indent}{a}: TODO: describe this parameter.\n" for a in arg_names]
    lines[last_idx + 1:last_idx + 1] = new_entries
    return True


def plan_fixes(root: Path, report: Report) -> dict[str, tuple[str, str]]:
    """Compute the safe subset of fixes without writing anything. Returns
    {relative path: (original source, fixed source)} for each file that
    would change."""
    planned: dict[str, tuple[str, str]] = {}

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

        # Re-typing a None default only edits text inside the signature, so it
        # runs first; the docstring fixes below insert lines and re-parse.
        source, _ = _fix_none_defaults(original, tree, findings)
        tree = ast.parse(source, filename=str(path))

        source, _ = _fix_bare_except_in_source(source, tree)

        needs_docs = sorted(
            (t for t in findings if t.param_count > 0 and not t.has_docstring_params),
            key=lambda t: t.line,
            reverse=True,  # bottom-up, so inserting lines never shifts a function not yet fixed
        )
        if needs_docs:
            lines = source.splitlines(keepends=True)
            tree2 = ast.parse(source, filename=str(path))
            for t in needs_docs:
                fn = _find_function_at_line(tree2, t.line)
                if fn is None:
                    continue
                missing = _missing_from_partial_docs(t)
                if missing:
                    _append_to_args_section(lines, fn, missing)
                    continue
                arg_names = _fully_undocumented_args(fn)
                if arg_names:
                    _apply_args_stub(lines, fn, arg_names)
            source = "".join(lines)

        if source != original:
            ast.parse(source, filename=str(path))  # never hand back a file that no longer parses
            planned[rel_file] = (original, source)

    return planned


def apply_fixes(root: Path, report: Report) -> list[str]:
    """Apply the safe subset of fixes in place. Returns the relative paths
    of files that were changed."""
    planned = plan_fixes(root, report)
    for rel_file, (_, fixed) in planned.items():
        (root / rel_file).write_text(fixed)
    return list(planned)
