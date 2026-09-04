"""Static analysis of TypeScript/JavaScript MCP server implementations.

Mirrors analyzer.py's checks (description, per-parameter docs, error handling)
for the official TS SDK's two high-level registration styles, the community
`fastmcp` (punkpeye/fastmcp) package's single-object style, and the low-level
`Server` SDK's static-list style:

    server.registerTool(name, { description, inputSchema: ZodObjectOrConst }, handler)
    context.accountTool(name, { description, inputSchema: ZodObjectOrConst }, handler)  // same config shape as registerTool, wrapping it internally
    server.tool(name, description, zodShapeOrConst, handler)
    server.addTool({ name, description, parameters: ZodObjectOrConst, execute })
    server.setRequestHandler(ListToolsRequestSchema, () => ({ tools: [...ToolArrayConst] }))
    defineTool({ name, description, schema, handler })   // or definePageTool(...)
    defineTool(args => ({ name, description, schema, handler }))

The last style has no per-tool handler closure to check for a try/catch (one
generic dispatcher serves every tool by name, often proxying elsewhere
entirely), so error_handling is intentionally not checked for it.

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

REGISTER_METHODS = {"registerTool", "tool", "accountTool"}
# fastmcp's single-object style: server.addTool({ name, description, parameters, execute })
SINGLE_OBJECT_METHODS = {"addTool"}
# low-level Server SDK style: server.setRequestHandler(ListToolsRequestSchema, handler)
LIST_TOOLS_METHOD = "setRequestHandler"
LIST_TOOLS_SCHEMA = "ListToolsRequestSchema"
# a "define the tool, register it elsewhere" wrapper factory: the call itself
# *is* the definition site (e.g. Chrome DevTools MCP's `defineTool({...})` /
# `definePageTool({...})`), taking either an object literal directly or a
# function that returns one — the actual `server.registerTool(...)` call that
# consumes it is a runtime loop over a collected array, which doesn't need to
# be resolved since every `defineTool`/`definePageTool` call site already is
# one tool definition on its own.
WRAPPER_FACTORY_METHODS = {"defineTool", "definePageTool"}


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
        # Join every literal fragment, dropping `${...}` substitutions rather
        # than discarding the whole string. A description built as static
        # boilerplate plus one interpolated suffix (e.g. a shared
        # `${CMD_PREFIX_DESCRIPTION}` appended to every tool) is common and
        # still has real, checkable text — only the dynamic part is unknown,
        # and omitting it can only under-count length, never fabricate content.
        fragments = [_text(c, src) for c in node.children if c.type == "string_fragment"]
        return "".join(fragments)
    if node.type == "binary_expression":
        # `'...' + '...'` — a common way to wrap a long description across
        # multiple lines. Only resolves if both sides are themselves literal;
        # a concatenation involving a variable is left unresolved rather than
        # guessed at (better to under-count than to fabricate content).
        operator = node.child_by_field_name("operator")
        if operator is not None and operator.text == b"+":
            left_val = _string_value(node.child_by_field_name("left"), src)
            right_val = _string_value(node.child_by_field_name("right"), src)
            if left_val is not None and right_val is not None:
                return left_val + right_val
    return None


def _object_pairs(node, src: bytes) -> dict[str, "Node"]:
    """For an `object` node, map property name -> value node. Skips computed keys."""
    if node is None or node.type != "object":
        return {}
    pairs = {}
    for child in node.children:
        if child.type == "shorthand_property_identifier":
            # `{ tools }` — shorthand for `{ tools: tools }`. The node itself
            # is both the key name and a reference to the same-named local
            # variable, so it doubles as its own value node; `_resolve` treats
            # it exactly like a regular `identifier` when looking it up.
            pairs[_text(child, src)] = child
            continue
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


def _has_describe_call(node, src: bytes | None = None, consts: dict | None = None) -> bool:
    """True if `.describe(...)` appears anywhere in this expression's call chain.
    If consts is given, first resolves a bare identifier — a Zod schema is
    commonly factored into a shared const and reused across several tools —
    to its definition, so a shared schema's own `.describe(...)` isn't missed
    just because this particular property references it by name."""
    if consts is not None:
        node, _ = _resolve(node, src, consts)
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


def _zod_wrapped_schema(node, src: bytes, consts: dict):
    """If a raw-JSON-Schema `inputSchema` value is actually
    `zodToJsonSchema(SomeArgsSchema)` (the well-known `zod-to-json-schema`
    package), resolve to the underlying Zod schema argument so it can still be
    analyzed as one. Returns (node, src) — never None for the tuple itself,
    though the node may be None if there's nothing to resolve."""
    if node is None or node.type != "call_expression":
        return None, src
    func = node.child_by_field_name("function")
    if func is None or func.type != "identifier" or _text(func, src) != "zodToJsonSchema":
        return None, src
    args_node = node.child_by_field_name("arguments")
    if args_node is None:
        return None, src
    arg_nodes = [c for c in args_node.children if c.type not in ("(", ")", ",")]
    if not arg_nodes:
        return None, src
    return _resolve(arg_nodes[0], src, consts)


