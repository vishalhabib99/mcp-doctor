"""Static analysis of TypeScript/JavaScript MCP server implementations.

Mirrors analyzer.py's checks (description, per-parameter docs, error handling)
for the official TS SDK's two registration styles, plus the community
`fastmcp` (punkpeye/fastmcp) package's single-object style:

    server.registerTool(name, { description, inputSchema: ZodObjectOrConst }, handler)
    server.tool(name, description, zodShapeOrConst, handler)
    server.addTool({ name, description, parameters: ZodObjectOrConst, execute })

Both the config object and the Zod schema are commonly a same-file `const`
reference rather than an inline literal (see the official `everything`
reference server), so this resolves same-file identifiers before giving up.

Requires the optional `tree_sitter` / `tree_sitter_typescript` packages —
callers should treat their absence as "skip TS/JS analysis", not an error.
"""

from __future__ import annotations

from pathlib import Path

from .analyzer import ToolFinding, ToolIssue

try:
    from tree_sitter import Language, Node, Parser
    from tree_sitter_typescript import language_tsx, language_typescript

    TS_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via TS_AVAILABLE branch
    TS_AVAILABLE = False

REGISTER_METHODS = {"registerTool", "tool"}
# fastmcp's single-object style: server.addTool({ name, description, parameters, execute })
SINGLE_OBJECT_METHODS = {"addTool"}


