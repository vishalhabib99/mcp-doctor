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
    server.setRequestHandler(ListToolsRequestSchema, () => ({ tools: Object.values(toolsNamespaceImport) }))
    defineTool({ name, description, schema, handler })   // or definePageTool(...)
    defineTool(args => ({ name, description, schema, handler }))
    const fooTool = { schema: { name, description, inputSchema }, handle }  // no wrapping call at all

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

import re
from pathlib import Path

from .analyzer import ToolFinding, ToolIssue, description_display_width

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
# mcp-framework's tool base class: `class FooTool extends MCPTool { name; description; schema }`
MCP_FRAMEWORK_BASE = "MCPTool"


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


def _find_tools_array(handler_node, src: bytes, local_funcs: dict | None = None, depth: int = 0):
    """Search a `setRequestHandler(ListToolsRequestSchema, handler)` handler body
    for the object literal it builds its response from (`{ tools: [...] }`,
    however it's returned) and return that `tools` property's raw value node.
    A handler that only calls a same-file helper (`async () => listTools()`)
    is followed into that function (chrisryugj/korean-law-mcp, 99 tools)."""
    for n in _walk(handler_node):
        if n.type == "object":
            tools_val = _object_pairs(n, src).get("tools")
            if tools_val is not None:
                return tools_val
    if local_funcs and depth < 2:
        for n in _walk(handler_node):
            if n.type != "call_expression":
                continue
            func = n.child_by_field_name("function")
            if func is None or func.type != "identifier":
                continue
            target = local_funcs.get(_text(func, src))
            if target is not None and target is not handler_node:
                found = _find_tools_array(target, src, local_funcs, depth + 1)
                if found is not None:
                    return found
    return None


def _raw_tools_list_branch(node, src: bytes):
    """If `node` is a hand-rolled JSON-RPC dispatch branch for `tools/list` —
    `case 'tools/list':` in a switch, or `if (method === 'tools/list') {...}` —
    return the node whose body builds the response; otherwise None."""
    if node.type == "switch_case":
        value = node.child_by_field_name("value")
        if value is not None and _string_value(value, src) == "tools/list":
            return node
        return None
    if node.type == "if_statement":
        cond = node.child_by_field_name("condition")
        if cond is None:
            return None
        for n in _walk(cond):
            if n.type != "binary_expression":
                continue
            op = n.child_by_field_name("operator")
            if op is None or _text(op, src) not in ("===", "=="):
                continue
            for side in ("left", "right"):
                v = n.child_by_field_name(side)
                if v is not None and v.type == "string" and _string_value(v, src) == "tools/list":
                    return node.child_by_field_name("consequence")
    return None


def _map_projection_receiver(node, src: bytes):
    """For `tools: someTools.map(t => ({ name: t.name, ... }))`, return the
    array being mapped (`someTools`), or None if `node` isn't that shape.
    Only a callback that returns an object with a `name` key counts: that's
    a projection of each registry entry into a `Tool`, one tool per element."""
    if node is None or node.type != "call_expression":
        return None
    func = node.child_by_field_name("function")
    if func is None or func.type != "member_expression":
        return None
    prop = func.child_by_field_name("property")
    if prop is None or _text(prop, src) != "map":
        return None
    args_node = node.child_by_field_name("arguments")
    if args_node is None:
        return None
    call_args = [c for c in args_node.children if c.type not in ("(", ")", ",")]
    if len(call_args) != 1:
        return None
    projected, _ = _extract_definition_object(call_args[0], src)
    if projected is None or "name" not in _object_pairs(projected, src):
        return None
    return func.child_by_field_name("object")


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
        elif child.type == "identifier":
            # `tools: [tool]` with `const tool = { name, description, inputSchema }`
            # defined elsewhere (hustcc/mcp-mermaid). Only a const that resolves
            # to an object literal counts; anything built at runtime is skipped.
            resolved, resolved_src = _resolve(child, src, consts)
            if resolved is not None and resolved.type == "object":
                out.append((resolved, resolved_src))
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


def _collect_function_declarations(tree_root, src: bytes) -> dict[str, "Node"]:
    """Map a named `function foo(...) {...}` (or `async function foo(...) {...}`)
    declaration's name to its own node, for resolving a `handle: foo`-style
    identifier reference to the actual function body. Same file-local,
    ambiguous-name-safe convention as `_collect_const_objects` — a name
    declared more than once is left out of the registry rather than guessed.
    Deliberately not merged into that registry: a `function_declaration` has
    no `variable_declarator` wrapper to walk."""
    registry: dict[str, "Node"] = {}
    ambiguous: set[str] = set()
    for n in _walk(tree_root):
        if n.type != "function_declaration":
            continue
        name_node = n.child_by_field_name("name")
        if name_node is None:
            continue
        name = _text(name_node, src)
        if name in ambiguous:
            continue
        if name in registry and registry[name] is not n:
            ambiguous.add(name)
            del registry[name]
            continue
        registry[name] = n
    return registry


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
        if n.type == "enum_declaration":
            # `enum MCPToolName { GET_POINTED_ELEMENT = 'get-pointed-element' }`
            # (etsd-tech/mcp-pointer): `MCPToolName.X` resolves through `_resolve`.
            name_node, value_node = n.child_by_field_name("name"), n
        elif n.type == "variable_declarator":
            name_node = n.child_by_field_name("name")
            value_node = n.child_by_field_name("value")
        else:
            continue
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


def _collect_namespace_imports(tree_root, src: bytes) -> dict[str, str]:
    """Map each `import * as NAME from "SPEC"` namespace import's local NAME to
    its raw module specifier, so `Object.values(NAME)` (a real pattern — a
    module exporting one `const` per tool, collected as a namespace object and
    turned into an array — verified against zcaceres/markdownify-mcp and
    flesler/mcp-tasks) can be traced to the module whose exports it wraps."""
    out: dict[str, str] = {}
    for n in tree_root.children:
        if n.type != "import_statement":
            continue
        clause = next((c for c in n.children if c.type == "import_clause"), None)
        source_node = next((c for c in n.children if c.type == "string"), None)
        if clause is None or source_node is None:
            continue
        ns = next((c for c in clause.children if c.type == "namespace_import"), None)
        if ns is None:
            continue
        ident = next((c for c in ns.children if c.type == "identifier"), None)
        spec = _string_value(source_node, src)
        if ident is not None and spec is not None:
            out[_text(ident, src)] = spec
    return out


def _object_values_arg(node) -> "Node | None":
    """For `Object.values(X)`, return the `X` argument node, else None."""
    if node is None or node.type != "call_expression":
        return None
    func = node.child_by_field_name("function")
    if func is None or func.type != "member_expression":
        return None
    obj = func.child_by_field_name("object")
    prop = func.child_by_field_name("property")
    if obj is None or prop is None or obj.type != "identifier" or prop.type != "property_identifier":
        return None
    if obj.text != b"Object" or prop.text != b"values":
        return None
    args_node = node.child_by_field_name("arguments")
    if args_node is None:
        return None
    arg_nodes = [c for c in args_node.children if c.type not in ("(", ")", ",")]
    return arg_nodes[0] if len(arg_nodes) == 1 and arg_nodes[0].type == "identifier" else None