def _find_tools_array(handler_node, src: bytes):
    """Search a `setRequestHandler(ListToolsRequestSchema, handler)` handler body
    for the object literal it builds its response from (`{ tools: [...] }`,
    however it's returned) and return that `tools` property's raw value node."""
    for n in _walk(handler_node):
        if n.type == "object":
            tools_val = _object_pairs(n, src).get("tools")
            if tools_val is not None:
                return tools_val
    return None


def _collect_tool_array_elements(array_node, src: bytes, consts: dict, depth: int = 0):
    """Return [(tool_object_node, its_src), ...] for every literal `Tool` object
    a `tools` array directly contains, following `...someConstArray` spreads
    (repo-wide, via `consts`) into their own elements recursively. A spread of
    something that doesn't resolve to an array literal (e.g. a function call's
    result, built at runtime) is genuinely dynamic and is skipped, not guessed at.
    """
    if array_node is None or array_node.type != "array" or depth > 5:
        return []
    out: list[tuple["Node", bytes]] = []
    for child in array_node.children:
        if child.type == "object":
            out.append((child, src))
        elif child.type == "spread_element":
            inner = child.children[-1] if child.children else None
            if inner is not None:
                resolved, resolved_src = _resolve(inner, src, consts)
                if resolved.type == "array":
                    out.extend(_collect_tool_array_elements(resolved, resolved_src, consts, depth + 1))
    return out


def _extract_definition_object(node, src: bytes):
    """For a `defineTool`/`definePageTool` call's single argument, return the
    tool-definition `object` literal — whether passed directly, or built by a
    factory function (`args => ({...})` or `args => { return {...}; }`).
    A function body with no top-level `return {...}` is genuinely dynamic
    (e.g. conditional returns) and yields (None, src) rather than a guess."""
    if node is None:
        return None, src
    if node.type == "object":
        return node, src
    if node.type not in ("arrow_function", "function_expression"):
        return None, src
    body = node.child_by_field_name("body")
    if body is None:
        return None, src
    if body.type == "object":
        return body, src
    if body.type == "parenthesized_expression":
        inner = next((c for c in body.children if c.type == "object"), None)
        return (inner, src) if inner is not None else (None, src)
    if body.type == "statement_block":
        for child in body.children:
            if child.type != "return_statement":
                continue
            for c in child.children:
                if c.type == "object":
                    return c, src
                if c.type == "parenthesized_expression":
                    inner = next((x for x in c.children if x.type == "object"), None)
                    if inner is not None:
                        return inner, src
    return None, src


def _find_try(node) -> bool:
    return any(n.type == "try_statement" for n in _walk(node))


