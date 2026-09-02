"""Static analysis of TypeScript/JavaScript MCP server implementations.

Mirrors analyzer.py's checks (description, per-parameter docs, error handling)
for the official TS SDK's two registration styles, plus the community
`fastmcp` (punkpeye/fastmcp) package's single-object style:

    server.registerTool(name, { description, inputSchema: ZodObjectOrConst }, handler)
    server.tool(name, description, zodShapeOrConst, handler)
    server.addTool({ name, description, parameters: ZodObjectOrConst, execute })

The name, config object, and Zod schema are commonly a `const` reference
rather than an inline literal, and the name is often a member-expression
property access on an exported tool-definition object (e.g.
`server.registerTool(fooTool.name, ...)` where `fooTool` is defined and
exported from another file) — this resolves identifiers, `||`-default
expressions, and cross-file member-expression property lookups before
giving up.

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


def _collect_const_objects(tree_root, src: bytes) -> dict[str, tuple["Node", bytes]]:
    """Map `const NAME = <expr>` at any scope to (<expr>'s node, this file's src),
    for resolving identifiers used as a config or schema argument. The src travels
    with the node since a name can be resolved via the cross-file registry in
    `find_ts_tools`, at which point it belongs to a different file's byte buffer."""
    registry: dict[str, tuple["Node", bytes]] = {}
    for n in _walk(tree_root):
        if n.type != "variable_declarator":
            continue
        name_node = n.child_by_field_name("name")
        value_node = n.child_by_field_name("value")
        if name_node is not None and name_node.type == "identifier" and value_node is not None:
            registry[_text(name_node, src)] = (value_node, src)
    return registry


def _resolve(node, src: bytes, consts: dict[str, tuple["Node", bytes]], depth: int = 0):
    """Resolve an identifier/member-expression/`||`-default down to a literal
    node, returning (resolved_node, its_src) since resolution can cross files."""
    if node is None or depth > 5:
        return node, src
    if node.type in ("as_expression", "satisfies_expression"):
        # `{ ... } as const` / `{ ... } satisfies ToolConfig` — very common on an
        # exported tool-definition object literal; the expression being asserted
        # is always the first child. Unwrap to keep resolving through it.
        inner = node.children[0] if node.children else None
        return _resolve(inner, src, consts, depth + 1) if inner is not None else (node, src)
    if node.type == "identifier":
        target = consts.get(node.text.decode("utf-8", errors="ignore"))
        if target is not None:
            target_node, target_src = target
            return _resolve(target_node, target_src, consts, depth + 1)
        return node, src
    if node.type == "binary_expression":
        operator = node.child_by_field_name("operator")
        # `paramName || "literal-default"` — a common optional-override-with-default
        # idiom. The literal is the name actually used at runtime unless a caller
        # overrides it, so resolve to that rather than treating the name as dynamic.
        if operator is not None and operator.text == b"||":
            right = node.child_by_field_name("right")
            if right is not None:
                return _resolve(right, src, consts, depth + 1)
    if node.type == "member_expression":
        # `fooTool.name` — a common pattern where a tool's config/schema is an
        # exported object literal defined (often in another file) and referenced
        # by property access at the registration call site, rather than spread
        # or destructured. Resolve the object, then look up the property on it.
        prop_node = node.child_by_field_name("property")
        obj_node = node.child_by_field_name("object")
        if prop_node is not None and prop_node.type == "property_identifier" and obj_node is not None:
            resolved_obj, resolved_src = _resolve(obj_node, src, consts, depth + 1)
            if resolved_obj.type == "object":
                pairs = _object_pairs(resolved_obj, resolved_src)
                prop_val = pairs.get(_text(prop_node, src))
                if prop_val is not None:
                    return _resolve(prop_val, resolved_src, consts, depth + 1)
    return node, src


def _resolve_str(node, src: bytes, consts: dict[str, tuple["Node", bytes]]) -> str | None:
    if node is None:
        return None
    resolved, resolved_src = _resolve(node, src, consts)
    return _string_value(resolved, resolved_src)


def _analyze_ts_tool(
    name: str, config_or_desc, config_src: bytes, schema_arg, schema_src: bytes,
    handler, consts: dict, file: str, line: int
) -> ToolFinding:
    if config_or_desc is not None and config_or_desc.type == "object":
        pairs = _object_pairs(config_or_desc, config_src)
        description = _resolve_str(pairs.get("description"), config_src, consts) or ""
        if schema_arg is None:
            # registerTool uses inputSchema; fastmcp's addTool uses parameters.
            schema_key = pairs.get("inputSchema") or pairs.get("parameters")
            if schema_key is not None:
                schema_arg, schema_src = _resolve(schema_key, config_src, consts)
    else:
        description = _string_value(config_or_desc, config_src) or ""

    zod_obj = _zod_object_arg(schema_arg) if schema_arg is not None else None
    props = _object_pairs(zod_obj, schema_src) if zod_obj is not None else {}
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
    parsed: list[tuple[Path, "Node", bytes]] = []

    for f in files:
        try:
            src = f.read_bytes()
        except OSError:
            continue
        parser = tsx_parser if f.suffix == ".tsx" else ts_parser
        tree = parser.parse(src)
        parsed.append((f, tree.root_node, src))

    # Repo-wide, name-based registry of `const NAME = {...}` object literals, so
    # a tool's name/config can be resolved even when it's referenced from another
    # file (e.g. `server.registerTool(fooTool.name, ...)` where `fooTool` is
    # exported from a different module and re-exported through a barrel file).
    # Name-based, not full import-resolved — same simplification already used
    # for the Python side's cross-file Field-alias registry.
    global_consts: dict[str, tuple["Node", bytes]] = {}
    for _f, file_root, file_src in parsed:
        for const_name, entry in _collect_const_objects(file_root, file_src).items():
            global_consts.setdefault(const_name, entry)

    for f, file_root, src in parsed:
        rel = str(f.relative_to(root))
        local_consts = _collect_const_objects(file_root, src)
        consts = {**global_consts, **local_consts}

        for node in _walk(file_root):
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
                config, config_src = _resolve(arg_nodes[0], src, consts)
                if config.type != "object":
                    continue
                pairs = _object_pairs(config, config_src)
                name_val = _resolve_str(pairs.get("name"), config_src, consts)
                if name_val is None:
                    continue  # dynamic tool name — can't attribute a finding to it
                handler_val = pairs.get("execute")
                handler = handler_val if handler_val is not None and handler_val.type in (
                    "arrow_function", "function_expression"
                ) else None
                findings.append(
                    _analyze_ts_tool(
                        name_val, config, config_src, None, config_src, handler, consts,
                        rel, node.start_point[0] + 1,
                    )
                )
                continue

            if len(arg_nodes) < 3:
                continue
            name_val = _resolve_str(arg_nodes[0], src, consts)
            if name_val is None:
                continue  # dynamic tool name — can't attribute a finding to it
            handler = arg_nodes[-1] if arg_nodes[-1].type in ("arrow_function", "function_expression") else None
            if method == "registerTool":
                config_raw, schema_raw = arg_nodes[1], None
            else:  # "tool": name, description, schema, handler
                config_raw, schema_raw = arg_nodes[1], arg_nodes[2] if len(arg_nodes) >= 4 else None
            config, config_src = _resolve(config_raw, src, consts)
            schema_arg, schema_src = (
                _resolve(schema_raw, src, consts) if schema_raw is not None else (None, src)
            )
            findings.append(
                _analyze_ts_tool(
                    name_val, config, config_src, schema_arg, schema_src, handler, consts,
                    rel, node.start_point[0] + 1,
                )
            )

    return findings, unparseable