def _module_exported_consts(module_root, module_src: bytes) -> dict[str, tuple["Node", bytes]]:
    """Top-level `export const NAME = <expr>` declarations only (not every
    `const` anywhere in the file, unlike `_collect_const_objects`) — this is
    what a `import * as X from "./module"` namespace object actually exposes,
    in declaration order to match `Object.values()`'s real iteration order."""
    out: dict[str, tuple["Node", bytes]] = {}
    for n in module_root.children:
        decl = n
        if n.type == "export_statement":
            decl = next((c for c in n.children if c.type in ("lexical_declaration", "variable_declaration")), None)
        if decl is None or decl.type not in ("lexical_declaration", "variable_declaration"):
            continue
        for child in decl.children:
            if child.type != "variable_declarator":
                continue
            name_node = child.child_by_field_name("name")
            value_node = child.child_by_field_name("value")
            if name_node is not None and name_node.type == "identifier" and value_node is not None:
                out[_text(name_node, module_src)] = (value_node, module_src)
    return out


# id(src buffer) -> that file's own consts, set per `find_ts_tools` run.
_FILE_CONSTS: dict[int, dict[str, tuple["Node", bytes]]] = {}


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
        name = node.text.decode("utf-8", errors="ignore")
        # Resolve against the file the identifier is in first: `AirQuality.NAME`
        # leads into airQuality.ts, whose own `const NAME` is the one meant, not
        # the repo-wide first `NAME` (cablate/mcp-google-map: every tool came
        # out as maps_air_quality, bug #90).
        own = _FILE_CONSTS.get(id(src))
        target = own.get(name) if own is not None and name in own else consts.get(name)
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
            if resolved_obj.type == "enum_declaration":
                body = resolved_obj.child_by_field_name("body")
                for member in (body.named_children if body is not None else []):
                    if member.type == "enum_assignment" and _text(member.child_by_field_name("name"), resolved_src) == _text(prop_node, src):
                        return _resolve(member.child_by_field_name("value"), resolved_src, consts, depth + 1)
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
                prop_name = _text(prop, src)
                if prop_name == "filter":
                    return _resolve(obj_node, src, consts, depth + 1)
                if prop_name == "parse":
                    # `ToolSchema.parse({...})` — a common Zod idiom for
                    # validate-and-return: the object passed in is exactly
                    # what's registered (parse returns its argument unchanged
                    # when valid), so resolve straight through to it rather
                    # than treating the call as an opaque dynamic value.
                    args_node = node.child_by_field_name("arguments")
                    if args_node is not None:
                        call_args = [c for c in args_node.children if c.type not in ("(", ")", ",")]
                        if len(call_args) == 1:
                            return _resolve(call_args[0], src, consts, depth + 1)
                if prop_name in ("trim", "trimStart", "trimEnd"):
                    # `` `...long description...`.trim() `` — a real, common
                    # idiom for a multi-line template-literal description
                    # (verified against brave/brave-search-mcp-server, where
                    # every one of its 8 tool descriptions is declared this
                    # way). Whitespace trimming never changes the actual
                    # content being checked for presence/length, so resolve
                    # straight through to the untrimmed receiver rather than
                    # treating the whole call as an unresolvable dynamic value.
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
        description_display_width=description_display_width(description.strip()),
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
    elif finding.description_display_width < 10:
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
    # A description the code passes but that can't be resolved statically
    # (`description: buildFulltextDescription({...})`, found on
    # cyanheads/pubmed-mcp-server) is unknown, not missing: flagging it as
    # "no description" was a false error.
    description_unresolved = False
    if config_or_desc is not None and config_or_desc.type == "object":
        pairs = _object_pairs(config_or_desc, config_src)
        resolved_description = _resolve_str(pairs.get("description"), config_src, consts)
        description = resolved_description or ""
        description_unresolved = resolved_description is None and "description" in pairs
        if schema_arg is None:
            # registerTool uses inputSchema; fastmcp's addTool uses parameters;
            # the defineTool/definePageTool wrapper style uses schema.
            schema_key = pairs.get("inputSchema") or pairs.get("parameters") or pairs.get("schema") or pairs.get("paramsSchema")
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
    if description_unresolved:
        finding.has_description = True
        finding.issues = [i for i in finding.issues if i.check != "description"]

    if handler is not None and not has_try:
        finding.issues.append(ToolIssue(
            name, file, line, "error_handling",
            "No try/catch in this handler's own body. The MCP SDK still returns a structured "
            "error either way, but without a handler-level catch the model only sees the "
            "generic exception text rather than specific, actionable guidance.",
            "warning",
        ))

    return finding


def _extends_name(class_node, src: bytes) -> str | None:
    """`class A extends B<T>` -> 'B' (also `extends ns.B` -> 'B')."""
    for child in class_node.children:
        if child.type != "class_heritage":
            continue
        for clause in child.children:
            if clause.type != "extends_clause":
                continue
            value = clause.child_by_field_name("value")
            if value is None:
                return None
            if value.type == "member_expression":
                prop = value.child_by_field_name("property")
                return _text(prop, src) if prop is not None else None
            return _text(value, src) if value.type == "identifier" else None
    return None


def _class_fields(class_node, src: bytes) -> dict[str, "Node"]:
    """Map a class body's `field = value` declarations (any modifiers) to value nodes."""
    body = class_node.child_by_field_name("body")
    fields: dict[str, "Node"] = {}
    if body is None:
        return fields
    for member in body.children:
        if member.type != "public_field_definition":
            continue
        name_node = member.child_by_field_name("name")
        value_node = member.child_by_field_name("value")
        if name_node is not None and value_node is not None:
            fields[_text(name_node, src)] = value_node
    return fields