def _collect_const_objects(tree_root, src: bytes) -> dict[str, tuple["Node", bytes]]:
    """Map `const NAME = <expr>` at any scope to (<expr>'s node, this file's src),
    for resolving identifiers used as a config or schema argument. The src travels
    with the node since a name can be resolved via the cross-file registry in
    `find_ts_tools`, at which point it belongs to a different file's byte buffer.

    Name-based, not scope-aware: if the same name is declared more than once in
    this file (two unrelated local variables in two different functions, say),
    there's no way to know which declaration a given reference actually means —
    so that name is left out of the registry entirely rather than silently
    resolved to whichever declaration happened to be walked last. An identifier
    that isn't in the registry is left unresolved by `_resolve`, which is the
    same safe fallback already used for a genuinely dynamic value; the risk
    being avoided here is worse than under-reporting — resolving to the *wrong*
    same-named variable's value and reporting it as fact.
    """
    registry: dict[str, tuple["Node", bytes]] = {}
    ambiguous: set[str] = set()
    for n in _walk(tree_root):
        if n.type != "variable_declarator":
            continue
        name_node = n.child_by_field_name("name")
        value_node = n.child_by_field_name("value")
        if name_node is None or name_node.type != "identifier" or value_node is None:
            continue
        name = _text(name_node, src)
        if name in ambiguous:
            continue
        if name in registry and registry[name][0] is not value_node:
            ambiguous.add(name)
            del registry[name]
            continue
        registry[name] = (value_node, src)
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
    if node.type in ("identifier", "shorthand_property_identifier"):
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
    if node.type == "call_expression":
        # `allTools.filter(tool => shouldIncludeTool(tool.name))` — a common way
        # to conditionally hide some tools from a base list at list-time. The
        # predicate can't be evaluated statically, but filtering never invents a
        # tool or changes its definition, only whether it's visible at runtime —
        # so for auditing purposes, resolve straight through to the base array
        # rather than treating the whole list as dynamic and skipping everything
        # in it.
        func = node.child_by_field_name("function")
        if func is not None and func.type == "member_expression":
            prop = func.child_by_field_name("property")
            obj_node = func.child_by_field_name("object")
            if prop is not None and prop.type == "property_identifier" and obj_node is not None:
                if _text(prop, src) == "filter":
                    return _resolve(obj_node, src, consts, depth + 1)
    return node, src


def _resolve_str(node, src: bytes, consts: dict[str, tuple["Node", bytes]]) -> str | None:
    if node is None:
        return None
    resolved, resolved_src = _resolve(node, src, consts)
    return _string_value(resolved, resolved_src)