def _text(node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _callee_name(node) -> str | None:
    """For `x.y(...)` return 'y'; for `y(...)` return 'y'."""
    if node.type != "call_expression":
        return None
    func = node.child_by_field_name("function")
    if func is None:
        return None
    if func.type == "member_expression":
        prop = func.child_by_field_name("property")
        return None if prop is None else prop.text.decode("utf-8", errors="ignore")
    if func.type == "identifier":
        return func.text.decode("utf-8", errors="ignore")
    return None


def _string_value(node, src: bytes) -> str | None:
    if node is None:
        return None
    if node.type == "string":
        frag = next((c for c in node.children if c.type == "string_fragment"), None)
        return _text(frag, src) if frag is not None else ""
    if node.type == "template_string":
        # Only trust a template literal with no ${...} interpolation.
        if any(c.type == "template_substitution" for c in node.children):
            return None
        frag = next((c for c in node.children if c.type == "string_fragment"), None)
        return _text(frag, src) if frag is not None else ""
    return None


def _object_pairs(node, src: bytes) -> dict[str, "Node"]:
    """For an `object` node, map property name -> value node. Skips computed/shorthand keys."""
    if node is None or node.type != "object":
        return {}
    pairs = {}
    for child in node.children:
        if child.type != "pair":
            continue
        key_node = child.child_by_field_name("key")
        value_node = child.child_by_field_name("value")
        if key_node is None or value_node is None:
            continue
        if key_node.type == "property_identifier":
            key = _text(key_node, src)
        elif key_node.type == "string":
            key = _string_value(key_node, src)
        else:
            continue
        if key is not None:
            pairs[key] = value_node
    return pairs


def _has_describe_call(node) -> bool:
    """True if `.describe(...)` appears anywhere in this expression's call chain."""
    for n in _walk(node):
        if n.type == "call_expression" and _callee_name(n) == "describe":
            return True
    return False


def _zod_object_arg(node):
    """Return the property-shape `object` node for a Zod schema argument, whether
    it's `z.object({...})` or a bare shape literal — the low-level SDK's
    `.tool(name, description, shape, handler)` takes the shape directly, not
    wrapped in `z.object(...)`."""
    if node is None:
        return None
    if node.type == "object":
        return node
    for n in _walk(node):
        if n.type == "call_expression" and _callee_name(n) == "object":
            args = n.child_by_field_name("arguments")
            if args is not None:
                for a in args.children:
                    if a.type == "object":
                        return a
    return None


def _find_try(node) -> bool:
    return any(n.type == "try_statement" for n in _walk(node))


def _collect_const_objects(tree_root, src: bytes) -> dict[str, "Node"]:
    """Map `const NAME = <expr>` at any scope to <expr>'s node, for resolving
    identifiers used as a config or schema argument."""
    registry: dict[str, "Node"] = {}
    for n in _walk(tree_root):
        if n.type != "variable_declarator":
            continue
        name_node = n.child_by_field_name("name")
        value_node = n.child_by_field_name("value")
        if name_node is not None and name_node.type == "identifier" and value_node is not None:
            registry[_text(name_node, src)] = value_node
    return registry


def _resolve(node, consts: dict[str, "Node"], depth: int = 0):
    if node is None or depth > 5:
        return node
    if node.type == "identifier":
        target = consts.get(node.text.decode("utf-8", errors="ignore"))
        return _resolve(target, consts, depth + 1) if target is not None else node
    if node.type == "binary_expression":
        operator = node.child_by_field_name("operator")
        # `paramName || "literal-default"` — a common optional-override-with-default
        # idiom. The literal is the name actually used at runtime unless a caller
        # overrides it, so resolve to that rather than treating the name as dynamic.
        if operator is not None and operator.text == b"||":
            right = node.child_by_field_name("right")
            if right is not None:
                return _resolve(right, consts, depth + 1)
    return node


def _analyze_ts_tool(
    name: str, config_or_desc, schema_arg, handler, consts: dict, src: bytes, file: str, line: int
) -> ToolFinding:
    config_or_desc = _resolve(config_or_desc, consts)
    schema_arg = _resolve(schema_arg, consts) if schema_arg is not None else None

    if config_or_desc is not None and config_or_desc.type == "object":
        pairs = _object_pairs(config_or_desc, src)
        description = _string_value(pairs.get("description"), src) or ""
        if schema_arg is None:
            # registerTool uses inputSchema; fastmcp's addTool uses parameters.
            schema_key = pairs.get("inputSchema") or pairs.get("parameters")
            if schema_key is not None:
                schema_arg = _resolve(schema_key, consts)
    else:
        description = _string_value(config_or_desc, src) or ""

    zod_obj = _zod_object_arg(schema_arg) if schema_arg is not None else None
    props = _object_pairs(zod_obj, src) if zod_obj is not None else {}
    param_count = len(props)
    documented = sum(1 for v in props.values() if _has_describe_call(v))

    has_try = _find_try(handler) if handler is not None else False

    finding = ToolFinding(
        name=name,
        file=file,
        line=line,
        has_description=bool(description.strip()),
        description_len=len(description.strip()),
        param_count=param_count,
        typed_param_count=param_count,  # Zod schemas are typed by construction
        has_docstring_params=documented >= param_count and param_count > 0,
        has_try_except=has_try,
        has_bare_except=False,
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

    if param_count and not finding.has_docstring_params:
        finding.issues.append(ToolIssue(
            name, file, line, "param_docs",
            f"{param_count - documented}/{param_count} Zod schema properties have no .describe(...) — "
            "the model only sees names, not intent.",
            "warning",
        ))

    if handler is not None and not has_try:
        finding.issues.append(ToolIssue(
            name, file, line, "error_handling",
            "No try/catch in this handler's own body. The MCP SDK still returns a structured "
            "error either way, but without a handler-level catch the model only sees the "
            "generic exception text rather than specific, actionable guidance.",
            "warning",
        ))

    return finding


def find_ts_tools(root: Path) -> tuple[list[ToolFinding], list[str]]:
    """Returns (findings, unparseable_relative_paths). Empty if tree_sitter isn't installed."""
    if not TS_AVAILABLE:
        return [], []

    ts_lang = Language(language_typescript())
    tsx_lang = Language(language_tsx())
    ts_parser = Parser(ts_lang)
    tsx_parser = Parser(tsx_lang)

    root = root.resolve()
    skip_dirs = {"node_modules", "dist", "build", ".next", "out"}
    files = []
    for p in root.rglob("*"):
        if p.suffix not in (".ts", ".tsx", ".js", ".jsx"):
            continue
        rel_parts = p.relative_to(root).parts
        if any(part in skip_dirs or part.startswith(".") for part in rel_parts):
            continue
        stem = p.stem.lower()
        if "test" in stem or "spec" in stem or "__tests__" in rel_parts:
            continue
        files.append(p)

    findings: list[ToolFinding] = []
    unparseable: list[str] = []

    for f in files:
        try:
            src = f.read_bytes()
        except OSError:
            continue
        parser = tsx_parser if f.suffix == ".tsx" else ts_parser
        tree = parser.parse(src)
        rel = str(f.relative_to(root))
        consts = _collect_const_objects(tree.root_node, src)

        for node in _walk(tree.root_node):
            if node.type != "call_expression":
                continue
            method = _callee_name(node)
            if method not in REGISTER_METHODS and method not in SINGLE_OBJECT_METHODS:
                continue
            args_node = node.child_by_field_name("arguments")
            if args_node is None:
                continue
            arg_nodes = [c for c in args_node.children if c.type not in ("(", ")", ",")]

            if method in SINGLE_OBJECT_METHODS:
                if len(arg_nodes) != 1:
                    continue
                config = _resolve(arg_nodes[0], consts)
                if config.type != "object":
                    continue
                pairs = _object_pairs(config, src)
                name_val = _string_value(_resolve(pairs.get("name"), consts), src)
                if name_val is None:
                    continue  # dynamic tool name — can't attribute a finding to it
                handler_val = pairs.get("execute")
                handler = handler_val if handler_val is not None and handler_val.type in (
                    "arrow_function", "function_expression"
                ) else None
                findings.append(
                    _analyze_ts_tool(name_val, config, None, handler, consts, src, rel, node.start_point[0] + 1)
                )
                continue

            if len(arg_nodes) < 3:
                continue
            name_val = _string_value(_resolve(arg_nodes[0], consts), src)
            if name_val is None:
                continue  # dynamic tool name — can't attribute a finding to it
            handler = arg_nodes[-1] if arg_nodes[-1].type in ("arrow_function", "function_expression") else None
            if method == "registerTool":
                config, schema = arg_nodes[1], None
            else:  # "tool": name, description, schema, handler
                config, schema = arg_nodes[1], arg_nodes[2] if len(arg_nodes) >= 4 else None
            findings.append(
                _analyze_ts_tool(name_val, config, schema, handler, consts, src, rel, node.start_point[0] + 1)
            )

    return findings, unparseable