def _analyze_mcp_framework_tool(
    name: str, desc_node, schema_node, src: bytes, consts: dict, file: str, line: int
) -> ToolFinding:
    """mcp-framework's `schema` is either `z.object({ f: z.x().describe(...) })`
    or its older per-field form, `{ f: { type: z.x(), description: "..." } }`,
    where a field counts as documented when it carries a `description`."""
    resolved_description = _resolve_str(desc_node, src, consts)
    description = resolved_description or ""

    param_count = documented = 0
    label = "Zod schema properties have no .describe(...)"
    if schema_node is not None:
        schema, schema_src = _resolve(schema_node, src, consts)
        if schema.type == "object":
            label = "schema fields have no description"
            for value in _object_pairs(schema, schema_src).values():
                field, field_src = _resolve(value, schema_src, consts)
                param_count += 1
                if field.type == "object":
                    field_desc = _object_pairs(field, field_src).get("description")
                    # A description that isn't a literal is present, just unresolvable.
                    if field_desc is not None and _resolve_str(field_desc, field_src, consts) != "":
                        documented += 1
                elif _has_describe_call(field, field_src, consts):
                    documented += 1
        else:
            zod_obj = _zod_object_arg(schema)
            if zod_obj is not None:
                props = _object_pairs(zod_obj, schema_src)
                param_count = len(props)
                documented = sum(1 for v in props.values() if _has_describe_call(v, schema_src, consts))

    finding = _finding_with_description_and_param_issues(
        name, file, line, description, param_count, documented, label,
    )
    if resolved_description is None and desc_node is not None:
        finding.has_description = True
        finding.issues = [i for i in finding.issues if i.check != "description"]
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
        if zod_node is None and _callee_name(schema) == "object":
            # A registry entry's own Zod schema (`schema: z.object({...})`),
            # converted to JSON Schema later by the projection that lists it.
            zod_node, zod_src = schema, resolved_schema_src
        zod_obj = _zod_object_arg(zod_node)
        if zod_obj is not None:
            zod_props = _object_pairs(zod_obj, zod_src)
            param_count = len(zod_props)
            documented = sum(1 for v in zod_props.values() if _has_describe_call(v, zod_src, consts))
            param_doc_label = "Zod schema properties have no .describe(...)"

    return _finding_with_description_and_param_issues(
        name, file, line, description, param_count, documented, param_doc_label,
    )


# Test files by name: `foo.test.ts`, `foo.spec.ts`, `foo-test.ts`, `test_foo.ts`,
# `testUtils.ts`, `setupTests.ts`. Matched as a whole name part, not a
# substring: `"test" in stem` skipped every tool in
# cyanheads/pentest-mcp-server (`pentest-encode.tool.ts`), and would skip
# `latest`, `contest` or `attestation` the same way.
# A camelCase suffix (`setupTests`, `fooSpec`) is only a convention, though:
# fr0ster/mcp-abap-adt keeps 17 real tools in `handleCreateUnitTest.ts`-style
# files (ABAP unit tests are the domain), so those files count as tests only
# when they hold test-framework code.
_TEST_STEM = re.compile(r"(?:^|[._\-])(?:tests?|specs?)(?:$|[._\-])|^tests?(?=[A-Z])")
_TEST_STEM_CAMEL = re.compile(r"[a-z](?:Tests?|Specs?)$")
_JS_TEST_CONTENT = re.compile(
    r"^\s*(?:describe|it|test|beforeAll|beforeEach|afterAll|afterEach)(?:\.\w+)?\s*\("
    r"|\bexpect(?:\.\w+)?\s*\("
    r"|from\s+['\"](?:vitest|@jest/globals|node:test|mocha|chai|@testing-library/[\w-]+)['\"]"
    r"|^\s*import\s+['\"]@testing-library/",
    re.M,
)


def _is_test_stem(stem: str) -> bool:
    return bool(_TEST_STEM.search(stem))


def _is_test_file(p: Path) -> bool:
    if _is_test_stem(p.stem):
        return True
    if not _TEST_STEM_CAMEL.search(p.stem):
        return False
    try:
        return bool(_JS_TEST_CONTENT.search(p.read_text(errors="ignore")))
    except OSError:
        return True


_IMPORT_NAMED = re.compile(
    r"""\b(?:import|export)\s+(?:type\s+)?\{([^}]*)\}\s*from\s*['"](\.[^'"]+)['"]"""
)


def _sdk_register_method(node, src: bytes) -> str:
    """'tool'/'registerTool' when `node` is `x.registerTool`, `x.registerTool.bind(x)`
    or either wrapped in `as T` / parentheses; '' otherwise."""
    while node is not None and node.type in ("as_expression", "parenthesized_expression", "satisfies_expression"):
        node = node.children[1] if node.type == "parenthesized_expression" and len(node.children) > 1 else (node.children[0] if node.children else None)
    if node is not None and node.type == "call_expression":
        fn = node.child_by_field_name("function")
        if fn is None or fn.type != "member_expression" or _text(fn.child_by_field_name("property"), src) != "bind":
            return ""
        node = fn.child_by_field_name("object")
    if node is None or node.type != "member_expression":
        return ""
    prop = node.child_by_field_name("property")
    method = _text(prop, src) if prop is not None else ""
    return method if method in ("tool", "registerTool") else ""


# Tools registered from runtime values: `server.registerTool(tool.name, ...)`
# / `.tool(def.name, ...)` in a loop, or a ListToolsRequestSchema handler
# (whose tools a literal-only scan may not see).
_DYNAMIC_REGISTRATION = re.compile(
    r"\.(?:registerTool|tool)\(\s*[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+\s*[,)]"
    r"|setRequestHandler\(\s*(?:[\w$]+\.)?ListToolsRequestSchema\b"
)


def _template_name(node, src: bytes, consts) -> str | None:
    """A tool name, with a template literal's `${X}` parts resolved when X is
    a constant string (AiDex's `${TOOL_PREFIX}init` -> `aidex_init`). One
    that can't be resolved makes the name dynamic: v1.15.2 dropped it and
    reported `GetVersions` for mcp-abap-adt's `Get${row.display}Versions`
    and `glpi_get_` for mcp-glpi's (bug #85)."""
    if node is None or node.type != "template_string":
        return _string_value(node, src)
    parts = []
    for c in node.children:
        if c.type == "string_fragment":
            parts.append(_text(c, src))
        elif c.type == "template_substitution":
            expr = next((x for x in c.named_children), None)
            value = _resolve_str(expr, src, consts) if expr is not None else None
            if value is None:
                return None
            parts.append(value)
    return "".join(parts)


def _export_used_elsewhere(f: Path, name: str, src: bytes, texts: dict) -> bool:
    """`name` is used again in its own file, or another file loads its
    module with a dynamic `import()` and mentions it."""
    if len(re.findall(rf"\b{re.escape(name)}\b", texts.get(f, ""))) > 1:
        return True
    stem = re.escape(f.with_suffix("").name)
    dyn = re.compile(rf"import\(\s*['\"][^'\"]*/{stem}(?:\.[cm]?[jt]sx?)?['\"]")
    return any(other != f and name in t and dyn.search(t) for other, t in texts.items())


def _off_product_path(f: Path, root: Path) -> bool:
    """Benchmark scripts, evals, examples, seed data and Storybook stories
    restate tool lists without serving them (2026-10 census: DollhouseMCP's
    scripts/benchmark-mcp-aql-tokens.ts, help-scout's evals/ prototype,
    director's registry seed and .stories.tsx)."""
    parts = f.relative_to(root).parts
    return ".stories." in f.name or any(
        p in ("scripts", "benchmarks", "benchmark", "evals", "eval", "examples", "example", "seed", "seeds")
        for p in parts[:-1]
    )


