"""Static analysis of Go MCP server implementations.

Supports the official `modelcontextprotocol/go-sdk`'s generic registration
style, verified directly against that SDK's own examples and against a real,
15k+-star production server (`xpzouying/xiaohongshu-mcp`):

    mcp.AddTool(server, &mcp.Tool{Name: "...", Description: "..."}, handler)

`handler` may be an inline `func(ctx, req, args ArgsType) (...)` literal, an
identifier referencing a top-level named function with the same shape, or
either of those wrapped in one or more helper calls (e.g. panic-recovery or
logging middleware — `withPanicRecovery("name", func(...) {...})`, which
every single tool in `xiaohongshu-mcp` turned out to use; verified this isn't
an edge case worth skipping before building support for it). Parameters are
documented one of two ways, both real and both checked:

  - explicit: `InputSchema: &jsonschema.Schema{Properties: map[string]*jsonschema.Schema{
        "x": {Type: "string", Description: "..."},
    }}` on the `mcp.Tool` literal itself
  - inferred: the handler's third parameter is a named struct type, and the
    SDK builds the schema from its `json`/`jsonschema` struct tags at
    registration time (`Name string `json:"name" jsonschema:"the person to
    greet"``) — confirmed directly in the SDK's own `examples/server/hello`.

Also supports `mark3labs/mcp-go`'s older fluent-builder style, verified
directly against that package's own source and against a second real,
1.8k+-star production server (`korotovsky/slack-mcp-server`):

    s.AddTool(mcp.NewTool(name,
        mcp.WithDescription("..."),
        mcp.WithString("param", mcp.Required(), mcp.Description("...")),
    ), handler)

Every parameter is declared inline in the builder chain itself (one of
`WithString`/`WithNumber`/`WithInteger`/`WithBoolean`/`WithObject`/
`WithArray`/`WithAny`, confirmed against the package's exported function
list), each optionally carrying `mcp.Description(...)` — there's no
struct-tag inference to fall back on here, unlike the official SDK, so the
handler itself is never even inspected for this style. The tool's own name
is commonly a package-level `const` reference rather than an inline string
literal (`ToolConversationsHistory = "conversations_history"`), which is
resolved the same name-based, ambiguity-safe way as the struct/function
registries above.

Also recognizes a project-local factory function wrapping registration —
verified against `github/github-mcp-server` (the official, 32k+-star GitHub
MCP server), which builds every tool through its own generic helper rather
than calling `AddTool` at each definition site:

    st := NewTool(toolsetMeta, mcp.Tool{Name: "...", Description: t("KEY", "...")},
        scopeAccess, handler)

Any call passing an `mcp.Tool{...}`/`&mcp.Tool{...}` literal with a *literal*
`Name` field is recognized regardless of the outer function's name — the
`AddTool` name itself was never load-bearing, just the common case. A `Name`
built from a variable (as in that repo's handful of shared single-field-update
factories, e.g. `prUpdateTool`) is left unresolved rather than guessed at,
same as the existing dynamic-name skip elsewhere in this module. The
`Description` field also resolves the common i18n convention
`t(key, defaultValue string) string` (confirmed against that repo's own
`TranslationHelperFunc` signature) to its literal fallback argument, since
that's the real text a model sees whenever the key isn't translated.

Also recognizes mark3labs/mcp-go's own exported `server.ServerTool{Tool: ...,
Handler: ...}` struct (server/server.go) used as a tool-factory return value
rather than an `AddTool` call argument — verified against
`hashicorp/terraform-mcp-server` (official HashiCorp), where every tool is
built as `server.ServerTool{Tool: mcp.NewTool(...), Handler: ...}` inside a
small per-tool factory function, collected into a slice, and registered
elsewhere via `AddTool(tool.Tool, tool.Handler)` in a loop — never a literal
`AddTool(mcp.NewTool(...), handler)` call site anywhere in the repo. The
composite literal itself is treated as the definition site.

Also recognizes a bare `mcp.Tool` (official SDK, not mark3labs) declared as
an element of a typed slice literal — `[]*mcp.Tool{ {Name: "...",
Description: "..."}, ... }`, Go's shorthand for eliding the repeated
`&mcp.Tool{...}` on every element — verified against `clidey/whodb`, where
every tool group is defined this way in a `*ToolDefinitions() []*mcp.Tool`
function and registered elsewhere by a switch on `tool.Name`, never through a
literal `AddTool` call. As with `server.ServerTool`, the slice element itself
is the definition site. Connecting a definition back to its handler (to check
parameter docs) would mean resolving that switch — genuinely more than a
"don't guess" reach for this pass, so `param_count` is left at 0 here (no
param-docs finding is raised) rather than guessed at; only name/description
are checked.

Error handling is intentionally not checked for Go tools at all (v1): unlike
Python's try/except or TS's try/catch, Go has no exception mechanism — a
handler communicates failure via its `error` return value, which the SDK
already turns into a structured tool error regardless of what the handler
does with it. What a *useful* Go-specific error-handling check should even
look for hasn't been researched carefully enough yet to check for it without
risking a check that's either wrong or meaningless; better to ship nothing
here than a guess.

Requires the optional `tree_sitter` / `tree_sitter_go` packages — callers
should treat their absence as "skip Go analysis", not an error.
"""