def _finding_with_description_and_param_issues(
    name: str, file: str, line: int, description: str, param_count: int, documented: int,
    param_doc_label: str,
) -> ToolFinding:
    """Build a ToolFinding with the description/param-docs issues that are common
    across every TS registration style. Caller fills in and appends the
    error_handling issue (or omits it), since not every style has a per-tool
    handler to inspect for one."""
    finding = ToolFinding(
        name=name,
        file=file,
        line=line,
        has_description=bool(description.strip()),
        description_len=len(description.strip()),
        param_count=param_count,
        typed_param_count=param_count,
        has_docstring_params=documented >= param_count and param_count > 0,
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

    if param_count and not finding.has_docstring_params:
        finding.issues.append(ToolIssue(
            name, file, line, "param_docs",
            f"{param_count - documented}/{param_count} {param_doc_label} — "
            "the model only sees names, not intent.",
            "warning",
        ))

    return finding


def _analyze_ts_tool(
    name: str, config_or_desc, config_src: bytes, schema_arg, schema_src: bytes,
    handler, consts: dict, file: str, line: int
) -> ToolFinding:
    if config_or_desc is not None and config_or_desc.type == "object":
        pairs = _object_pairs(config_or_desc, config_src)
        description = _resolve_str(pairs.get("description"), config_src, consts) or ""
        if schema_arg is None:
            # registerTool uses inputSchema; fastmcp's addTool uses parameters;
            # the defineTool/definePageTool wrapper style uses schema.
            schema_key = pairs.get("inputSchema") or pairs.get("parameters") or pairs.get("schema")
            if schema_key is not None:
                schema_arg, schema_src = _resolve(schema_key, config_src, consts)
    else:
        description = _string_value(config_or_desc, config_src) or ""

    zod_obj = _zod_object_arg(schema_arg) if schema_arg is not None else None
    props = _object_pairs(zod_obj, schema_src) if zod_obj is not None else {}
    param_count = len(props)
    documented = sum(1 for v in props.values() if _has_describe_call(v, schema_src, consts))

    has_try = _find_try(handler) if handler is not None else False

    finding = _finding_with_description_and_param_issues(
        name, file, line, description, param_count, documented,
        "Zod schema properties have no .describe(...)",
    )
    finding.has_try_except = has_try

    if handler is not None and not has_try:
        finding.issues.append(ToolIssue(
            name, file, line, "error_handling",
            "No try/catch in this handler's own body. The MCP SDK still returns a structured "
            "error either way, but without a handler-level catch the model only sees the "
            "generic exception text rather than specific, actionable guidance.",
            "warning",
        ))

    return finding


def _analyze_json_schema_tool(
    name: str, desc_node, schema_node, schema_src: bytes, consts: dict, src: bytes, file: str, line: int
) -> ToolFinding:
    """For the low-level `Server` SDK's `setRequestHandler(ListToolsRequestSchema, ...)`
    style: tools are plain `Tool` objects (raw JSON Schema, not Zod) returned from
    a static or const-referenced array, not individual `registerTool`/`.tool()`
    call sites. There's no per-tool handler closure to inspect for a try/catch —
    a single generic dispatcher (keyed by name, often proxying to a different
    process entirely, as with a Chrome-extension-backed server) serves every
    tool — so error_handling is deliberately not checked for this style."""
    description = _resolve_str(desc_node, src, consts) or ""

    schema, resolved_schema_src = (
        _resolve(schema_node, schema_src, consts) if schema_node is not None else (None, schema_src)
    )

    param_count = 0
    documented = 0
    param_doc_label = "JSON-schema properties have no description"

    if schema is not None and schema.type == "object":
        properties_node = _object_pairs(schema, resolved_schema_src).get("properties")
        if properties_node is not None:
            resolved_props, resolved_props_src = _resolve(properties_node, resolved_schema_src, consts)
            if resolved_props.type == "object":
                props = _object_pairs(resolved_props, resolved_props_src)
                param_count = len(props)
                for v in props.values():
                    v_resolved, v_resolved_src = _resolve(v, resolved_props_src, consts)
                    if v_resolved.type == "object":
                        prop_desc = _object_pairs(v_resolved, v_resolved_src).get("description")
                        if _string_value(prop_desc, v_resolved_src):
                            documented += 1
    elif schema is not None and schema.type == "call_expression":
        # `inputSchema: zodToJsonSchema(SomeArgsSchema)` — the well-known
        # zod-to-json-schema package, used to keep one Zod schema as the single
        # source of truth while serving raw JSON Schema over the low-level SDK.
        # Unwrap to the underlying Zod schema so param docs are still checked,
        # rather than going blind on every tool that uses this (common) idiom.
        zod_node, zod_src = _zod_wrapped_schema(schema, resolved_schema_src, consts)
        zod_obj = _zod_object_arg(zod_node)
        if zod_obj is not None:
            zod_props = _object_pairs(zod_obj, zod_src)
            param_count = len(zod_props)
            documented = sum(1 for v in zod_props.values() if _has_describe_call(v, zod_src, consts))
            param_doc_label = "Zod schema properties have no .describe(...)"

    return _finding_with_description_and_param_issues(
        name, file, line, description, param_count, documented, param_doc_label,
    )


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
        # Directory-based exclusion matches the Python analyzer's `_is_test_file`
        # (any "test"/"tests" path segment) — verified against a real miss:
        # mcp-use/mcp-use's `libraries/typescript/packages/agent/tests/servers/
        # simple_server.ts`, a genuine test fixture ("Minimal stdio MCP server
        # ... for agent integration tests") whose filename stem alone
        # (`simple_server`) and directory (`tests`, not Jest's `__tests__`)
        # both slipped past the old check.
        if "test" in stem or "spec" in stem or any(part in ("test", "tests", "__tests__") for part in rel_parts):
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

    # Maps each file's src buffer (by identity — buffers are never copied, only
    # passed around by reference through resolution) back to its relative path,
    # so a tool object resolved from a static array can be reported at its own
    # definition site rather than the (possibly different-file) call site.
    src_to_rel: dict[int, str] = {id(s): str(f.relative_to(root)) for f, _, s in parsed}
    seen_list_tools: set[tuple[str, str, int]] = set()

    for f, file_root, src in parsed:
        rel = str(f.relative_to(root))
        local_consts = _collect_const_objects(file_root, src)
        consts = {**global_consts, **local_consts}

        for node in _walk(file_root):
            if node.type != "call_expression":
                continue
            method = _callee_name(node)
            if (
                method not in REGISTER_METHODS
                and method not in SINGLE_OBJECT_METHODS
                and method not in WRAPPER_FACTORY_METHODS
                and method != LIST_TOOLS_METHOD
            ):
                continue
            args_node = node.child_by_field_name("arguments")
            if args_node is None:
                continue
            arg_nodes = [c for c in args_node.children if c.type not in ("(", ")", ",")]

            if method == LIST_TOOLS_METHOD:
                if len(arg_nodes) < 2 or arg_nodes[0].type != "identifier":
                    continue
                if _text(arg_nodes[0], src) != LIST_TOOLS_SCHEMA:
                    continue
                tools_array_raw = _find_tools_array(arg_nodes[1], src)
                if tools_array_raw is None:
                    continue
                tools_array, tools_array_src = _resolve(tools_array_raw, src, consts)
                for tool_obj, tool_src in _collect_tool_array_elements(tools_array, tools_array_src, consts):
                    pairs = _object_pairs(tool_obj, tool_src)
                    name_val = _resolve_str(pairs.get("name"), tool_src, consts)
                    if name_val is None:
                        continue  # dynamic tool name — can't attribute a finding to it
                    tool_file = src_to_rel.get(id(tool_src), rel)
                    tool_line = tool_obj.start_point[0] + 1
                    # The same static tool list is commonly wired into more than
                    # one setRequestHandler call site (e.g. separate stdio/HTTP
                    # transport entrypoints) — dedupe by the tool's own
                    # definition, not the call site, so it's reported once.
                    dedup_key = (name_val, tool_file, tool_line)
                    if dedup_key in seen_list_tools:
                        continue
                    seen_list_tools.add(dedup_key)
                    findings.append(
                        _analyze_json_schema_tool(
                            name_val, pairs.get("description"), pairs.get("inputSchema"), tool_src,
                            consts, tool_src, tool_file, tool_line,
                        )
                    )
                continue

            if method in WRAPPER_FACTORY_METHODS:
                if len(arg_nodes) != 1:
                    continue
                definition, definition_src = _extract_definition_object(arg_nodes[0], src)
                if definition is None:
                    continue
                definition, definition_src = _resolve(definition, definition_src, consts)
                if definition.type != "object":
                    continue
                pairs = _object_pairs(definition, definition_src)
                name_val = _resolve_str(pairs.get("name"), definition_src, consts)
                if name_val is None:
                    continue  # dynamic tool name — can't attribute a finding to it
                handler_val = pairs.get("handler")
                handler = handler_val if handler_val is not None and handler_val.type in (
                    "arrow_function", "function_expression"
                ) else None
                findings.append(
                    _analyze_ts_tool(
                        name_val, definition, definition_src, None, definition_src, handler, consts,
                        rel, node.start_point[0] + 1,
                    )
                )
                continue

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
            if method in ("registerTool", "accountTool"):
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