def _not_a_tool_definition(node, pairs: dict, src: bytes) -> bool:
    """Same-shaped objects that aren't tools, seen in the 2026-10 census:
    resource definitions (`uri`/`uriTemplate`/`mimeType`, cyanheads/git-mcp-server),
    OpenAPI parameters (`in`), and API parameter specs (`{ name, type: 'Query', schema, description }`,
    tableau-mcp's zodios client) or any entry of a `parameters:` array."""
    if pairs.keys() & {"uri", "uriTemplate", "mimeType", "in"}:
        return True  # `in: 'query'`: an OpenAPI parameter (daiso-mcp's spec builder)
    if "type" in pairs and pairs["type"].type == "string":
        return True
    holder = node.parent
    if holder is not None and holder.type == "array" and holder.parent is not None and holder.parent.type == "pair":
        key = holder.parent.child_by_field_name("key")
        if key is not None and _text(key, src).strip("'\"") in ("parameters", "params", "args", "arguments", "properties", "fields"):
            return True
    return False


def _module_key(path: Path) -> str:
    """A module's identity for import matching: no extension, `/index` dropped."""
    p = path.with_suffix("") if path.suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs") else path
    return str(p.parent if p.name == "index" else p)


def _imported_names(parsed) -> set[tuple[str, str]]:
    """(module_key, exported_name) for every named import/re-export between
    the repo's own (non-test) files; `X as Y` keeps the exported name X."""
    out: set[tuple[str, str]] = set()
    for f, _root, src in parsed:
        for m in _IMPORT_NAMED.finditer(src.decode("utf-8", errors="ignore")):
            target = _module_key((f.parent / m.group(2)).resolve())
            for spec in m.group(1).split(","):
                name = spec.strip().removeprefix("type ").split(" as ")[0].strip()
                if name:
                    out.add((target, name))
    return out


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
    for p in sorted(root.rglob("*")):  # same order on every Python version
        if p.suffix not in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            continue
        rel_parts = p.relative_to(root).parts
        if any(part in skip_dirs or part.startswith(".") for part in rel_parts):
            continue
        # Directory-based exclusion matches the Python analyzer's `_is_auxiliary_file`
        # (any "test"/"tests" path segment) — verified against a real miss:
        # mcp-use/mcp-use's `libraries/typescript/packages/agent/tests/servers/
        # simple_server.ts`, a genuine test fixture ("Minimal stdio MCP server
        # ... for agent integration tests") whose filename stem alone
        # (`simple_server`) and directory (`tests`, not Jest's `__tests__`)
        # both slipped past the old check.
        # Fixture folders too (swarmclaw's `__fixtures__/fake-mcp-stdio-server.mjs`,
        # peta-core's `scripts/compat-smoke/fixtures/`): test servers, not the product.
        if _is_test_file(p) or any(part in ("test", "tests", "__tests__", "fixtures", "__fixtures__") for part in rel_parts):
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
    _FILE_CONSTS.clear()
    for _f, file_root, file_src in parsed:
        own = _collect_const_objects(file_root, file_src)
        _FILE_CONSTS[id(file_src)] = own
        for const_name, entry in own.items():
            global_consts.setdefault(const_name, entry)

    # Maps each file's src buffer (by identity — buffers are never copied, only
    # passed around by reference through resolution) back to its relative path,
    # so a tool object resolved from a static array can be reported at its own
    # definition site rather than the (possibly different-file) call site.
    src_to_rel: dict[int, str] = {id(s): str(f.relative_to(root)) for f, _, s in parsed}
    seen_list_tools: set[tuple[str, str, int]] = set()

    # Resolves a relative `import * as X from "./spec"` module specifier to the
    # parsed file it refers to, tolerating the common TS-emits-.js-imports-for-
    # .ts-source mismatch by comparing paths with their suffix stripped.
    by_module_path: dict[Path, tuple[Path, "Node", bytes]] = {
        f.with_suffix(""): (f, file_root, file_src) for f, file_root, file_src in parsed
    }
    module_exports_cache: dict[Path, dict[str, tuple["Node", bytes]]] = {}

    def _resolve_namespace_module(current_file: Path, spec: str):
        if not spec.startswith("."):
            return None  # only same-repo relative imports are traceable
        target = (current_file.parent / spec).resolve().with_suffix("")
        return by_module_path.get(target)

    def _emit_static_list_tools(tools_array_raw, f, rel, src, consts, namespace_imports):
        """Report every tool in a static `tools` list a list-tools handler returns."""
        if tools_array_raw is None:
            return

        # `Object.values(tools)` where `tools` is a namespace import
        # (`import * as tools from "./tools.js"`) — the module it
        # points at exports one `const` object per tool rather than a
        # single array, so its elements come from that module's own
        # top-level exports instead of `_collect_tool_array_elements`.
        ns_arg = _object_values_arg(tools_array_raw)
        tool_elements: list[tuple["Node", bytes]] = []
        if ns_arg is not None:
            spec = namespace_imports.get(_text(ns_arg, src))
            module_entry = _resolve_namespace_module(f, spec) if spec else None
            if module_entry is not None:
                mod_path, mod_root, mod_src = module_entry
                mod_exports = module_exports_cache.get(mod_path)
                if mod_exports is None:
                    mod_exports = _module_exported_consts(mod_root, mod_src)
                    module_exports_cache[mod_path] = mod_exports
                for value_node, value_src in mod_exports.values():
                    resolved_val, resolved_val_src = _resolve(value_node, value_src, consts)
                    if resolved_val.type == "object":
                        tool_elements.append((resolved_val, resolved_val_src))
        else:
            # `tools: registry.map(t => ({ name: t.name, ... }))`: each
            # registry entry is one tool, so read the entries themselves.
            mapped = _map_projection_receiver(tools_array_raw, src)
            tools_array, tools_array_src = _resolve(
                mapped if mapped is not None else tools_array_raw, src, consts
            )
            tool_elements = _collect_tool_array_elements(tools_array, tools_array_src, consts)

        for tool_obj, tool_src in tool_elements:
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
                    name_val, pairs.get("description"),
                    pairs.get("inputSchema") if "inputSchema" in pairs else pairs.get("schema"),
                    tool_src,
                    consts, tool_src, tool_file, tool_line,
                )
            )

    for f, file_root, src in parsed:
        rel = str(f.relative_to(root))
        local_consts = _collect_const_objects(file_root, src)
        consts = {**global_consts, **local_consts}
        local_funcs = _collect_function_declarations(file_root, src)
        namespace_imports = _collect_namespace_imports(file_root, src)

        for node in _walk(file_root):
            if node.type == "variable_declarator":
                # A bare (no wrapping call) typed const tool object, e.g.
                # `const navigateTool: Tool<typeof NavigateInputSchema> = {
                #   capability: "core", schema: { name, description, inputSchema },
                #   handle: handleNavigate,
                # }` — verified against browserbase/mcp-server-browserbase, whose
                # own MCP-SDK registration site (`server.tool(tool.schema.name,
                # ...)`) is a runtime `.forEach()` over a collected array with only
                # property-accessed args, genuinely unresolvable there. The
                # `schema`+`handle` sibling-field combination is distinctive
                # enough to trust without needing a project-specific type name
                # (the `Tool<...>` annotation varies per project) — requiring a
                # resolvable literal `schema.name` keeps a same-named-but-
                # unrelated object from being mistaken for one.
                value_node = node.child_by_field_name("value")
                if value_node is None or value_node.type != "object":
                    continue
                outer_pairs = _object_pairs(value_node, src)
                schema_field = outer_pairs.get("schema")
                handle_field = outer_pairs.get("handle")
                if schema_field is None or handle_field is None:
                    continue
                schema_obj, schema_src = _resolve(schema_field, src, consts)
                if schema_obj.type != "object":
                    continue
                schema_pairs = _object_pairs(schema_obj, schema_src)
                if "name" not in schema_pairs:
                    continue  # not this shape — a same-named unrelated object
                name_val = _resolve_str(schema_pairs.get("name"), schema_src, consts)
                if name_val is None:
                    continue  # dynamic tool name — can't attribute a finding to it
                handler = handle_field
                if handler.type == "identifier":
                    handler = local_funcs.get(_text(handler, src))
                elif handler.type not in ("arrow_function", "function_expression"):
                    handler = None
                finding = _analyze_ts_tool(
                    name_val, schema_obj, schema_src, None, schema_src, handler, consts,
                    rel, node.start_point[0] + 1,
                )
                findings.append(finding)
                continue
            # Hand-rolled JSON-RPC dispatch with no SDK at all:
            # `switch (method) { case 'tools/list': return { result: { tools: TOOLS } } }`
            # or `if (method === 'tools/list') { ... }` (kitfunso/hippo-memory, 13
            # tools). Same static-list resolution as the SDK handler below; a
            # proxy that builds its list at runtime doesn't resolve and is skipped.
            raw_list_body = _raw_tools_list_branch(node, src)
            if raw_list_body is not None:
                _emit_static_list_tools(
                    _find_tools_array(raw_list_body, src, local_funcs),
                    f, rel, src, consts, namespace_imports,
                )
                continue
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
                _emit_static_list_tools(
                    _find_tools_array(arg_nodes[1], src, local_funcs),
                    f, rel, src, consts, namespace_imports,
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

            if method == "tool" and len(arg_nodes) == 2:
                # `@cyanheads/mcp-ts-core`'s definition style (30+ public
                # servers): `tool('name', { description, input: z.object(...),
                # async handler(input, ctx) {...} })`. Verified against a real
                # miss, cyanheads/clinicaltrialsgov-mcp-server (0 of 8 found).
                # Requiring both `description` and `input` keeps the SDK's
                # two-arg `server.tool(name, callback)` from matching.
                # error_handling isn't checked: the framework's documented
                # rule is "logic throws, framework catches".
                config, config_src = _resolve(arg_nodes[1], src, consts)
                if config.type != "object":
                    continue
                pairs = _object_pairs(config, config_src)
                if "description" not in pairs or "input" not in pairs:
                    continue
                name_val = _resolve_str(arg_nodes[0], src, consts)
                if name_val is None:
                    continue  # dynamic tool name — can't attribute a finding to it
                schema_arg, schema_src = _resolve(pairs["input"], config_src, consts)
                findings.append(
                    _analyze_ts_tool(
                        name_val, config, config_src, schema_arg, schema_src, None, consts,
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

    # `mcp-framework` (QuantGeekDev/mcp-framework, ~60 public servers): each
    # tool is a class, `class FooTool extends MCPTool<In> { name = "foo";
    # description = "..."; schema = {...}; async execute(input) {...} }`,
    # auto-discovered from a tools/ directory — there's no registration call
    # anywhere to resolve. Found in the 2026-09-30 framework sweep, where
    # every one of these servers scanned as 0 tools. Tools may extend a
    # repo-local base class that itself extends MCPTool, so the chain is
    # followed repo-wide (by class name, the same simplification as the
    # const registry). error_handling isn't checked: the framework's
    # `toolCall` wraps every `execute()` in its own try/catch.
    class_bases: dict[str, str] = {}
    tool_classes: list[tuple[Path, "Node", "Node", bytes]] = []
    for f, file_root, src in parsed:
        for node in _walk(file_root):
            if node.type not in ("class_declaration", "abstract_class_declaration"):
                continue
            name_node = node.child_by_field_name("name")
            base = _extends_name(node, src)
            if name_node is None or base is None:
                continue
            class_bases.setdefault(_text(name_node, src), base)
            if node.type == "class_declaration":
                tool_classes.append((f, file_root, node, src))

    def _extends_mcp_tool(base: str) -> bool:
        seen: set[str] = set()
        while base not in seen and len(seen) < 6:
            if base == MCP_FRAMEWORK_BASE:
                return True
            seen.add(base)
            base = class_bases.get(base, "")
        return False

    known_names = {fd.name for fd in findings}
    for f, file_root, class_node, src in tool_classes:
        if not _extends_mcp_tool(_extends_name(class_node, src)):
            continue
        rel = str(f.relative_to(root))
        consts = {**global_consts, **_collect_const_objects(file_root, src)}
        fields = _class_fields(class_node, src)
        name_val = _resolve_str(fields.get("name"), src, consts)
        if name_val is None or name_val in known_names:
            continue  # inherited/dynamic name, or already reported by another style
        known_names.add(name_val)
        findings.append(
            _analyze_mcp_framework_tool(
                name_val, fields.get("description"), fields.get("schema"), src, consts,
                rel, class_node.start_point[0] + 1,
            )
        )

    # Plain tool-definition objects registered by a runtime loop, e.g.
    # `export function assignmentTools(canvas): ToolDefinition[] { return [
    #   { name: 'list_assignments', description, inputSchema: {...}, handler }, ...
    # ] }` with `for (const tool of tools) server.registerTool(tool.name, ...)`.
    # Verified against a real miss: bruchris/canvas-lms-mcp (scan request #3),
    # 165 tools, 0 found — the only registration call has property-accessed
    # args, unresolvable there. The name+description+inputSchema+handler
    # (or `run`/`execute`, as in hustcc/mcp-echarts) combination on one object literal is distinctive enough to trust, and
    # the object must sit directly in an array or a const so a registerTool
    # config object (no `name` key) or a same-shaped call argument can't match.
    # Runs last and skips names already found, so a repo that also registers
    # the same tool through a resolvable style isn't double-counted.
    # error_handling isn't checked: the loop's shared wrapper (canvas's
    # buildHandler) is where the catch lives, not each handler.
    # An exported const needs no handler key when other source code imports
    # it: fr0ster/mcp-abap-adt has 300+
    # `export const TOOL_DEFINITION = { name, description, inputSchema } as const`,
    # one per handler file, imported under aliases into group files and
    # registered in a loop (0 found before). Exported alone isn't enough: the
    # same shape is a docs/test fixture, and abap keeps 5 definitions nothing
    # imports. Test files are already out of `parsed`, so a fixture used only
    # by tests stays uncounted. `as const` / `satisfies` wrappers are looked through.
    # When the repo registers tools only from runtime values (a loop's
    # `server.registerTool(tool.name, ...)`, or a ListToolsRequestSchema
    # handler that returns a variable or a `.map` over one), a definition
    # object needs no handler key and may use `schema`/`parameters`/
    # `paramsSchema` for its schema, or sit in `new SomeTool({...})`
    # (2026-10 census: postman, tableau, reddit-mcp-buddy, memory-bank-mcp
    # and others, 0 found before). Without that evidence the old, stricter
    # shape still applies, so a docs or OpenAI-function fixture can't match.
    texts = {f: src.decode("utf-8", errors="ignore") for f, _, src in parsed}
    dynamic_registration = any(_DYNAMIC_REGISTRATION.search(t) for t in texts.values())
    imported = _imported_names(parsed)
    known_names = {fd.name for fd in findings}
    for f, file_root, src in parsed:
        rel = str(f.relative_to(root))
        consts = {**global_consts, **_collect_const_objects(file_root, src)}
        for node in _walk(file_root):
            if node.type != "object" or node.parent is None:
                continue
            holder = node.parent
            while holder is not None and holder.type in ("as_expression", "satisfies_expression", "parenthesized_expression"):
                holder = holder.parent
            in_new = (
                dynamic_registration and holder is not None and holder.type == "arguments"
                and holder.parent is not None and holder.parent.type == "new_expression"
            )
            # `router.setTool({ schema: { name, description, inputSchema }, handler })`
            # (alioshr/memory-bank-mcp, 0 of 5 found before).
            in_schema_pair = (
                dynamic_registration and holder is not None and holder.type == "pair"
                and _text(holder.child_by_field_name("key"), src).strip("'\"") in ("schema", "tool", "definition")
                and holder.parent is not None and holder.parent.parent is not None
                and holder.parent.parent.type == "arguments"
            )
            if holder is None or (holder.type not in ("array", "variable_declarator") and not in_new and not in_schema_pair):
                continue
            pairs = _object_pairs(node, src)
            schema_keys = ("inputSchema", "schema", "parameters", "paramsSchema") if dynamic_registration else ("inputSchema",)
            schema_key = next((k for k in schema_keys if k in pairs), None)
            if not {"name", "description"} <= pairs.keys() or schema_key is None:
                continue
            if dynamic_registration and _not_a_tool_definition(node, pairs, src):
                continue
            exported = (
                holder.type == "variable_declarator"
                and holder.parent is not None
                and holder.parent.parent is not None
                and holder.parent.parent.type == "export_statement"
            )
            const_name = _text(holder.child_by_field_name("name"), src) if exported else ""
            used_export = exported and (_module_key(f), const_name) in imported
            strict = used_export or bool(pairs.keys() & {"handler", "run", "execute"})
            # An exported definition stays out unless something uses it: its
            # own file (`getTools()` returning it, mcp-atom-of-thoughts) or a
            # dynamic `import()` of its module (mcp-klever-vm). Bug #85:
            # v1.15.2 counted mcp-abap-adt's 5 unit-test TOOL_DEFINITIONs,
            # which nothing uses.
            if not strict and (
                not dynamic_registration or _off_product_path(f, root)
                or (exported and not _export_used_elsewhere(f, const_name, src, texts))
            ):
                continue  # hustcc/mcp-echarts names its handler `run`
            name_node, name_src = _resolve(pairs["name"], src, consts)
            name_val = _template_name(name_node, name_src, consts)
            if name_val is None or name_val in known_names:
                continue  # dynamic name, or already reported by another style
            known_names.add(name_val)
            # Raw JSON Schema (`{ type: 'object', properties }`, as abap uses)
            # is read as JSON Schema; reading it as a Zod shape counted `type`
            # and `properties` as two undescribed params.
            schema, schema_src = _resolve(pairs[schema_key], src, consts)
            if schema is not None and schema.type == "object" and "properties" in _object_pairs(schema, schema_src):
                findings.append(
                    _analyze_json_schema_tool(
                        name_val, pairs["description"], pairs[schema_key], src, consts, src,
                        rel, node.start_point[0] + 1,
                    )
                )
                continue
            findings.append(
                _analyze_ts_tool(
                    name_val, node, src, None, src, None, consts,
                    rel, node.start_point[0] + 1,
                )
            )

    # A repo-local wrapper around the SDK call, e.g. strausmann/mcp-dockhand's
    # `function registerTool(server, name, schema, callback) {
    #    (server as any).tool(name, describeTool(name), schema, async (args) => {
    #      try { return await callback(args) } catch ... }) }`,
    # called 350+ times as `registerTool(server, 'list_x', {...}, async () => ...)`.
    # The wrapper's own `.tool(name, ...)` has a parameter as its name, so it
    # was skipped and every tool was missed. A function counts as a wrapper
    # when its body passes one of its own parameters as the name to `.tool(...)`
    # or `.registerTool(...)`; each call to it with a literal name is a tool,
    # with description, schema and handler read from the arguments the wrapper
    # forwards. A description the wrapper builds itself is unknown, not
    # missing; a wrapper with its own try/catch handles errors for every tool.
    # Also (cmer81/open-meteo-mcp, 0 of 17): the wrapper can be a class method
    # called as `this.registerReadOnlyTool(server, WEATHER_FORECAST_TOOL, schema,
    # handler)`, the SDK call inside can go through a saved alias
    # (`const reg = server.registerTool.bind(server)`), and the name and
    # description can be fields of a parameter (`meta.name`, `meta.description`),
    # read at each call site from the const object passed in. Method wrappers
    # only match `this.method(...)` calls, so an unrelated `x.method(...)` with
    # the same name can't.
    wrappers: dict[str, dict] = {}
    method_wrappers: dict[str, dict] = {}
    for f, file_root, src in parsed:
        for node in _walk(file_root):
            is_method = False
            if node.type == "function_declaration":
                fname_node, fn = node.child_by_field_name("name"), node
            elif node.type == "variable_declarator":
                value = node.child_by_field_name("value")
                if value is None or value.type not in ("arrow_function", "function_expression", "function"):
                    continue
                fname_node, fn = node.child_by_field_name("name"), value
            elif node.type == "method_definition":
                fname_node, fn, is_method = node.child_by_field_name("name"), node, True
            else:
                continue
            params_node, body = fn.child_by_field_name("parameters"), fn.child_by_field_name("body")
            if fname_node is None or params_node is None or body is None:
                continue
            params = []
            for p in params_node.children:
                if p.type in ("required_parameter", "optional_parameter"):
                    pat = p.child_by_field_name("pattern")
                    params.append(_text(pat, src) if pat is not None else None)
                elif p.type == "identifier":
                    params.append(_text(p, src))
            aliases = {}
            for d in _walk(body):
                if d.type == "variable_declarator":
                    alias_name, alias_method = d.child_by_field_name("name"), _sdk_register_method(d.child_by_field_name("value"), src)
                    if alias_name is not None and alias_name.type == "identifier" and alias_method:
                        aliases[_text(alias_name, src)] = alias_method
            for call in _walk(body):
                if call.type != "call_expression":
                    continue
                callee = call.child_by_field_name("function")
                if callee is None:
                    continue
                if callee.type == "member_expression":
                    prop = callee.child_by_field_name("property")
                    method = _text(prop, src) if prop is not None else ""
                elif callee.type == "identifier":
                    method = aliases.get(_text(callee, src), "")
                else:
                    continue
                if method not in ("tool", "registerTool"):
                    continue
                cargs_node = call.child_by_field_name("arguments")
                cargs = [c for c in cargs_node.children if c.type not in ("(", ")", ",")] if cargs_node else []

                def ref(n):
                    """(param index, field or None) for `param` or `param.field`."""
                    if n is None:
                        return None
                    if n.type == "identifier" and _text(n, src) in params:
                        return params.index(_text(n, src)), None
                    if n.type == "member_expression":
                        obj, fld = n.child_by_field_name("object"), n.child_by_field_name("property")
                        if obj is not None and fld is not None and obj.type == "identifier" and _text(obj, src) in params:
                            return params.index(_text(obj, src)), _text(fld, src)
                    return None

                if len(cargs) < 3 or ref(cargs[0]) is None:
                    continue
                handler_ref = ref(cargs[-1])
                spec = {"name": ref(cargs[0]), "desc": None, "schema": None,
                        "handler": handler_ref if handler_ref and handler_ref[1] is None else None,
                        "has_try": _find_try(body)}
                if method == "tool" and len(cargs) >= 4:
                    spec["desc"], spec["schema"] = ref(cargs[1]), ref(cargs[2])
                elif method == "registerTool" and cargs[1].type == "object":
                    pairs = _object_pairs(cargs[1], src)
                    spec["desc"], spec["schema"] = ref(pairs.get("description")), ref(pairs.get("inputSchema"))
                (method_wrappers if is_method else wrappers)[_text(fname_node, src)] = spec
                break
    known_names = {fd.name for fd in findings}
    for f, file_root, src in parsed if wrappers or method_wrappers else ():
        rel = str(f.relative_to(root))
        consts = {**global_consts, **_collect_const_objects(file_root, src)}
        for node in _walk(file_root):
            if node.type != "call_expression":
                continue
            callee = node.child_by_field_name("function")
            if callee is None:
                continue
            if callee.type == "identifier":
                spec = wrappers.get(_text(callee, src))
            elif callee.type == "member_expression":
                obj, prop = callee.child_by_field_name("object"), callee.child_by_field_name("property")
                if obj is None or prop is None or obj.type != "this":
                    continue
                spec = method_wrappers.get(_text(prop, src))
            else:
                continue
            if spec is None:
                continue
            args_node = node.child_by_field_name("arguments")
            args = [c for c in args_node.children if c.type not in ("(", ")", ",")] if args_node else []

            def arg(r):
                """The call-site node (and its src) a wrapper parameter ref points to."""
                if r is None or r[0] >= len(args):
                    return None, src
                n = args[r[0]]
                if r[1] is None:
                    return n, src
                obj_node, obj_src = _resolve(n, src, consts)
                if obj_node is None or obj_node.type != "object":
                    return None, src
                return _object_pairs(obj_node, obj_src).get(r[1]), obj_src

            name_node, name_src = arg(spec["name"])
            name_val = _resolve_str(name_node, name_src, consts) if name_node is not None else None
            if name_val is None or name_val in known_names:
                continue
            known_names.add(name_val)
            desc_node, desc_src = arg(spec["desc"])
            schema_node, _ = arg(spec["schema"])
            schema_arg, schema_src = _resolve(schema_node, src, consts) if schema_node is not None else (None, src)
            handler, _ = arg(spec["handler"])
            if spec["has_try"] or (handler is not None and handler.type not in ("arrow_function", "function_expression")):
                handler = None
            finding = _analyze_ts_tool(
                name_val, desc_node, desc_src, schema_arg, schema_src, handler, consts,
                rel, node.start_point[0] + 1,
            )
            if desc_node is None or _resolve_str(desc_node, desc_src, consts) is None:
                finding.has_description = True  # built at runtime: unknown, not missing
                finding.issues = [i for i in finding.issues if i.check != "description"]
            if spec["has_try"]:
                finding.has_try_except = True
            findings.append(finding)

    findings.extend(_module_per_tool_findings(parsed, root, global_consts, {fd.name for fd in findings}))
    return findings, unparseable


def _member_path(node, src: bytes) -> tuple[str, list[str]] | None:
    """`tool.metadata.name` -> ("tool", ["metadata", "name"]); `.shape` and a
    wrapping one-argument call (`z.object(tool.schema)`,
    `toolInputSchema(tool.inputSchema)`) are looked through."""
    while node is not None and node.type == "call_expression":
        args = node.child_by_field_name("arguments")
        inner = [c for c in args.named_children] if args is not None else []
        if len(inner) != 1:
            return None
        node = inner[0]
    props: list[str] = []
    while node is not None and node.type == "member_expression":
        prop = node.child_by_field_name("property")
        if prop is None or prop.type != "property_identifier":
            return None
        props.append(_text(prop, src))
        node = node.child_by_field_name("object")
    if node is None or node.type != "identifier" or not props:
        return None
    props.reverse()
    if props[-1] == "shape" and len(props) > 1:
        props.pop()
    return _text(node, src), props


def _is_runtime_binding(site, name: str, src: bytes) -> bool:
    """`name` is a loop variable (`for (const tool of tools)`) or a parameter
    of a function enclosing `site` (`tools.map((tool) => ...)`)."""
    node = site
    while node is not None:
        if node.type in ("for_in_statement",):
            left = node.child_by_field_name("left")
            if left is not None and name in re.findall(r"[A-Za-z_$][\w$]*", _text(left, src)):
                return True
        if node.type in ("arrow_function", "function_expression", "function_declaration", "method_definition"):
            params = node.child_by_field_name("parameters") or node.child_by_field_name("parameter")
            if params is not None and name in re.findall(r"[A-Za-z_$][\w$]*", _text(params, src).split(":")[0] if params.type == "identifier" else _text(params, src)):
                return True
        node = node.parent
    return False


def _module_per_tool_signatures(parsed) -> set[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...] | None]]:
    """(name path, description path, schema path) read off registrations that
    take every field from one runtime tool object: `server.registerTool(
    tool.method, { description: tool.description, inputSchema:
    tool.parameters.shape }, ...)` (postmanlabs/postman-mcp-server), or a
    ListTools handler's `tools.map((tool) => ({ name: tool.metadata.name,
    description: tool.metadata.description, inputSchema: ... }))`
    (vercel/next-devtools-mcp). The paths say which exports a tool module has."""
    sigs = set()
    for _f, file_root, src in parsed:
        for node in _walk(file_root):
            name_node = config = None
            if node.type == "call_expression":
                fn = node.child_by_field_name("function")
                prop = fn.child_by_field_name("property") if fn is not None and fn.type == "member_expression" else None
                if prop is None or _text(prop, src) not in ("registerTool", "tool"):
                    continue
                args = [c for c in node.child_by_field_name("arguments").named_children]
                if len(args) < 2:
                    continue
                name_node = args[0]
                config = args[1] if args[1].type == "object" else None
                desc_node = _object_pairs(config, src).get("description") if config is not None else (
                    args[1] if args[1].type == "member_expression" else None)
            elif node.type == "object":
                pairs = _object_pairs(node, src)
                if "name" not in pairs or "description" not in pairs:
                    continue
                name_node, config, desc_node = pairs["name"], node, pairs["description"]
            else:
                continue
            name_path = _member_path(name_node, src)
            desc_path = _member_path(desc_node, src) if desc_node is not None else None
            if name_path is None or desc_path is None or name_path[0] != desc_path[0]:
                continue
            if not _is_runtime_binding(node, name_path[0], src):
                continue  # `constants.LIST_TERMINALS_NAME` (Adyen/adyen-mcp): a module, not a tool object
            schema_path = None
            if config is not None:
                pairs = _object_pairs(config, src)
                schema_val = next((pairs[k] for k in ("inputSchema", "parameters", "schema") if k in pairs), None)
                sp = _member_path(schema_val, src) if schema_val is not None else None
                if sp is not None and sp[0] == name_path[0]:
                    schema_path = tuple(sp[1])
            sigs.add((tuple(name_path[1]), tuple(desc_path[1]), schema_path))
    return sigs


def _module_per_tool_findings(parsed, root: Path, global_consts: dict, known_names: set) -> list[ToolFinding]:
    """One tool per module that exports what a runtime registration reads
    (see `_module_per_tool_signatures`): postman's 216 `src/tools/*.ts`, each
    `export const method = '...'`, `description`, `parameters = z.object(...)`,
    loaded with readdir + import() (0 found before). A module counts only when
    every path resolves to its own top-level exports and the name is a literal."""
    sigs = _module_per_tool_signatures(parsed)
    if not sigs:
        return []
    # A module another module imports by name is a helper or a sub-tool
    # (postman's getCollection/getCollectionMap.ts, dispatched by
    # getCollection), not a module the loader registers on its own; a
    # namespace import (`import * as browserEval`, next-devtools) still counts.
    files_by_key = {str(f.with_suffix("")): f for f, _, _ in parsed}

    def target_key(importer: Path, spec: str) -> str:
        # Exact path, not `_module_key`: `getCollection.ts` and
        # `getCollection/index.ts` must stay two modules here.
        p = (importer.parent / spec).resolve()
        p = p.with_suffix("") if p.suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs") else p
        return str(p) if str(p) in files_by_key else str(p / "index")

    named_imported = {
        target_key(f, m.group(2))
        for f, _, src in parsed for m in _IMPORT_NAMED.finditer(src.decode("utf-8", errors="ignore"))
    }
    local_exports = {}
    for f, file_root, src in parsed:
        exports = {}
        for node in file_root.named_children:
            if node.type != "export_statement":
                continue
            # Only `export const x = ...` itself: not locals inside an
            # exported function (1mcp-app/agent's `const name = arg.name || 'argument'`).
            for lexical in node.named_children:
                if lexical.type != "lexical_declaration":
                    continue
                for decl in lexical.named_children:
                    if decl.type == "variable_declarator" and decl.child_by_field_name("value") is not None:
                        exports[_text(decl.child_by_field_name("name"), src)] = (decl.child_by_field_name("value"), file_root, src)
        local_exports[str(f.with_suffix(""))] = exports
    out = []
    for f, file_root, src in parsed:
        if _off_product_path(f, root) or str(f.with_suffix("")) in named_imported:
            continue
        exports = {k: v[0] for k, v in local_exports.get(str(f.with_suffix("")), {}).items()}
        if not exports:
            # A file that only re-exports one module's tool fields
            # (`export { method, description, parameters } from './getCollection/index.js'`,
            # postman's top-level getCollection.ts) stands for that module.
            targets = [
                local_exports.get(target_key(f, m.group(2)), {})
                for m in _IMPORT_NAMED.finditer(src.decode("utf-8", errors="ignore"))
                if m.group(0).startswith("export")
            ]
            if len(targets) == 1 and targets[0]:
                exports = {k: v[0] for k, v in targets[0].items()}
                _, file_root, src = next(iter(targets[0].values()))
        if not exports:
            continue
        consts = {**global_consts, **_collect_const_objects(file_root, src)}

        def get(path):
            node = exports.get(path[0])
            for key in path[1:]:
                node, _ = _resolve(node, src, consts)
                if node is None or node.type != "object":
                    return None
                node = _object_pairs(node, src).get(key)
            return node

        for name_path, desc_path, schema_path in sigs:
            name_node, desc_node = get(name_path), get(desc_path)
            if name_node is None or desc_node is None:
                continue
            name_node, name_src = _resolve(name_node, src, consts)
            name_val = _template_name(name_node, name_src, consts)
            if not name_val or name_val in known_names:
                continue
            known_names.add(name_val)
            line = name_node.start_point[0] + 1
            schema_node = get(schema_path) if schema_path else None
            schema, schema_src = _resolve(schema_node, src, consts) if schema_node is not None else (None, src)
            if schema is not None and schema.type == "object" and "properties" in _object_pairs(schema, schema_src):
                out.append(_analyze_json_schema_tool(name_val, desc_node, schema_node, src, consts, src, str(f.relative_to(root)), line))
            else:
                desc_resolved, desc_src = _resolve(desc_node, src, consts)
                out.append(_analyze_ts_tool(name_val, desc_resolved, desc_src, schema, schema_src, None, consts,
                                            str(f.relative_to(root)), line))
            break
    return out
