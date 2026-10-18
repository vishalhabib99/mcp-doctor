"""Static analysis of Rust MCP server implementations.

Supports the official Rust SDK's (`rmcp`, modelcontextprotocol/rust-sdk)
attribute style, read against the SDK's own macro source
(`crates/rmcp-macros/src/tool.rs` and `common.rs`) rather than assumed:

    #[tool(name = "sum", description = "Calculate the sum of two numbers")]
    async fn sum(&self, Parameters(req): Parameters<SumRequest>) -> String { ... }

The macro's own rules, mirrored here:

  - name: the `name = "..."` argument, else the function's own name
  - description: the `description = ...` argument, else the function's doc
    comments (each line trimmed, blank lines dropped, joined with "\\n"); a
    `description` argument replaces the doc comments entirely
  - parameters: the first argument typed `Parameters<T>`, whose schema schemars
    derives from struct `T` at build time — a field's `///` doc comment or
    `#[schemars(description = ...)]` becomes its description, `Option<_>` and
    `#[serde(default)]` make it optional, and serde's `rename`/`rename_all`
    set the name the model sees. An explicit `input_schema = ...` argument
    replaces all of that and is left unread.

Of the 20 most-starred Rust MCP servers sampled on 2026-10-18 (build-or-not
record in the outreach repo), 11 register every tool this way. A description
or parameter type that isn't a literal (a `const` defined elsewhere is
resolved; `include_str!`, `format!`, a generic `T::Parameter` are not) is
left unread rather than guessed at: a description counts as present with no
length check, and parameters go unchecked.

Also recognizes the hand-written `tools/list` shape two more servers in that
sample use (joaoh82/rustunnel, jahala/tilth): one `json!({"name": "...",
"description": "...", "inputSchema": {...}})` object per tool. Only an object
with both a literal `name` and an `inputSchema` key counts, so a server-info
block or an unrelated JSON payload isn't read as a tool.

Error handling isn't checked, for the same reason as in go_analyzer: a Rust
handler reports failure through its `Result`, which the SDK turns into a tool
error whatever the handler does.

Requires the optional `tree_sitter` / `tree_sitter_rust` packages — callers
should treat their absence as "skip Rust analysis", not an error.
"""

from __future__ import annotations

import re
from pathlib import Path

from .analyzer import ToolFinding, ToolIssue, description_display_width

try:
    from tree_sitter import Language, Node, Parser
    from tree_sitter_rust import language as rust_language

    RUST_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via RUST_AVAILABLE branch
    RUST_AVAILABLE = False

TOOL_ATTRIBUTE_PATHS = {"tool", "rmcp::tool"}
# Not the server's own code: build output, vendored crates, test-only and
# benchmark crates (the Go analyzer skips `_test.go` the same way).
SKIP_DIRS = {"target", "vendor", "tests", "benches", "fuzz", "node_modules"}
_UNKNOWN = object()  # a description that exists but isn't a literal