from __future__ import annotations

from pathlib import Path

from .analyzer import ToolFinding, ToolIssue

try:
    from tree_sitter import Language, Node, Parser
    from tree_sitter_go import language as go_language

    GO_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via GO_AVAILABLE branch
    GO_AVAILABLE = False

# mark3labs/mcp-go's parameter-defining ToolOptions — every exported
# `With*(name string, opts ...PropertyOption) ToolOption` function in the
# package, verified against its own mcp/tools.go rather than assumed. Tool-
# level options (WithDescription, WithToolTitle, WithToolAnnotation, ...)
# are deliberately not in this set — only these declare a named parameter.
MARK3LABS_PARAM_OPTIONS = {
    "WithString", "WithNumber", "WithInteger", "WithBoolean",
    "WithObject", "WithArray", "WithAny",
}


def _text(node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _string_value(node, src: bytes) -> str | None:
    """Resolve `"..."` and `` `...` `` literals, and `+`-joined chains of them
    (a common way to wrap a long tool description across multiple lines —
    verified against `skyhook-io/radar`'s real tool descriptions). A chain
    with any non-string-literal operand is left unresolved rather than
    guessed at."""
    if node is None:
        return None
    if node.type == "interpreted_string_literal":
        frag = next((c for c in node.children if c.type == "interpreted_string_literal_content"), None)
        return _text(frag, src) if frag is not None else ""
    if node.type == "raw_string_literal":
        frag = next((c for c in node.children if c.type == "raw_string_literal_content"), None)
        return _text(frag, src) if frag is not None else ""
    if node.type == "binary_expression":
        operator = node.child_by_field_name("operator")
        if operator is not None and _text(operator, src) == "+":
            left = _string_value(node.child_by_field_name("left"), src)
            right = _string_value(node.child_by_field_name("right"), src)
            if left is not None and right is not None:
                return left + right
    return None


def _composite_fields(node, src: bytes) -> dict[str, "Node"]:
    """For a `composite_literal` (a Go struct literal, e.g. `mcp.Tool{Name: ...}`)
    or a bare `literal_value` (the same thing with its element type elided —
    e.g. each `{Type: "string", ...}` entry inside a typed
    `map[string]*jsonschema.Schema{...}`, which the grammar doesn't wrap in
    its own `composite_literal` since there's no type to attach), map field
    name -> its value node. Skips positional (unkeyed) literals — every real
    call site seen in the wild uses keyed fields for tool structs."""
    if node is None:
        return {}
    if node.type == "composite_literal":
        body = node.child_by_field_name("body")
    elif node.type == "literal_value":
        body = node
    else:
        return {}
    if body is None or body.type != "literal_value":
        return {}
    fields = {}
    for child in body.children:
        if child.type != "keyed_element":
            continue
        key_node = child.child_by_field_name("key")
        value_node = child.child_by_field_name("value")
        if key_node is None or value_node is None:
            continue
        # `key`/`value` are wrapped in a `literal_element`; unwrap to the real node.
        key_inner = key_node.children[0] if key_node.type == "literal_element" and key_node.children else key_node
        value_inner = value_node.children[0] if value_node.type == "literal_element" and value_node.children else value_node
        if key_inner is not None and key_inner.type == "identifier":
            fields[_text(key_inner, src)] = value_inner
    return fields


def _composite_type_name(node, src: bytes) -> str | None:
    """Text of a `composite_literal`'s type field, e.g. 'mcp.Tool' for
    `mcp.Tool{...}`. None for anything else, including a bare `literal_value`
    (which has no type of its own — see `_composite_fields`)."""
    if node is None or node.type != "composite_literal":
        return None
    type_node = node.child_by_field_name("type")
    return _text(type_node, src) if type_node is not None else None


def _resolve_string_field(node, src: bytes, const_registry: dict[str, str] | None = None) -> str | None:
    """Resolve a string literal directly; a bare identifier through
    `const_registry` (a package-level `const NAME = "..."` reference — long
    descriptions are commonly pulled out to their own const, verified against
    `clidey/whodb`'s `descPlatformX` constants); or — for the common i18n
    convention `t(key, defaultValue string) string` (verified against
    `github/github-mcp-server`'s own `TranslationHelperFunc` signature) — the
    literal default-value argument of a call whose last argument is a string
    literal. Anything else (a non-literal fallback, a dynamic key, an
    unregistered/ambiguous const name) is left unresolved rather than
    guessed at."""
    direct = _string_value(node, src)
    if direct is not None:
        return direct
    if node is not None and node.type == "identifier" and const_registry is not None:
        return const_registry.get(_text(node, src))
    if node is not None and node.type == "call_expression":
        args_node = node.child_by_field_name("arguments")
        if args_node is not None:
            call_args = [c for c in args_node.children if c.type not in ("(", ")", ",")]
            if call_args:
                return _string_value(call_args[-1], src)
    return None


def _unwrap_pointer(node):
    """`&mcp.Tool{...}` — a composite literal is almost always taken by pointer
    at a registration call site; unwrap `&<expr>` to `<expr>` itself. Any other
    unary expression (`-x`, `!x`, ...) is left alone, not a pointer literal."""
    if node is None:
        return None
    if node.type == "unary_expression" and node.children and node.children[0].type == "&":
        operand = node.child_by_field_name("operand")
        if operand is not None:
            return operand
    return node


def _selector_field(node) -> str | None:
    """For `pkg.Field` return 'Field'; None if not a selector expression."""
    if node is None or node.type != "selector_expression":
        return None
    field = node.child_by_field_name("field")
    return None if field is None else field.text.decode("utf-8", errors="ignore")


def _has_describe_content(tag_node, src: bytes) -> tuple[bool, int]:
    """A field's `jsonschema:"..."` struct tag content, verified against the
    official SDK's own examples, is the bare description text directly (no
    `description=` key) — e.g. `` `json:"name" jsonschema:"the person to
    greet"` ``. Returns (has_non_trivial_description, its length)."""
    if tag_node is None:
        return False, 0
    raw = _string_value(tag_node, src) or ""
    # Extract the jsonschema:"..." segment out of the combined struct tag string.
    marker = 'jsonschema:"'
    idx = raw.find(marker)
    if idx == -1:
        return False, 0
    rest = raw[idx + len(marker):]
    end = rest.find('"')
    content = rest if end == -1 else rest[:end]
    return bool(content.strip()), len(content.strip())


def _json_field_name(tag_node, src: bytes, fallback: str) -> str:
    """The wire parameter name comes from the `json:"name,omitempty"` tag's
    first comma-separated segment; falls back to the Go field name if there's
    no json tag (the SDK's own behavior when the tag is absent isn't checked
    here since it doesn't affect whether a field counts as documented)."""
    raw = _string_value(tag_node, src) or "" if tag_node is not None else ""
    marker = 'json:"'
    idx = raw.find(marker)
    if idx == -1:
        return fallback
    rest = raw[idx + len(marker):]
    end = rest.find('"')
    content = rest if end == -1 else rest[:end]
    name = content.split(",")[0].strip()
    return name or fallback


def _collect_struct_types(tree_root, src: bytes) -> dict[str, tuple["Node", bytes]]:
    """Repo-wide, name-based registry of `type NAME struct {...}` declarations,
    for resolving a handler's third-parameter type to its field list. Any name
    declared more than once anywhere in the repo is treated as ambiguous and
    left out entirely — the same "don't guess" safety fix already applied to
    the TS/JS analyzer's identical const registry, learned from a real bug
    there (silently resolving to an unrelated same-named local variable)."""
    registry: dict[str, tuple["Node", bytes]] = {}
    ambiguous: set[str] = set()
    for n in _walk(tree_root):
        if n.type != "type_spec":
            continue
        name_node = n.child_by_field_name("name")
        type_node = n.child_by_field_name("type")
        if name_node is None or type_node is None or type_node.type != "struct_type":
            continue
        name = _text(name_node, src)
        if name in ambiguous:
            continue
        if name in registry and registry[name][0] is not type_node:
            ambiguous.add(name)
            del registry[name]
            continue
        registry[name] = (type_node, src)
    return registry


def _collect_function_decls(tree_root, src: bytes) -> dict[str, tuple["Node", bytes]]:
    """Repo-wide, name-based registry of top-level `func NAME(...) {...}`
    declarations, for resolving a handler passed by name rather than inline.
    Same ambiguous-name safety rule as `_collect_struct_types`."""
    registry: dict[str, tuple["Node", bytes]] = {}
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
        if name in registry and registry[name][0] is not n:
            ambiguous.add(name)
            del registry[name]
            continue
        registry[name] = (n, src)
    return registry


def _collect_const_strings(tree_root, src: bytes) -> dict[str, str]:
    """Repo-wide, name-based registry of `const NAME = "literal"` -> its
    string value (e.g. `ToolConversationsHistory = "conversations_history"`),
    for resolving a mark3labs/mcp-go tool's name when it's a const reference
    rather than an inline literal. Same ambiguous-name safety rule as the
    struct/function registries: a name declared more than once with a
    different value is left unresolved rather than guessed."""
    registry: dict[str, str] = {}
    ambiguous: set[str] = set()
    for n in _walk(tree_root):
        if n.type != "const_spec":
            continue
        name_node = n.child_by_field_name("name")
        value_node = n.child_by_field_name("value")
        if name_node is None or value_node is None:
            continue
        # `value` is an `expression_list`; only a single-name `NAME = "x"`
        # spec (not `A, B = "x", "y"`) resolves to one unambiguous literal.
        if value_node.type != "expression_list" or len(value_node.children) != 1:
            continue
        str_val = _string_value(value_node.children[0], src)
        if str_val is None:
            continue
        name = _text(name_node, src)
        if name in ambiguous:
            continue
        if name in registry and registry[name] != str_val:
            ambiguous.add(name)
            del registry[name]
            continue
        registry[name] = str_val
    return registry


def _analyze_new_tool_call(new_tool_call, src: bytes, const_registry: dict[str, str]) -> tuple[str | None, str, int, int]:
    """For a `mcp.NewTool(name, mcp.WithDescription(...), mcp.WithString(...),
    ...)` call (mark3labs/mcp-go's fluent builder), returns
    (name, description, param_count, documented_count). Every parameter is
    declared inline here — no handler inspection needed for this style."""
    args_node = new_tool_call.child_by_field_name("arguments")
    if args_node is None:
        return None, "", 0, 0
    arg_nodes = [c for c in args_node.children if c.type not in ("(", ")", ",")]
    if not arg_nodes:
        return None, "", 0, 0

    name_node = arg_nodes[0]
    if name_node.type == "identifier":
        name_val = const_registry.get(_text(name_node, src))
    else:
        name_val = _string_value(name_node, src)

    description = ""
    param_count = 0
    documented = 0
    for opt in arg_nodes[1:]:
        if opt.type != "call_expression":
            continue
        method = _selector_field(opt.child_by_field_name("function"))
        opt_args_node = opt.child_by_field_name("arguments")
        opt_args = [c for c in opt_args_node.children if c.type not in ("(", ")", ",")] if opt_args_node is not None else []
        if method == "WithDescription":
            if opt_args:
                description = _resolve_string_field(opt_args[0], src, const_registry) or ""
        elif method in MARK3LABS_PARAM_OPTIONS:
            param_count += 1
            has_desc = any(
                a.type == "call_expression" and _selector_field(a.child_by_field_name("function")) == "Description"
                for a in opt_args[1:]
            )
            if has_desc:
                documented += 1

    return name_val, description, param_count, documented


def _build_mark3labs_finding(new_tool_call, rel: str, line: int, src: bytes, const_registry: dict[str, str]) -> ToolFinding | None:
    """Builds a ToolFinding from a `mcp.NewTool(...)` fluent-builder call,
    shared by both real registration shapes verified against production
    repos: a direct `s.AddTool(mcp.NewTool(...), handler)` call, and a
    `server.ServerTool{Tool: mcp.NewTool(...), Handler: ...}` composite
    literal (mark3labs/mcp-go's own exported `ServerTool` struct, used to
    build a tool as data and register it elsewhere, often through a loop —
    see `_walk_server_tool_literals`). Returns None for a dynamic/unresolved
    tool name, same as every other skip-rather-than-guess case in this file."""
    name_val, description, param_count, documented = _analyze_new_tool_call(new_tool_call, src, const_registry)
    if name_val is None:
        return None
    finding = ToolFinding(
        name=name_val,
        file=rel,
        line=line,
        has_description=bool(description.strip()),
        description_len=len(description.strip()),
        param_count=param_count,
        typed_param_count=param_count,
        has_docstring_params=documented >= param_count and param_count > 0,
        has_try_except=True,  # not checked for Go — see module docstring
        has_bare_except=False,
        description_text=description,
    )
    if not finding.has_description:
        finding.issues.append(ToolIssue(
            name_val, rel, finding.line, "description",
            "Tool has no description. An agent cannot decide when to call this.",
            "error",
        ))
    elif finding.description_len < 10:
        finding.issues.append(ToolIssue(
            name_val, rel, finding.line, "description",
            f"Description is only {finding.description_len} chars — likely just restates the name.",
            "warning",
        ))
    if param_count and not finding.has_docstring_params:
        finding.issues.append(ToolIssue(
            name_val, rel, finding.line, "param_docs",
            f"{param_count - documented}/{param_count} builder options have no mcp.Description(...) — "
            "the model only sees names, not intent.",
            "warning",
        ))
    return finding


def _finding_from_name_description(name_val: str, description: str, rel: str, line: int) -> ToolFinding:
    """Shared tail end of building a ToolFinding once a tool's name/description
    text is known but no handler is available to check parameters against —
    used by both the bare-literal and closure-call slice-element paths below.
    `param_count=0` deliberately means "not checked", not "no parameters"."""
    finding = ToolFinding(
        name=name_val,
        file=rel,
        line=line,
        has_description=bool(description.strip()),
        description_len=len(description.strip()),
        param_count=0,
        typed_param_count=0,
        has_docstring_params=False,
        has_try_except=True,  # not checked for Go — see module docstring
        has_bare_except=False,
        description_text=description,
    )
    if not finding.has_description:
        finding.issues.append(ToolIssue(
            name_val, rel, finding.line, "description",
            "Tool has no description. An agent cannot decide when to call this.",
            "error",
        ))
    elif finding.description_len < 10:
        finding.issues.append(ToolIssue(
            name_val, rel, finding.line, "description",
            f"Description is only {finding.description_len} chars — likely just restates the name.",
            "warning",
        ))
    return finding


def _build_tool_slice_element_finding(
    item, rel: str, line: int, src: bytes, const_registry: dict[str, str] | None = None
) -> ToolFinding | None:
    """Builds a ToolFinding from one bare `{Name: "...", Description: "..."}`
    element of a `[]*mcp.Tool{...}`/`[]mcp.Tool{...}` slice literal — see the
    module docstring's `clidey/whodb` note. No handler is available at this
    site, so parameters are deliberately left unchecked (`param_count=0`)
    rather than guessed at."""
    fields = _composite_fields(item, src)
    name_val = _string_value(fields.get("Name"), src)
    if name_val is None:
        return None
    description = _resolve_string_field(fields.get("Description"), src, const_registry) or ""
    return _finding_from_name_description(name_val, description, rel, line)


def _block_statements(block_node) -> list:
    """Top-level statements of a `block` node — e.g. a function or closure
    body — regardless of whether the installed tree-sitter-go wraps them in
    an intermediate `statement_list` node (grammar >=0.24) or lists them as
    the block's own direct children (<0.24; confirmed by diffing the
    grammar's `block`/`_statement_list` rule across its own release tags).
    Doesn't descend into nested blocks (an `if`/`for`/etc.), matching the
    original single-grammar behavior this generalizes."""
    if block_node is None:
        return []
    stmt_list = next((c for c in block_node.children if c.type == "statement_list"), None)
    container = stmt_list if stmt_list is not None else block_node
    return [c for c in container.children if c.type not in ("{", "}")]


def _collect_local_tool_factories(tree_root, src: bytes) -> dict[str, tuple[list[str], dict[str, str]]]:
    """Same-file registry of local closures shaped like
    `read := func(name, description string) *mcp.Tool { return &mcp.Tool{
    Name: name, Description: description, ...} }` — a compact way to define
    many similarly-shaped tools by position (verified against
    `clidey/whodb`'s `read(...)` helper, used for 35 of its tools). Maps the
    closure's name to (its parameter names in call order, {param name: the
    `mcp.Tool` field it's passed straight through to}) — only a direct
    passthrough (`Field: paramName`) is recorded, so a closure that builds a
    field from anything else just won't have that field resolved later.
    Same ambiguous-name safety rule as `_collect_struct_types`: a name
    redeclared differently anywhere in the file is dropped entirely."""
    factories: dict[str, tuple[list[str], dict[str, str]]] = {}
    ambiguous: set[str] = set()
    for node in _walk(tree_root):
        if node.type != "short_var_declaration":
            continue
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None:
            continue
        left_ids = [c for c in left.children if c.type == "identifier"]
        right_vals = [c for c in right.children if c.type == "func_literal"]
        if len(left_ids) != 1 or len(right_vals) != 1:
            continue
        name = _text(left_ids[0], src)
        func_lit = right_vals[0]
        params_node = func_lit.child_by_field_name("parameters")
        body = func_lit.child_by_field_name("body")
        if params_node is None or body is None:
            continue
        param_names: list[str] = []
        for decl in params_node.children:
            if decl.type != "parameter_declaration":
                continue
            param_names.extend(_text(c, src) for c in decl.children if c.type == "identifier")
        if not param_names:
            continue
        return_stmt = next((c for c in _block_statements(body) if c.type == "return_statement"), None)
        if return_stmt is None:
            continue
        expr_list = next((c for c in return_stmt.children if c.type == "expression_list"), None)
        if expr_list is None or len(expr_list.children) != 1:
            continue
        ret_val = _unwrap_pointer(expr_list.children[0])
        if _composite_type_name(ret_val, src) != "mcp.Tool":
            continue
        param_set = set(param_names)
        field_by_param: dict[str, str] = {}
        for field_name, value_node in _composite_fields(ret_val, src).items():
            if value_node.type == "identifier":
                val_name = _text(value_node, src)
                if val_name in param_set:
                    field_by_param[val_name] = field_name
        if not field_by_param:
            continue
        entry = (param_names, field_by_param)
        if name in ambiguous:
            continue
        if name in factories and factories[name] != entry:
            ambiguous.add(name)
            del factories[name]
            continue
        factories[name] = entry
    return factories


def _build_tool_slice_call_finding(
    call_node, factories: dict[str, tuple[list[str], dict[str, str]]], rel: str, line: int, src: bytes
) -> ToolFinding | None:
    """Resolves a `read("name", "description")`-style call to a local
    tool-factory closure (`_collect_local_tool_factories`) into a
    ToolFinding. Only a call with literal-string args, one per closure
    parameter, is resolved — any other shape is left unresolved rather than
    guessed at."""
    func = call_node.child_by_field_name("function")
    if func is None or func.type != "identifier":
        return None
    factory = factories.get(_text(func, src))
    if factory is None:
        return None
    param_names, field_by_param = factory
    args_node = call_node.child_by_field_name("arguments")
    if args_node is None:
        return None
    arg_nodes = [c for c in args_node.children if c.type not in ("(", ")", ",")]
    if len(arg_nodes) != len(param_names):
        return None
    values: dict[str, str] = {}
    for pname, anode in zip(param_names, arg_nodes):
        val = _string_value(anode, src)
        if val is None:
            return None
        values[pname] = val
    name_val = values.get(next((p for p, f in field_by_param.items() if f == "Name"), ""))
    if name_val is None:
        return None
    description = values.get(next((p for p, f in field_by_param.items() if f == "Description"), ""), "")
    return _finding_from_name_description(name_val, description, rel, line)


def _struct_field_docs(struct_type, src: bytes) -> tuple[int, int]:
    """Returns (param_count, documented_count) from a struct's field list,
    counting only exported (capitalized) fields with a `json` tag entry, since
    those are the ones the SDK actually exposes to the model."""
    # `field_declaration_list` is a plain positional child of `struct_type`,
    # not exposed under a named field (unlike `composite_literal`'s `body`).
    body = next((c for c in struct_type.children if c.type == "field_declaration_list"), None)
    if body is None:
        return 0, 0
    param_count = 0
    documented = 0
    for child in body.children:
        if child.type != "field_declaration":
            continue
        name_node = child.child_by_field_name("name")
        tag_node = child.child_by_field_name("tag")
        if name_node is None:
            continue
        param_count += 1
        has_desc, _ = _has_describe_content(tag_node, src)
        if has_desc:
            documented += 1
    return param_count, documented


def _resolve_input_schema(schema_value, src: bytes) -> tuple[int, int] | None:
    """Explicit `InputSchema: &jsonschema.Schema{Properties: map[string]*jsonschema.Schema{
    "x": {..., Description: "..."},
    }}` form. Returns (param_count, documented_count), or None if this isn't
    that shape (caller falls back to struct-tag inference)."""
    schema = _unwrap_pointer(schema_value)
    if schema is None or schema.type != "composite_literal":
        return None
    fields = _composite_fields(schema, src)
    props = fields.get("Properties")
    if props is None:
        return None
    props = _unwrap_pointer(props)
    # The `map[string]*jsonschema.Schema{...}` literal itself is always a
    # `composite_literal` (it has an explicit map type) even though each of
    # its *entries* may have an elided element type (see `_composite_fields`).
    if props is None or props.type != "composite_literal":
        return None
    body = props.child_by_field_name("body")
    if body is None or body.type != "literal_value":
        return 0, 0
    param_count = 0
    documented = 0
    for child in body.children:
        if child.type != "keyed_element":
            continue
        value_node = child.child_by_field_name("value")
        if value_node is None:
            continue
        value_inner = value_node.children[0] if value_node.type == "literal_element" and value_node.children else value_node
        prop_literal = _unwrap_pointer(value_inner)
        if prop_literal is None or prop_literal.type not in ("composite_literal", "literal_value"):
            continue
        param_count += 1
        prop_fields = _composite_fields(prop_literal, src)
        desc_val = prop_fields.get("Description")
        if desc_val is not None and (_string_value(desc_val, src) or "").strip():
            documented += 1
    return param_count, documented


def _handler_last_param_type(handler_node) -> "Node | None":
    """For a `func(ctx, req, args ArgsType) (...)` literal or matching named
    function declaration, return the type node of the *last* parameter — the
    generic `ToolHandlerFor[In, Out]`'s `In`, i.e. the tool's argument struct.
    Taking the last parameter (not literally the third) also covers a
    dependency-injecting wrapper's extra leading param, e.g.
    `func(ctx, deps ToolDependencies, req, args ArgsType) (...)` — verified
    against `github/github-mcp-server`'s own local handler shape."""
    params = handler_node.child_by_field_name("parameters")
    if params is None:
        return None
    decls = [c for c in params.children if c.type == "parameter_declaration"]
    if len(decls) < 3:
        return None
    return decls[-1].child_by_field_name("type")


def _resolve_handler(node, func_registry: dict, src: bytes, depth: int = 0):
    """Resolve the third `AddTool` argument to the actual `func_literal` whose
    third parameter carries the tool's argument type. Handles it being passed
    directly, by name (a top-level named function), or wrapped in one or more
    helper calls — a real, common pattern for cross-cutting concerns like
    panic recovery or logging (e.g. `withPanicRecovery("name", func(...) {...})`,
    seen wrapping every single handler in `xiaohongshu-mcp`). For a wrapper
    call, the `func_literal` argument is taken wherever it appears in the
    call's own argument list (most commonly last); a wrapped identifier is
    resolved the same way a bare one would be. Capped at 3 hops in case of
    nested wrapping."""
    if node is None or depth > 3:
        return None
    if node.type == "func_literal":
        return node
    if node.type == "identifier":
        entry = func_registry.get(node.text.decode("utf-8", errors="ignore"))
        return entry[0] if entry is not None else None
    if node.type == "call_expression":
        args_node = node.child_by_field_name("arguments")
        if args_node is None:
            return None
        for arg in args_node.children:
            if arg.type in ("(", ")", ","):
                continue
            resolved = _resolve_handler(arg, func_registry, src, depth + 1)
            if resolved is not None:
                return resolved
    return None


def find_go_tools(root: Path) -> tuple[list[ToolFinding], list[str]]:
    """Returns (findings, unparseable_relative_paths). Empty if tree_sitter or
    tree_sitter_go isn't installed."""
    if not GO_AVAILABLE:
        return [], []

    lang = Language(go_language())
    parser = Parser(lang)

    root = root.resolve()
    skip_dirs = {"vendor", ".git"}
    files = []
    for p in root.rglob("*.go"):
        rel_parts = p.relative_to(root).parts
        if any(part in skip_dirs or part.startswith(".") for part in rel_parts):
            continue
        if p.name.endswith("_test.go"):
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
        tree = parser.parse(src)
        parsed.append((f, tree.root_node, src))

    def _merge_cross_file(per_file_dicts: list[dict[str, tuple["Node", bytes]]]) -> dict[str, tuple["Node", bytes]]:
        """Merge several already-within-file-safe registries, applying the same
        ambiguous-name rule across files: a name declared in more than one file
        is left out entirely rather than resolved to whichever came first."""
        merged: dict[str, tuple["Node", bytes]] = {}
        ambiguous: set[str] = set()
        for d in per_file_dicts:
            for name, entry in d.items():
                if name in ambiguous:
                    continue
                if name in merged and merged[name][0] is not entry[0]:
                    ambiguous.add(name)
                    del merged[name]
                    continue
                merged[name] = entry
        return merged

    def _merge_const_strings(per_file_dicts: list[dict[str, str]]) -> dict[str, str]:
        """Same cross-file ambiguity rule as `_merge_cross_file`, for the
        plain-string const registry (compares values, not node identity)."""
        merged: dict[str, str] = {}
        ambiguous: set[str] = set()
        for d in per_file_dicts:
            for name, value in d.items():
                if name in ambiguous:
                    continue
                if name in merged and merged[name] != value:
                    ambiguous.add(name)
                    del merged[name]
                    continue
                merged[name] = value
        return merged

    struct_registry = _merge_cross_file([_collect_struct_types(fr, fs) for _f, fr, fs in parsed])
    func_registry = _merge_cross_file([_collect_function_decls(fr, fs) for _f, fr, fs in parsed])
    const_registry = _merge_const_strings([_collect_const_strings(fr, fs) for _f, fr, fs in parsed])

    for f, file_root, src in parsed:
        rel = str(f.relative_to(root))
        local_factories = _collect_local_tool_factories(file_root, src)
        for node in _walk(file_root):
            if node.type == "composite_literal":
                type_text = _composite_type_name(node, src)
                if type_text in ("[]*mcp.Tool", "[]mcp.Tool"):
                    # A typed slice of bare `mcp.Tool` literals — see module
                    # docstring's `clidey/whodb` note. Each element is its own
                    # definition site.
                    body = node.child_by_field_name("body")
                    if body is not None and body.type == "literal_value":
                        for element in body.children:
                            if element.type != "literal_element":
                                continue
                            item = next(
                                (c for c in element.children
                                 if c.type in ("literal_value", "composite_literal", "call_expression")),
                                None,
                            )
                            if item is None:
                                continue
                            if item.type == "call_expression":
                                finding = _build_tool_slice_call_finding(
                                    item, local_factories, rel, item.start_point[0] + 1, src
                                )
                            else:
                                finding = _build_tool_slice_element_finding(
                                    item, rel, item.start_point[0] + 1, src, const_registry
                                )
                            if finding is not None:
                                findings.append(finding)
                    continue
                # mark3labs/mcp-go's own `server.ServerTool{Tool: mcp.NewTool(...),
                # Handler: ...}` struct — a first-class SDK type (server/server.go),
                # used to build a tool as data and register it elsewhere, often
                # through a loop over a slice (verified against
                # hashicorp/terraform-mcp-server, where every tool is built this
                # way, never via a literal `AddTool(mcp.NewTool(...), ...)` call
                # site). The composite literal itself is the definition site.
                if type_text != "server.ServerTool":
                    continue
                fields = _composite_fields(node, src)
                tool_field = fields.get("Tool")
                if tool_field is None or tool_field.type != "call_expression":
                    continue
                if _selector_field(tool_field.child_by_field_name("function")) != "NewTool":
                    continue
                finding = _build_mark3labs_finding(tool_field, rel, node.start_point[0] + 1, src, const_registry)
                if finding is not None:
                    findings.append(finding)
                continue
            if node.type != "call_expression":
                continue
            func = node.child_by_field_name("function")
            is_add_tool = _selector_field(func) == "AddTool"
            args_node = node.child_by_field_name("arguments")
            if args_node is None:
                continue
            arg_nodes = [c for c in args_node.children if c.type not in ("(", ")", ",")]
            if not is_add_tool and not arg_nodes:
                continue

            if is_add_tool and len(arg_nodes) == 2:
                # mark3labs/mcp-go's fluent-builder style: `s.AddTool(mcp.NewTool(...), handler)`.
                new_tool_call = arg_nodes[0]
                if new_tool_call.type != "call_expression" or _selector_field(new_tool_call.child_by_field_name("function")) != "NewTool":
                    continue
                finding = _build_mark3labs_finding(new_tool_call, rel, node.start_point[0] + 1, src, const_registry)
                if finding is not None:
                    findings.append(finding)
                continue

            if is_add_tool:
                if len(arg_nodes) != 3:
                    continue
                tool_arg = _unwrap_pointer(arg_nodes[1])
                handler_arg = arg_nodes[2]
            else:
                # A project-local factory wrapping registration — see module
                # docstring. Recognize any call passing an `mcp.Tool{...}`/
                # `&mcp.Tool{...}` literal, regardless of the outer function's
                # name; the handler is taken as the call's last argument,
                # matching every real factory shape seen so far.
                tool_arg = None
                for a in arg_nodes:
                    unwrapped = _unwrap_pointer(a)
                    if _composite_type_name(unwrapped, src) == "mcp.Tool":
                        tool_arg = unwrapped
                        break
                if tool_arg is None:
                    continue
                handler_arg = arg_nodes[-1]

            if tool_arg is None or tool_arg.type != "composite_literal":
                continue
            fields = _composite_fields(tool_arg, src)
            name_val = _string_value(fields.get("Name"), src)
            if name_val is None:
                continue  # dynamic/referenced tool name — can't attribute a finding to it
            description = _resolve_string_field(fields.get("Description"), src, const_registry) or ""

            handler_node = _resolve_handler(handler_arg, func_registry, src)

            param_count = 0
            documented = 0
            param_doc_label = "struct fields have no jsonschema tag"

            schema_field = fields.get("InputSchema")
            explicit = _resolve_input_schema(schema_field, src) if schema_field is not None else None
            if explicit is not None:
                param_count, documented = explicit
                param_doc_label = "input-schema properties have no Description"
            elif handler_node is not None:
                type_node = _handler_last_param_type(handler_node)
                if type_node is not None and type_node.type == "type_identifier":
                    entry = struct_registry.get(_text(type_node, src))
                    if entry is not None:
                        struct_type, struct_src = entry
                        param_count, documented = _struct_field_docs(struct_type, struct_src)

            finding = ToolFinding(
                name=name_val,
                file=rel,
                line=node.start_point[0] + 1,
                has_description=bool(description.strip()),
                description_len=len(description.strip()),
                param_count=param_count,
                typed_param_count=param_count,
                has_docstring_params=documented >= param_count and param_count > 0,
                has_try_except=True,  # not checked for Go — see module docstring
                has_bare_except=False,
                description_text=description,
            )

            if not finding.has_description:
                finding.issues.append(ToolIssue(
                    name_val, rel, finding.line, "description",
                    "Tool has no description. An agent cannot decide when to call this.",
                    "error",
                ))
            elif finding.description_len < 10:
                finding.issues.append(ToolIssue(
                    name_val, rel, finding.line, "description",
                    f"Description is only {finding.description_len} chars — likely just restates the name.",
                    "warning",
                ))

            if param_count and not finding.has_docstring_params:
                finding.issues.append(ToolIssue(
                    name_val, rel, finding.line, "param_docs",
                    f"{param_count - documented}/{param_count} {param_doc_label} — "
                    "the model only sees names, not intent.",
                    "warning",
                ))

            findings.append(finding)

    return findings, unparseable