def _text(node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def _walk(node):
    """Pre-order walk without recursion, like go_analyzer's."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


_ESCAPE = re.compile(r"\\(?:\n[ \t\r\n]*|u\{([0-9a-fA-F]{1,6})\}|x([0-9a-fA-F]{2})|(.))", re.S)
_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", '"': '"', "'": "'"}


def _unescape(body: str) -> str:
    def repl(m: re.Match) -> str:
        if m.group(1):
            return chr(int(m.group(1), 16))
        if m.group(2):
            return chr(int(m.group(2), 16))
        if m.group(3) is not None:
            return _SIMPLE_ESCAPES.get(m.group(3), m.group(3))
        return ""  # `\` at a line end: the newline and the next line's indent are dropped
    return _ESCAPE.sub(repl, body)


def _string_value(node, src: bytes) -> str | None:
    if node is None:
        return None
    raw = _text(node, src)
    if node.type == "string_literal":
        start = raw.index('"')
        return _unescape(raw[start + 1 : -1])
    if node.type == "raw_string_literal":
        m = re.match(r'^[a-z]*r(#*)"(.*)"\1$', raw, re.S)
        return m.group(2) if m else None
    return None


def _collect_const_strings(tree_root, src: bytes) -> dict[str, str]:
    """`const NAME: &str = "...";` / `static NAME: &str = "...";` at any level."""
    out: dict[str, str] = {}
    for node in _walk(tree_root):
        if node.type in ("const_item", "static_item"):
            name = node.child_by_field_name("name")
            value = _string_value(node.child_by_field_name("value"), src)
            if name is not None and value is not None:
                out[_text(name, src)] = value
    return out


def _value_string(nodes: list, src: bytes, consts: dict[str, str]):
    """A description written as a literal, a `concat!` of literals, or a const
    → its text; anything else → _UNKNOWN."""
    if len(nodes) == 1:
        n = nodes[0]
        if n.type == "call_expression":  # "...".to_string() / .into() / .to_owned()
            func = n.child_by_field_name("function")
            args = n.child_by_field_name("arguments")
            if (func is not None and func.type == "field_expression" and args is not None
                    and args.named_child_count == 0
                    and _text(func.child_by_field_name("field"), src) in ("to_string", "into", "to_owned")):
                n = func.child_by_field_name("value")
        value = _string_value(n, src)
        if value is not None:
            return value
        if n.type == "identifier" and _text(n, src) in consts:
            return consts[_text(n, src)]
        return _UNKNOWN
    if (len(nodes) == 4 and nodes[1].type == "." and _text(nodes[2], src) in ("to_string", "into", "to_owned")
            and _text(nodes[3], src).replace(" ", "") == "()"):
        return _value_string(nodes[:1], src, consts)  # the same, as tokens inside a macro
    if (len(nodes) == 3 and nodes[0].type == "identifier" and _text(nodes[0], src) == "concat"
            and nodes[1].type == "!" and nodes[2].type == "token_tree"):
        parts = []
        for c in nodes[2].children[1:-1]:
            if c.type == ",":
                continue
            value = _value_string([c], src, consts)
            if value is _UNKNOWN:
                return _UNKNOWN
            parts.append(value)
        return "".join(parts)
    return _UNKNOWN


def _split_args(token_tree, src: bytes) -> dict[str, list]:
    """`(name = "x", description = "y", annotations(...))` → {key: value nodes};
    a bare key (`local`, `annotations(...)`) maps to the nodes after it."""
    args: dict[str, list] = {}
    if token_tree is None:
        return args
    current: list = []
    for c in token_tree.children[1:-1]:
        if c.type == ",":
            if current:
                key = _text(current[0], src)
                args[key] = current[2:] if len(current) > 1 and current[1].type == "=" else current[1:]
            current = []
        else:
            current.append(c)
    if current:
        key = _text(current[0], src)
        args[key] = current[2:] if len(current) > 1 and current[1].type == "=" else current[1:]
    return args


def _attribute_parts(attr_item, src: bytes) -> tuple[str, "Node | None", "Node | None"]:
    """attribute_item → (path, args token_tree, `= value` node)."""
    attr = next((c for c in attr_item.children if c.type == "attribute"), None)
    if attr is None:
        return "", None, None
    path = _text(attr.children[0], src) if attr.children else ""
    tree = next((c for c in attr.children if c.type == "token_tree"), None)
    value = attr.child_by_field_name("value")
    return path, tree, value


def _leading_items(node) -> list:
    """The attributes and comments directly above an item, in source order."""
    items = []
    prev = node.prev_sibling
    while prev is not None and prev.type in ("attribute_item", "line_comment", "block_comment"):
        items.append(prev)
        prev = prev.prev_sibling
    items.reverse()
    return items


def _is_doc_comment(node) -> bool:
    return any(c.type == "outer_doc_comment_marker" for c in node.children)


def _doc_text(items: list, src: bytes):
    """What rustdoc (and so rmcp/schemars) reads as the item's docs: each
    line trimmed, blank lines dropped. _UNKNOWN for `#[doc = include_str!(..)]`."""
    lines: list[str] = []
    for item in items:
        if item.type in ("line_comment", "block_comment") and _is_doc_comment(item):
            doc = next((c for c in item.children if c.type == "doc_comment"), None)
            text = _text(doc, src) if doc is not None else ""
            if item.type == "block_comment":
                text = "\n".join(line.strip().lstrip("*") for line in text.splitlines())
            if text.strip():
                lines.append(text.strip())
        elif item.type == "attribute_item":
            path, _tree, value = _attribute_parts(item, src)
            if path == "doc" and value is not None:
                doc = _string_value(value, src)
                if doc is None:
                    return _UNKNOWN
                if doc.strip():
                    lines.append(doc.strip())
    return "\n".join(lines)


def _attr_args(items: list, src: bytes, path: str) -> list[dict[str, list]]:
    return [_split_args(tree, src) for item in items if item.type == "attribute_item"
            for p, tree, _v in [_attribute_parts(item, src)] if p == path]


def _has_flag(items: list, src: bytes, path: str, flag: str) -> bool:
    return any(flag in args for args in _attr_args(items, src, path))


def _is_cfg_test(items: list, src: bytes) -> bool:
    for item in items:
        if item.type == "attribute_item":
            path, tree, _v = _attribute_parts(item, src)
            if path == "cfg" and tree is not None and re.fullmatch(r"\(\s*test\s*\)", _text(tree, src)):
                return True
    return False


_RENAME_ALL = {
    "lowercase": lambda w: "".join(w),
    "UPPERCASE": lambda w: "".join(w).upper(),
    "PascalCase": lambda w: "".join(x.capitalize() for x in w),
    "camelCase": lambda w: w[0] + "".join(x.capitalize() for x in w[1:]),
    "snake_case": lambda w: "_".join(w),
    "SCREAMING_SNAKE_CASE": lambda w: "_".join(w).upper(),
    "kebab-case": lambda w: "-".join(w),
    "SCREAMING-KEBAB-CASE": lambda w: "-".join(w).upper(),
}


def _serde_string(items: list, src: bytes, key: str) -> str | None:
    for args in _attr_args(items, src, "serde"):
        if key in args:
            value = _value_string(args[key], src, {})
            if isinstance(value, str):
                return value
    return None


def _strip_option(type_node, src: bytes) -> bool:
    """True when the field's type is `Option<_>` (schemars leaves it out of `required`)."""
    if type_node is None or type_node.type != "generic_type":
        return False
    base = type_node.child_by_field_name("type")
    return base is not None and _text(base, src).split("::")[-1] == "Option"


def _type_name(type_node, src: bytes) -> str | None:
    """Last path segment of a plain named type (`tools::GotoInput` → `GotoInput`),
    None for anything generic over a type parameter (`T::Parameter`)."""
    if type_node is None:
        return None
    if type_node.type == "type_identifier":
        return _text(type_node, src)
    if type_node.type == "scoped_type_identifier":
        path = type_node.child_by_field_name("path")
        if path is not None and _text(path, src) in ("Self", "T") or (
            path is not None and re.fullmatch(r"[A-Z]", _text(path, src))
        ):
            return None
        name = type_node.child_by_field_name("name")
        return _text(name, src) if name is not None else None
    return None


class _StructParams:
    """(names, required, documented flags) for a `Parameters<T>` struct, or None
    when the struct can't be read field by field."""

    def __init__(self, registry: dict[str, list[tuple["Node", bytes]]]):
        self.registry = registry

    def lookup(self, name: str, prefer_src: bytes | None):
        entries = self.registry.get(name, [])
        same_file = [e for e in entries if e[1] is prefer_src]
        if len(same_file) == 1:
            return same_file[0]
        return entries[0] if len(entries) == 1 else None

    def read(self, name: str, prefer_src: bytes | None, depth: int = 0):
        entry = self.lookup(name, prefer_src)
        if entry is None or depth > 3:
            return None
        struct, src = entry
        body = struct.child_by_field_name("body")
        if body is None or body.type != "field_declaration_list":
            return None
        struct_items = _leading_items(struct)
        rename_all = _serde_string(struct_items, src, "rename_all")
        struct_default = _has_flag(struct_items, src, "serde", "default")
        params: list[tuple[str, bool, bool]] = []  # (name, required, documented)
        for field in body.children:
            if field.type != "field_declaration":
                continue
            items = _leading_items(field)
            if any(_has_flag(items, src, p, f) for p in ("serde", "schemars")
                   for f in ("skip", "skip_deserializing")):
                continue
            type_node = field.child_by_field_name("type")
            if _has_flag(items, src, "serde", "flatten"):
                inner = self.read(_type_name(type_node, src) or "", src, depth + 1)
                if inner is None:
                    return None
                params.extend(inner)
                continue
            raw = _text(field.child_by_field_name("name"), src).removeprefix("r#")
            renamed = _serde_string(items, src, "rename")
            if renamed is None and rename_all in _RENAME_ALL:
                renamed = _RENAME_ALL[rename_all](raw.split("_"))
            doc = _doc_text(items, src)
            documented = doc is _UNKNOWN or bool(doc) or any(
                k in args for args in _attr_args(items, src, "schemars") for k in ("description", "doc")
            )
            required = not (_strip_option(type_node, src) or struct_default
                            or _has_flag(items, src, "serde", "default"))
            params.append((renamed or raw, required, documented))
        return params


def _parameters_type(fn_node, src: bytes):
    """The `T` of the first `Parameters<T>` argument, as rmcp's macro finds it."""
    params = fn_node.child_by_field_name("parameters")
    if params is None:
        return None, False
    for p in params.children:
        if p.type != "parameter":
            continue
        t = p.child_by_field_name("type")
        if t is None or t.type != "generic_type":
            continue
        base = t.child_by_field_name("type")
        if base is None or _text(base, src).split("::")[-1] != "Parameters":
            continue
        args = t.child_by_field_name("type_arguments")
        inner = next((c for c in args.children if c.is_named), None) if args is not None else None
        return inner, True
    return None, False


def _make_finding(name: str, description, rel: str, line: int, params, label: str) -> ToolFinding:
    known = isinstance(description, str)
    text = description.strip() if known else ""
    finding = ToolFinding(
        name=name,
        file=rel,
        line=line,
        has_description=bool(text) or not known,
        description_len=len(text),
        description_display_width=description_display_width(text),
        param_count=len(params) if params else 0,
        typed_param_count=len(params) if params else 0,
        has_docstring_params=bool(params) and all(d for _n, _r, d in params),
        has_try_except=True,  # not checked for Rust — see module docstring
        has_bare_except=False,
        description_text=description if known else "",
        params_tracked=params is not None,
    )
    if params is not None:
        finding.param_names = [n for n, _r, _d in params]
        finding.required_param_names = [n for n, r, _d in params if r]
    if not finding.has_description:
        finding.issues.append(ToolIssue(
            name, rel, line, "description",
            "Tool has no description. An agent cannot decide when to call this.",
            "error",
        ))
    elif known and finding.description_display_width < 10:
        finding.issues.append(ToolIssue(
            name, rel, line, "description",
            f"Description is only {finding.description_len} chars — likely just restates the name.",
            "warning",
        ))
    if params and not finding.has_docstring_params:
        missing = sum(1 for _n, _r, d in params if not d)
        finding.issues.append(ToolIssue(
            name, rel, line, "param_docs",
            f"{missing}/{len(params)} {label} — the model only sees names, not intent.",
            "warning",
        ))
    return finding


def _tool_attribute_finding(fn_node, src: bytes, rel: str, consts: dict[str, str],
                            structs: _StructParams, rewrites_descriptions: bool = False) -> ToolFinding | None:
    items = _leading_items(fn_node)
    tool_attr = next((
        tree for item in items if item.type == "attribute_item"
        for path, tree, _v in [_attribute_parts(item, src)] if path in TOOL_ATTRIBUTE_PATHS
    ), _UNKNOWN)
    if tool_attr is _UNKNOWN:
        return None
    args = _split_args(tool_attr, src)

    fn_name = fn_node.child_by_field_name("name")
    name = _value_string(args["name"], src, consts) if "name" in args else (
        _text(fn_name, src) if fn_name is not None else _UNKNOWN)
    if not isinstance(name, str):
        return None  # a computed name: don't attribute findings to a guess

    description = (_value_string(args["description"], src, consts) if "description" in args
                   else _doc_text(items, src))
    if description == "" and rewrites_descriptions:
        description = _UNKNOWN  # filled in at runtime — see _rewrites_descriptions

    params = None
    label = "fields have no doc comment or #[schemars(description)]"
    if "input_schema" not in args:
        type_node, found = _parameters_type(fn_node, src)
        if not found:
            params = []  # no Parameters<T>: rmcp gives the tool an empty schema
        else:
            type_name = _type_name(type_node, src)
            if type_name is not None:
                params = structs.read(type_name, src)
                label = f"fields of {type_name} have no doc comment or #[schemars(description)]"
    line = (fn_name or fn_node).start_point[0] + 1
    return _make_finding(name, description, rel, line, params, label)


def _object_pairs(token_tree, src: bytes) -> dict[str, list] | None:
    """`{"k": v, ...}` inside a `json!` → {k: value nodes}; None if it isn't one."""
    if not token_tree.children or token_tree.children[0].type != "{":
        return None
    pairs: dict[str, list] = {}
    current: list = []
    for c in token_tree.children[1:-1] + [None]:
        if c is None or c.type == ",":
            if len(current) >= 3 and current[1].type == ":":
                key = _string_value(current[0], src)
                if key is not None:
                    pairs[key] = current[2:]
            current = []
        else:
            current.append(c)
    return pairs


def _json_schema_params(schema_nodes: list, src: bytes):
    if len(schema_nodes) != 1 or schema_nodes[0].type != "token_tree":
        return None
    schema = _object_pairs(schema_nodes[0], src)
    if schema is None:
        return None
    props_nodes = schema.get("properties")
    if props_nodes is None:
        return []
    if len(props_nodes) != 1 or props_nodes[0].type != "token_tree":
        return None
    props = _object_pairs(props_nodes[0], src)
    if props is None:
        return None
    required: set[str] = set()
    req_nodes = schema.get("required")
    if req_nodes and req_nodes[0].type == "token_tree":
        required = {v for c in req_nodes[0].children if (v := _string_value(c, src)) is not None}
    params = []
    for key, value in props.items():
        if len(value) == 1 and value[0].type == "token_tree":
            body = _object_pairs(value[0], src) or {}
            documented = "description" in body
        else:
            documented = True  # built elsewhere (`"health_check": health_check_schema`): not read
        params.append((key, key in required, documented))
    return params


def _json_tool_finding(token_tree, src: bytes, rel: str, consts: dict[str, str]) -> ToolFinding | None:
    pairs = _object_pairs(token_tree, src)
    # MCP's key is `inputSchema`; an `input_schema` object is an LLM-API tool
    # (lean-ctx's proxy adds one to Anthropic requests), not an MCP tool.
    if not pairs or "name" not in pairs or "inputSchema" not in pairs:
        return None
    name = _value_string(pairs["name"], src, consts)
    if not isinstance(name, str):
        return None
    description = _value_string(pairs["description"], src, consts) if "description" in pairs else ""
    params = _json_schema_params(pairs["inputSchema"], src)
    line = token_tree.start_point[0] + 1
    return _make_finding(name, description, rel, line, params, "input-schema properties have no description")


def _json_bodies(node, src: bytes):
    """The `(...)` of every `json!(...)` under node. A `json!` nested in another
    macro's tokens (`vec![serde_json::json!({...}), ...]`) isn't a macro node to
    tree-sitter, only `json` `!` `(...)` tokens, so both are matched that way."""
    for n in _walk(node):
        if n.type == "token_tree":
            bang = n.prev_sibling
            if (bang is not None and bang.type == "!" and bang.prev_sibling is not None
                    and _text(bang.prev_sibling, src).split("::")[-1] == "json"):
                yield n


def _token_calls(token_tree, src: bytes):
    """`path(arg, arg, ...)` written inside another macro's tokens
    (`vec![Tool::new(...), ...]`) → (path text, [arg token lists], first node)."""
    children = token_tree.children
    for i, c in enumerate(children):
        if c.type != "token_tree" or _text(c, src)[:1] != "(" or i == 0:
            continue
        j = i - 1
        if children[j].type != "identifier":
            continue
        while j >= 2 and children[j - 1].type == "::" and children[j - 2].type == "identifier":
            j -= 2
        if j > 0 and children[j - 1].type in ("!", ".", "fn"):
            continue  # a macro body, a method call, or a definition
        args: list[list] = [[]]
        for t in c.children[1:-1]:
            if t.type == ",":
                args.append([])
            elif t.type not in ("line_comment", "block_comment"):
                args[-1].append(t)
        if not args[-1]:
            args.pop()
        path = "".join(_text(n, src) for n in children[j:i])
        yield path, args, children[j]


def _call_args(call, src: bytes):
    """A call_expression → (path text, [arg node lists], node)."""
    func = call.child_by_field_name("function")
    args_node = call.child_by_field_name("arguments")
    if func is None or args_node is None:
        return None
    args = [[c] for c in args_node.named_children if c.type not in ("line_comment", "block_comment")]
    return _text(func, src), args, call


def _constructor_finding(call, src: bytes, rel: str, consts: dict[str, str], uses_rmcp: bool) -> ToolFinding | None:
    """rmcp's own `Tool::new(name, description, schema)`, or a project helper
    with the same argument order whose name says tool and whose schema is a
    `json!` literal — yvgude/lean-ctx defines all 96 of its tools as
    `tool_def("ctx_read", "Read file ...", json!({...}))`. `call` is
    (path, [arg node lists], node), from a call node or from macro tokens."""
    if call is None:
        return None
    func_text, args, node = call
    if len(args) != 3:
        return None
    path = func_text.replace(" ", "").split("::")
    schema_bodies = [b for n in args[2] for b in _json_bodies(n, src)]
    if path[-2:] == ["Tool", "new"]:
        if not uses_rmcp:
            return None
    elif not ("tool" in path[-1].lower() and schema_bodies):
        return None
    name = _value_string(args[0], src, consts)
    if not isinstance(name, str):
        return None
    description = _value_string(args[1], src, consts)
    params = None
    if schema_bodies:
        inner = [c for c in schema_bodies[0].named_children if c.type == "token_tree"]
        params = _json_schema_params(inner[:1], src)
    line = node.start_point[0] + 1
    return _make_finding(name, description, rel, line, params, "input-schema properties have no description")


def _rewrites_descriptions(file_root, src: bytes) -> tuple[bool, bool]:
    """(assigns some `x.description = ...`, touches the tool router or list_tools).
    A crate doing both can set descriptions at runtime: genomoncology/biomcp
    writes `route.attr.description` from a catalog table, portofcontext/pctx
    overrides `tool.description` in its own `list_tools`, and both leave their
    `#[tool]` attributes without one."""
    assigns = any(
        n.type == "assignment_expression"
        and (left := n.child_by_field_name("left")) is not None and left.type == "field_expression"
        and _text(left.child_by_field_name("field"), src) == "description"
        for n in _walk(file_root)
    )
    return assigns, b"fn list_tools" in src or b"ToolRouter" in src


def _crate_root(rel: str, root: Path) -> str:
    d = Path(rel).parent
    while d != d.parent and not (root / d / "Cargo.toml").is_file():
        d = d.parent
    return str(d)


def _cargo_uses_rmcp(root: Path) -> bool:
    for manifest in root.rglob("Cargo.toml"):
        if any(part in ("target", ".git") for part in manifest.relative_to(root).parts):
            continue
        try:
            # `rmcp = ...`, or the table form `[dependencies.rmcp]` (elastic/mcp-server-elasticsearch)
            if re.search(r"^\s*rmcp\s*=|^\s*\[[\w.-]*dependencies\.rmcp\]", manifest.read_text(errors="ignore"), re.M):
                return True
        except OSError:
            continue
    return False


def _test_ranges(file_root, src: bytes) -> list[tuple[int, int]]:
    """Byte ranges of `#[cfg(test)]` modules, functions and impls."""
    return [
        (n.start_byte, n.end_byte) for n in _walk(file_root)
        if n.type in ("mod_item", "function_item", "impl_item") and _is_cfg_test(_leading_items(n), src)
    ]


def test_line_ranges(src: bytes) -> list[tuple[int, int]]:
    """1-based line ranges of `#[cfg(test)]` code, for the repo-wide secrets scan."""
    if not RUST_AVAILABLE:
        return []
    file_root = Parser(Language(rust_language())).parse(src).root_node
    ranges = []
    for start, end in _test_ranges(file_root, src):
        ranges.append((src.count(b"\n", 0, start) + 1, src.count(b"\n", 0, end) + 1))
    return ranges


def find_rust_tools(root: Path) -> tuple[list[ToolFinding], list[str]]:
    """Returns (findings, unparseable_relative_paths). Empty if tree_sitter or
    tree_sitter_rust isn't installed."""
    if not RUST_AVAILABLE:
        return [], []

    root = root.resolve()
    files = []
    for p in sorted(root.rglob("*.rs")):  # same order on every Python version
        parts = p.relative_to(root).parts
        if any(part in SKIP_DIRS or part.startswith(".") for part in parts[:-1]):
            continue
        if p.name in ("tests.rs", "build.rs") or p.name.endswith(("_test.rs", "_tests.rs")):
            continue
        files.append(p)
    if not files:
        return [], []

    parser = Parser(Language(rust_language()))
    parsed: list[tuple[str, "Node", bytes]] = []
    for f in files:
        try:
            src = f.read_bytes()
        except OSError:
            continue
        parsed.append((str(f.relative_to(root)), parser.parse(src).root_node, src))

    consts: dict[str, str] = {}
    ambiguous: set[str] = set()
    registry: dict[str, list[tuple["Node", bytes]]] = {}
    for _rel, file_root, src in parsed:
        for k, v in _collect_const_strings(file_root, src).items():
            if k in consts and consts[k] != v:
                ambiguous.add(k)
            consts[k] = v
        for node in _walk(file_root):
            if node.type == "struct_item":
                name = node.child_by_field_name("name")
                if name is not None:
                    registry.setdefault(_text(name, src), []).append((node, src))
    for k in ambiguous:
        consts.pop(k, None)
    structs = _StructParams(registry)
    uses_rmcp = _cargo_uses_rmcp(root)
    crate_flags: dict[str, list[bool]] = {}
    for rel, file_root, src in parsed:
        assigns, hooks = _rewrites_descriptions(file_root, src)
        flags = crate_flags.setdefault(_crate_root(rel, root), [False, False])
        flags[0] |= assigns
        flags[1] |= hooks

    findings: list[ToolFinding] = []
    for rel, file_root, src in parsed:
        test_ranges = _test_ranges(file_root, src)

        def in_test(n) -> bool:
            return any(s <= n.start_byte < e for s, e in test_ranges)

        constructor_args: set[int] = set()  # json! bodies already read as a constructor's schema
        seen_objects: set[int] = set()  # a json! nested in another is walked once

        for node in _walk(file_root):
            if node.type == "function_item" and uses_rmcp:
                finding = _tool_attribute_finding(node, src, rel, consts, structs,
                                                  all(crate_flags[_crate_root(rel, root)]))
                if finding is not None and not in_test(node):
                    findings.append(finding)
            elif node.type == "call_expression" and not in_test(node):
                finding = _constructor_finding(_call_args(node, src), src, rel, consts, uses_rmcp)
                if finding is not None:
                    findings.append(finding)
                    constructor_args.update(t.start_byte for t in _json_bodies(node, src))
            elif node.type == "token_tree" and not in_test(node):
                for call in _token_calls(node, src):
                    finding = _constructor_finding(call, src, rel, consts, uses_rmcp)
                    if finding is not None:
                        findings.append(finding)
                        constructor_args.update(t.start_byte for n in call[1][2] for t in _json_bodies(n, src))
        for body in _json_bodies(file_root, src):
            if body.start_byte in constructor_args or in_test(body):
                continue
            for tree in _walk(body):
                if tree.type == "token_tree" and tree.start_byte not in seen_objects:
                    seen_objects.add(tree.start_byte)
                    finding = _json_tool_finding(tree, src, rel, consts)
                    if finding is not None:
                        findings.append(finding)
    return findings, []
