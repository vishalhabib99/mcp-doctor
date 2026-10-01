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
argument), and scores them. For the low-level SDK style, an ``inputSchema``
property may itself be built by a shared zero-arg helper function
(``"paper_id": _paper_id_property()``) or spread in from one
(``**_page_properties()``) rather than written inline — verified against
``blazickjp/arxiv-mcp-server`` — and is resolved the same way rather than
being reported as undocumented.
against a set of conformance and quality checks that matter for an agent
actually calling the tool at runtime: does it have a description an LLM
can act on, are parameters documented and typed, does it handle errors
instead of leaking stack traces back to the model, is it documented for
humans in the README.
"""

from __future__ import annotations

import ast
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

SECRET_PATTERN = re.compile(
    r"""(api[_-]?key|secret|token|password|access[_-]?key)\s*=\s*["'](?=[^"']*\d)[A-Za-z0-9_\-/+]{12,}["']""",
    re.IGNORECASE,
)

FASTMCP_DECORATOR_NAMES = {"tool"}

# Spec: https://modelcontextprotocol.io/specification/2026-07-28/server/tools#tool-names
# "Tool names SHOULD be between 1 and 128 characters... allowed characters: A-Z, a-z,
# 0-9, _, -, . ... SHOULD NOT contain spaces, commas... SHOULD be unique within a server."
VALID_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

# A tool whose own description says it's deprecated is a compatibility shim,
# not something a maintainer would reasonably keep advertising in the
# README's tool list — flagging it as an undocumented gap would push toward
# documenting a tool the project is actively trying to phase out. Verified
# against a real false positive dogfooding firecrawl/firecrawl-mcp-server:
# `firecrawl_extract`'s description opens with "Deprecated compatibility
# entry point. Use firecrawl_scrape..." and is correctly absent from the
# README's tool list, which only documents the current surface.
_DEPRECATED_RE = re.compile(r"\bdeprecated\b", re.IGNORECASE)

_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _readme_and_linked_docs_text(readme: Path, root: Path) -> str:
    """The README's own text, plus the text of any local .md file it links
    to directly (one hop, not transitive) — a large project commonly
    factors its actual content (a full tool/feature list, in particular)
    out into a linked doc rather than inlining it, and a tool name search
    that only reads the README itself would call every one of those tools
    undocumented. Verified against a real false positive dogfooding the
    official `modelcontextprotocol/servers` `everything` reference server:
    its README says outright "A complete list of the registered MCP
    primitives... can be found in the Server Features document" and links
    `docs/features.md`, which does list every tool by its exact name — the
    README itself never repeats them.

    Only same-repo relative links are followed (no http(s)/mailto, no
    escaping the repo root via `..`) — this reads local documentation the
    project itself ships, not arbitrary URLs."""
    text = readme.read_text(errors="ignore")
    combined = [text]
    root_resolved = root.resolve()
    for match in _MD_LINK_RE.finditer(text):
        target = match.group(1).split("#", 1)[0].strip()
        if not target.lower().endswith(".md") or "://" in target or target.startswith("mailto:"):
            continue
        candidate = (readme.parent / target).resolve()
        if root_resolved not in candidate.parents and candidate != root_resolved:
            continue
        if not candidate.is_file():
            continue
        try:
            combined.append(candidate.read_text(errors="ignore"))
        except OSError:
            continue
    return "\n".join(combined)


def description_display_width(text: str) -> int:
    """Terminal/`wcwidth`-style display width: a Wide or Fullwidth character
    (CJK and similar dense scripts, per Unicode's own East Asian Width
    property) counts as 2 columns, everything else as 1 — the standard
    convention terminals already use to size text, reused here so the
    length-based description-quality heuristic below isn't calibrated to
    English's character density alone. Found on a real repo,
    `xpzouying/xiaohongshu-mcp`: complete, well-formed Chinese descriptions
    (e.g. "检查小红书登录状态", 9 characters, a full sentence) were read as
    artificially short — 9 raw characters undercounts how much a CJK
    character actually conveys relative to a Latin one, while display width
    (18) reflects it fairly without guessing at a language-specific
    conversion factor."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


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
    description_display_width: int
    param_count: int
    typed_param_count: int
    has_docstring_params: bool
    has_try_except: bool
    has_bare_except: bool
    description_text: str = ""
    issues: list[ToolIssue] = field(default_factory=list)
    # Python-only for now — populated in _analyze_function_as_tool (every
    # decorator/class-based registration style funnels through it) and in
    # _find_lowlevel_tools for the raw Tool(inputSchema=...) constructor
    # style, whenever every property name is statically resolvable. Used by
    # schema_diff.py to catch a breaking change between two runs. Empty for
    # TS/Go tools, and for a raw-schema tool with an unresolvable property,
    # same incremental language-by-language pattern as everything else here;
    # comparing empty-to-empty across two runs is a natural no-op, not a
    # false positive.
    param_names: list[str] = field(default_factory=list)
    required_param_names: list[str] = field(default_factory=list)
    # True unless a raw-schema property name couldn't be resolved to a string
    # literal (e.g. a variable used as a dict key), in which case param_names
    # itself is a known-incomplete subset of the tool's real parameters —
    # schema_diff.py must not treat a name missing from an incomplete list as
    # removed, only as "not statically visible" (reported by Edward
    # Izgorodin, github.com/modelcontextprotocol/modelcontextprotocol#3322).
    param_names_complete: bool = True


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


def _is_plain_string_metadata(e: ast.expr) -> bool:
    return isinstance(e, ast.Constant) and isinstance(e.value, str) and bool(e.value.strip())


def _annotated_elts_have_description(
    elts: list[ast.expr], alias_registry: dict[str, bool], plain_string_ok: bool = True
) -> bool:
    for i, e in enumerate(elts):
        if _field_call_has_description(e):
            return True
        # `Annotated[T, "text"]`: the first element is the type, so only
        # metadata after it counts. See _plain_string_docs_reach_model.
        if plain_string_ok and i > 0 and _is_plain_string_metadata(e):
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


# Repo-local names for FastMCP's Context, e.g. `ScoreContext = Context[AppState, Any]`
# (tskovlund/mcp-score). Set per scan by analyze_repo via _collect_context_aliases.
_CONTEXT_NAMES: set[str] = {"Context"}


def _collect_context_aliases(trees: list[tuple[str, "ast.Module"]]) -> set[str]:
    """Module-level aliases of Context: `X = Context`, `X = Context[...]`,
    `X: TypeAlias = Context[...]` and `type X = Context[...]`. Name-based,
    like the rest of the Context handling."""
    names = {"Context"}

    def base(value):
        if isinstance(value, ast.Subscript):
            value = value.value
        return _annotation_base_name(value)

    for _, tree in trees:
        for node in tree.body:
            target, value = None, None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target, value = node.target, node.value
            elif type(node).__name__ == "TypeAlias":  # `type X = ...`, Python 3.12+
                target, value = node.name, node.value
            if isinstance(target, ast.Name) and base(value) == "Context":
                names.add(target.id)
    return names


def _is_context_name(annotation: ast.expr | None) -> bool:
    if isinstance(annotation, ast.Subscript):  # Context[AppState, Any]
        annotation = annotation.value
    return _annotation_base_name(annotation) in _CONTEXT_NAMES


def _is_context_param(arg: ast.arg) -> bool:
    """A `Context`-typed parameter (`ctx: Context`, `mcp.server.fastmcp.Context`,
    `Context | None`, `Optional[Context]`) is injected by FastMCP at call time
    and stripped from the tool's exposed schema before it's ever built — same
    treatment as `self`/`cls`, not something an agent ever sees or documents.
    Name-based, like everywhere else here: doesn't verify the annotation
    actually resolves to fastmcp's Context class."""
    annotation = arg.annotation
    if _is_context_name(annotation):
        return True
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _is_context_name(annotation.left) or _is_context_name(annotation.right)
    if isinstance(annotation, ast.Subscript):
        base_name = _annotation_base_name(annotation.value)
        if base_name == "Optional":
            return _is_context_name(annotation.slice)
        if base_name == "Union":
            elts = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
            return any(_is_context_name(e) for e in elts)
    return False


def _param_documented_via_field(
    arg: ast.arg,
    default: ast.expr | None,
    alias_registry: dict[str, bool] | None = None,
    plain_string_ok: bool = True,
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
            if _annotated_elts_have_description(elts, alias_registry, plain_string_ok):
                return True
    return _field_call_has_description(default) if default is not None else False


def _has_plain_string_annotated_doc(arg: ast.arg) -> bool:
    annotation = arg.annotation
    if isinstance(annotation, ast.Subscript) and _annotation_base_name(annotation.value) == "Annotated":
        sl = annotation.slice
        elts = sl.elts if isinstance(sl, ast.Tuple) else [sl]
        return any(i > 0 and _is_plain_string_metadata(e) for i, e in enumerate(elts))
    return False


_OFFICIAL_SDK_SERVER_MODULES = ("mcp.server.fastmcp", "mcp.server.mcpserver")


def _plain_string_docs_reach_model(trees: list[tuple[str, ast.Module]]) -> bool:
    """Whether `Annotated[T, "text"]` becomes the parameter's description in
    the schema the model sees. Verified 2026-09-29 by listing a tool's
    inputSchema: standalone `fastmcp` 3.4.7 keeps the string as the
    description; the official SDK drops it in both 1.30 (`mcp.server.fastmcp`)
    and 2.1 (`mcp.server.mcpserver`), since pydantic ignores bare-string
    metadata. Hand-rolled frameworks that read it themselves exist too
    (mrexodia/ida-pro-mcp's zeromcp), so only a repo that uses the official
    SDK and never imports standalone fastmcp counts as dropping it. Repo-wide
    rather than per-file, because tool modules usually import the server
    instance from another file instead of the SDK directly."""
    official = standalone = False
    for _, tree in trees:
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                names = {a.name for a in n.names}
                if n.module == "fastmcp" or n.module.startswith("fastmcp."):
                    standalone = True
                elif n.module.startswith(_OFFICIAL_SDK_SERVER_MODULES) or (
                    n.module == "mcp.server" and names & {"FastMCP", "MCPServer", "fastmcp", "mcpserver"}
                ):
                    official = True
            elif isinstance(n, ast.Import):
                for a in n.names:
                    if a.name == "fastmcp" or a.name.startswith("fastmcp."):
                        standalone = True
                    elif a.name.startswith(_OFFICIAL_SDK_SERVER_MODULES):
                        official = True
    return standalone or not official


def _is_url_param_name(name: str) -> bool:
    """Name-based, like every other heuristic in this module: `url`/`icon_url`/
    `target_url` match (the token appears between underscores or at either
    end), `curl`/`hourly` don't (the token has to be a whole underscore-
    separated component, not a substring)."""
    tokens = name.lower().split("_")
    return "url" in tokens or "uri" in tokens


def _annotation_is_str_like(annotation: ast.expr | None) -> bool:
    """Whether a parameter's annotation resolves to `str`, unwrapping
    `str | None`, `Optional[str]`, `Union[str, ...]`, and `Annotated[str, ...]`
    — the same shapes `_annotation_is_str_like`'s callers care about, since a
    URL-shaped schema hint only makes sense on a string field."""
    if annotation is None:
        return False
    if isinstance(annotation, ast.Name):
        return annotation.id == "str"
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _annotation_is_str_like(annotation.left) or _annotation_is_str_like(annotation.right)
    if isinstance(annotation, ast.Subscript):
        base_name = _annotation_base_name(annotation.value)
        if base_name == "Optional":
            return _annotation_is_str_like(annotation.slice)
        if base_name == "Union":
            elts = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
            return any(_annotation_is_str_like(e) for e in elts)
        if base_name == "Annotated":
            sl = annotation.slice
            elts = sl.elts if isinstance(sl, ast.Tuple) else [sl]
            return bool(elts) and _annotation_is_str_like(elts[0])
    return False


def _dict_has_str_key(d: ast.Dict, key: str) -> bool:
    return any(isinstance(k, ast.Constant) and k.value == key for k in d.keys)


def _field_call_has_format_hint(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)
    if name != "Field":
        return False
    if _kwarg_present(node, "format"):
        return True
    extra = _kwarg_call(node, "json_schema_extra")
    # json_schema_extra is normally a dict literal (`{"format": "uri"}`), not
    # a call — _kwarg_call only matches ast.Call, so fetch it separately here
    # rather than reusing that helper for a shape it wasn't built for.
    for kw in node.keywords:
        if kw.arg == "json_schema_extra" and isinstance(kw.value, ast.Dict):
            if _dict_has_str_key(kw.value, "format"):
                return True
    return False


def _param_has_url_format_hint(arg: ast.arg, default: ast.expr | None) -> bool:
    """Whether a URL-shaped parameter declares a `format` schema hint —
    `Annotated[str, Field(format="uri")]`, `Field(json_schema_extra={"format": "uri"})`,
    or the `x: str = Field(...)` default-value spelling of either. Doesn't
    resolve a shared type-alias the way `_param_documented_via_field` does for
    descriptions — a repo-wide `UrlField = Annotated[str, Field(format="uri")]`
    alias would currently still be flagged. Known limitation, not a silent gap."""
    annotation = arg.annotation
    if isinstance(annotation, ast.Subscript):
        base = annotation.value
        base_name = base.attr if isinstance(base, ast.Attribute) else (base.id if isinstance(base, ast.Name) else None)
        if base_name == "Annotated":
            sl = annotation.slice
            elts = sl.elts if isinstance(sl, ast.Tuple) else [sl]
            if any(_field_call_has_format_hint(e) for e in elts[1:]):
                return True
    return _field_call_has_format_hint(default) if default is not None else False


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


def _build_mount_namespace_map(trees: list[tuple[str, ast.Module]]) -> dict[str, str]:
    """Repo-wide map of file -> namespace prefix, for FastMCP's `main.mount(sub,
    namespace="ns")` pattern (a documented way to compose several sub-servers
    into one — e.g. mcp-atlassian mounts separate jira/confluence FastMCP
    instances this way). Mounting with a namespace renames every tool on the
    mounted server to `ns_toolname` at the protocol level, so two same-named
    tools defined in two mounted sub-servers (e.g. both define `search`) are
    NOT actually duplicates — they're `jira_search`/`confluence_search` to a
    real client. Without this, the tool_name uniqueness check produces a false
    positive on a repo doing nothing wrong.

    Resolution is name-based only, same simplification as the alias/error-
    handling registries above: the mount call's target must be a simple name
    (`X.mount(sub_mcp, namespace=...)`, not an inline expression), and that
    name must be assigned via exactly one `name = SomeCall(...)` across the
    whole repo — ambiguous or unresolvable cases are dropped rather than
    guessed, consistent with this file's existing "don't guess" standard."""
    mount_targets: list[tuple[str, str]] = []
    for _, tree in trees:
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "mount"):
                continue
            if not node.args or not isinstance(node.args[0], ast.Name):
                continue
            namespace = _kwarg_str(node, "namespace") or _kwarg_str(node, "prefix")
            if namespace:
                mount_targets.append((node.args[0].id, namespace))

    if not mount_targets:
        return {}

    var_files: dict[str, set[str]] = {}
    for rel, tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        var_files.setdefault(tgt.id, set()).add(rel)

    result: dict[str, str] = {}
    ambiguous_files: set[str] = set()
    for var_name, namespace in mount_targets:
        files = var_files.get(var_name)
        if not files or len(files) != 1:
            continue
        (owning_file,) = files
        if owning_file in result and result[owning_file] != namespace:
            ambiguous_files.add(owning_file)
            continue
        result[owning_file] = namespace
    for f in ambiguous_files:
        result.pop(f, None)
    return result


_SERVER_START_METHODS = {"run", "serve", "stdio"}


def _find_standalone_entrypoint_files(
    trees: list[tuple[str, ast.Module]], mounted_files: set[str]
) -> set[str]:
    """Files with their own `if __name__ == "__main__": x.run()` block on a
    locally-defined instance — a real, independently-runnable server, found
    dogfooding a monorepo (`jingcheng-chen/rhinomcp`) that ships one real
    server plus dozens of scratch/experimental servers under `experiments/`,
    several of which happen to redeclare the same tool name (`create_object`,
    `analyze_objects`, ...) as the real one for local testing. The spec's
    'SHOULD be unique within a server' doesn't apply across two servers that
    can never both be running at once, so tools in these files are excluded
    from the tool-name-uniqueness comparison against every *other* such file
    (each remains its own group; genuine duplicates within one standalone
    file, or within the shared default group, are still caught).

    The `__main__` block may also start the server through a function defined in the same file
    (`if __name__ == "__main__": main()`), found dogfooding `mrexodia/ida-pro-mcp`, whose
    supervisor and worker servers are separate processes that both expose `idb_open`. A file
    whose instance another file imports is never standalone: tools registered on it from other
    modules share its server, so a name declared in both is a real duplicate.

    Conservative like the mount-namespace map above: a file already known to
    be a mount *target* is never treated as standalone, even if it also has
    its own `__main__` guard for standalone testing — evidence it's meant to
    be composed into another server wins over evidence it can run alone."""
    # Instances other files import (`from pkg.server import mcp`) are shared: tools registered on
    # them from elsewhere belong to the same server, so their file is never standalone.
    imported_names: set[tuple[str, str]] = set()
    for _, tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                stem = node.module.rsplit(".", 1)[-1]
                imported_names.update((stem, a.name) for a in node.names)

    result: set[str] = set()
    for rel, tree in trees:
        if rel in mounted_files:
            continue
        stem = Path(rel).stem
        local_call_vars = {
            tgt.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
            for tgt in node.targets
            if isinstance(tgt, ast.Name) and (stem, tgt.id) not in imported_names
        }
        local_funcs = {
            node.name: node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        def runs_local_server(scope: ast.AST) -> bool:
            return any(
                isinstance(stmt, ast.Call)
                and isinstance(stmt.func, ast.Attribute)
                and stmt.func.attr in _SERVER_START_METHODS
                and isinstance(stmt.func.value, ast.Name)
                and stmt.func.value.id in local_call_vars
                for stmt in ast.walk(scope)
            )

        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "__name__"
                and any(
                    isinstance(c, ast.Constant) and c.value == "__main__"
                    for c in node.test.comparators
                )
            ):
                continue
            # `if __name__ == "__main__": main()`, where main() starts the server, is at least as
            # common as calling run() inline (mrexodia/ida-pro-mcp's supervisor, 12k stars).
            called = {
                c.func.id for c in ast.walk(node)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in local_funcs
            }
            if runs_local_server(node) or any(runs_local_server(local_funcs[f]) for f in called):
                result.add(rel)
                break
    return result


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


def _kwarg_call(call: ast.Call, key: str) -> ast.Call | None:
    for kw in call.keywords:
        if kw.arg == key and isinstance(kw.value, ast.Call):
            return kw.value
    return None


def _kwarg_bool(call: ast.Call, *keys: str) -> bool | None:
    """Reads a boolean keyword argument, trying each of `keys` in turn — for
    reading either the snake_case or camelCase spelling of the same
    ToolAnnotations field without the caller needing to know which one a
    given SDK version uses."""
    for kw in call.keywords:
        if kw.arg in keys and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, bool):
            return kw.value.value
    return None


def _kwarg_present(call: ast.Call, *keys: str) -> bool:
    """Whether any of `keys` was passed at all, regardless of its value —
    for a field like `destructiveHint` whose *presence* is the thing that
    matters (the spec defines it as meaningful only when `readOnlyHint` is
    false, not any particular value of it)."""
    return any(kw.arg in keys for kw in call.keywords)


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

    # Bounded breadth-first search per function: handled if it, or anything it
    # reaches within 5 calls, has its own try/except. An earlier memoized
    # recursion cached a provisional False for a function on a call cycle (and
    # results cut short by the depth limit), so the answer depended on which
    # end of the cycle was resolved first: set iteration order, which changes
    # with the hash seed. Found as run-to-run flips on oraios/serena.
    own = {name: _contains_try_except(node)[0] for name, node in functions.items()}
    callees = {name: _direct_call_names(node, import_aliases) for name, node in functions.items()}
    memo: dict[str, bool] = {}
    for name in functions:
        seen, frontier, found = {name}, [name], own[name]
        for _ in range(5):
            if found or not frontier:
                break
            nxt = []
            for fn_name in frontier:
                for callee in callees[fn_name]:
                    if callee in functions and callee not in seen:
                        seen.add(callee)
                        nxt.append(callee)
            found = any(own[c] for c in nxt)
            frontier = nxt
        memo[name] = found
    # A tool calling the alias directly (e.g. `_compare_strategies()` for a
    # function actually `def`-ed as `compare_strategies`) should still hit —
    # make the registry itself alias-aware rather than requiring every call
    # site to resolve through import_aliases too.
    for alias_name, real_name in import_aliases.items():
        if real_name in memo:
            memo.setdefault(alias_name, memo[real_name])
    return memo


# A tool declaring `readOnlyHint: true` is a real, load-bearing signal — an
# agent framework may gate approval prompts or safety checks on it (verified
# real-world case: DeusData/codebase-memory-mcp#2118, where 13 of 15 tools
# were mislabeled destructive/not-read-only, affecting exactly this kind of
# client-side gating). This looks for the opposite, more dangerous
# direction: a tool that claims to be read-only but whose own body contains
# an obvious write/mutation signature. Deliberately narrow and conservative
# — a raw SQL mutation verb, a file opened in a write mode, a filesystem
# deletion call, or a client library's mutating HTTP verb (scoped to a
# recognizable client name, since bare `.post(`/`.delete(` are common method
# names for plenty of non-HTTP things) — not `.save()`/`.commit()` alone,
# which are common enough on read-only code paths (an ORM's read-only
# session, a computed result saved to a local variable) to be more noise
# than signal. A hit here means "worth a human look", not a confirmed bug.
_MUTATION_SIGNALS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|DROP\s+TABLE|ALTER\s+TABLE)\b', re.IGNORECASE), "a raw SQL mutation statement"),
    (re.compile(r'open\([^)]*[\'"](w|a|wb|ab|w\+|a\+)[\'"]'), "a file opened in a write/append mode"),
    (re.compile(r'\b(os\.remove|os\.unlink|os\.rmdir|shutil\.rmtree)\('), "a filesystem deletion call"),
    (re.compile(r'\b(requests|httpx|client|session)\.(post|put|delete|patch)\('), "a mutating HTTP client call"),
]


def _scan_for_mutation_signal(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    try:
        body_text = ast.unparse(fn)
    except Exception:
        return None
    for pattern, label in _MUTATION_SIGNALS:
        if pattern.search(body_text):
            return label
    return None


def _analyze_function_as_tool(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    file: str,
    description_override: str | None = None,
    alias_registry: dict[str, bool] | None = None,
    name_override: str | None = None,
    excluded_arg_names: set[str] | None = None,
    error_handling_registry: dict[str, bool] | None = None,
    declared_read_only: bool | None = None,
    declared_destructive_present: bool = False,
    plain_string_ok: bool = True,
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
        a.arg for a in args
        if _param_documented_via_field(a, defaults_by_arg.get(a), alias_registry, plain_string_ok)
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
        # ...or be wrapped by a locally-defined decorator that does, e.g.
        # tskovlund/mcp-score's `@score_tool`, which turns BridgeError into an
        # actionable ToolError around every tool.
        if not has_try:
            has_try = any(
                error_handling_registry.get(_annotation_base_name(
                    d.func if isinstance(d, ast.Call) else d) or "", False)
                for d in fn.decorator_list
            )

    finding = ToolFinding(
        name=tool_name,
        file=file,
        line=fn.lineno,
        has_description=bool(description.strip()),
        description_len=len(description.strip()),
        description_display_width=description_display_width(description.strip()),
        param_count=len(args),
        typed_param_count=typed,
        has_docstring_params=documented_count >= len(args) and len(args) > 0,
        has_try_except=has_try,
        has_bare_except=has_bare,
        description_text=description,
        param_names=[a.arg for a in args],
        required_param_names=[a.arg for a in args if a not in defaults_by_arg],
    )

    if not finding.has_description:
        finding.issues.append(ToolIssue(
            tool_name, file, fn.lineno, "description",
            "Tool has no description. An agent cannot decide when to call this.",
            "error",
        ))
    elif finding.description_display_width < 10:
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
        dropped = [] if plain_string_ok else [
            a.arg for a in args if a.arg not in field_documented_names and _has_plain_string_annotated_doc(a)
        ]
        if dropped:
            message = (
                f"Parameter description(s) for {', '.join(dropped)} are written as Annotated[T, \"...\"], "
                "which the official MCP Python SDK drops from the schema (pydantic ignores bare-string "
                "metadata) — the model only sees names, not intent. Use Annotated[T, Field(description=...)] "
                "or an Args: docstring section instead."
            )
        else:
            message = (
                "Parameters aren't documented — no Args:/:param: docstring section and no per-parameter "
                "Field(description=...) — the model only sees names, not intent."
            )
        finding.issues.append(ToolIssue(tool_name, file, fn.lineno, "param_docs", message, "warning"))

    url_params_missing_format = [
        a.arg for a in args
        if _is_url_param_name(a.arg)
        and _annotation_is_str_like(a.annotation)
        and not _param_has_url_format_hint(a, defaults_by_arg.get(a))
    ]
    if url_params_missing_format:
        finding.issues.append(ToolIssue(
            tool_name, file, fn.lineno, "url_format_hint",
            f"{', '.join(url_params_missing_format)} looks like a URL parameter (by name) but "
            "has no `format: \"uri\"` schema hint — a schema-only client (a fuzzer, or a strict "
            "schema-driven agent) sees a bare string, not a URL. Add it via "
            "`Field(json_schema_extra={\"format\": \"uri\"})`. Name-based heuristic — worth a "
            "human look, not confirmed.",
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

    if declared_read_only:
        mutation_signal = _scan_for_mutation_signal(fn)
        if mutation_signal:
            finding.issues.append(ToolIssue(
                tool_name, file, fn.lineno, "annotation_mismatch",
                f"Declared readOnlyHint: true, but this tool's own body contains {mutation_signal} — "
                "an agent framework that gates approval prompts on this hint may skip confirming an "
                "action that isn't actually read-only. Heuristic — worth a human look, not confirmed.",
                "warning", "security",
            ))
        if declared_destructive_present:
            finding.issues.append(ToolIssue(
                tool_name, file, fn.lineno, "annotation_contradiction",
                "Declared readOnlyHint: true and also set destructiveHint — the spec defines "
                "destructiveHint as meaningful only when readOnlyHint is false, so this combination "
                "is self-contradictory and destructiveHint's value here has no defined meaning. "
                "Common when a new tool is cloned from an existing write-tool's annotation block and "
                "only readOnlyHint gets flipped to true.",
                "warning", "security",
            ))

    return finding


def _find_fastmcp_tools(
    tree: ast.Module,
    file: str,
    alias_registry: dict[str, bool] | None = None,
    error_handling_registry: dict[str, bool] | None = None,
    plain_string_ok: bool = True,
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
                    node, file, alias_registry=alias_registry, error_handling_registry=error_handling_registry,
                    plain_string_ok=plain_string_ok,
                ))
                break
            description_override = _kwarg_str(call, "description")
            name_override = _kwarg_str(call, "name")
            excluded_args = _kwarg_str_list(call, "exclude_args")
            annotations_call = _kwarg_call(call, "annotations")
            declared_read_only = (
                _kwarg_bool(annotations_call, "read_only_hint", "readOnlyHint")
                if annotations_call is not None else None
            )
            declared_destructive_present = (
                _kwarg_present(annotations_call, "destructive_hint", "destructiveHint")
                if annotations_call is not None else False
            )
            findings.append(_analyze_function_as_tool(
                node, file, description_override, alias_registry, name_override,
                excluded_args, error_handling_registry, declared_read_only,
                declared_destructive_present, plain_string_ok,
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
        description_display_width=description_display_width(description.strip()),
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
    elif finding.description_display_width < 10:
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


FuncDef = Union[ast.FunctionDef, ast.AsyncFunctionDef]


def _imports_fastmcp(tree: ast.Module) -> bool:
    """Whether a file imports FastMCP, standalone or the official SDK's copy."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            if node.module == "fastmcp" or node.module.startswith(("fastmcp.", "mcp.server.fastmcp", "mcp.server.mcpserver")):
                # mcp.server.mcpserver: the SDK v2 name for FastMCP (MCPServer)
                return True
            if node.module == "mcp.server" and any(a.name in ("FastMCP", "MCPServer") for a in node.names):
                return True
        elif isinstance(node, ast.Import) and any(a.name.split(".")[0] == "fastmcp" for a in node.names):
            return True
    return False


class _RepoFunctions:
    """Repo-wide lookup for a function passed by reference to a registration
    call (`mcp.tool(fn)`, `mcp.add_tool(fn)`), when `fn` is defined in
    another module — e.g. `lens-finance/mcp`, whose `app.py` imports every
    tool from `mcp_server.tools.*` and registers each with `app.add_tool`.
    Only top-level functions of a module, and only a module path that maps
    to exactly one file; anything else stays unresolved."""

    def __init__(self, trees: list[tuple[str, ast.Module]]):
        self.top_level: dict[str, dict[str, FuncDef]] = {}
        self.by_dotted: dict[str, list[str]] = {}
        for rel, tree in trees:
            self.top_level[rel] = {
                n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            parts = list(Path(rel).with_suffix("").parts)
            if parts and parts[-1] == "__init__":
                parts = parts[:-1]
            for i in range(len(parts)):
                self.by_dotted.setdefault(".".join(parts[i:]), []).append(rel)

    def module_file(self, importer: str, module: str | None, level: int) -> str | None:
        if level:
            base = list(Path(importer).parent.parts)
            if level > 1:
                base = base[: len(base) - (level - 1)]
            dotted = ".".join(base + (module.split(".") if module else []))
            matches = [r for r in self.by_dotted.get(dotted, []) if ".".join(Path(r).with_suffix("").parts) in (dotted, dotted + ".__init__")]
        else:
            matches = self.by_dotted.get(module or "", [])
        return matches[0] if len(matches) == 1 else None

    def function(self, importer: str, module: str | None, level: int, name: str) -> tuple[FuncDef, str] | None:
        target = self.module_file(importer, module, level)
        if target is None:
            return None
        fn = self.top_level.get(target, {}).get(name)
        return (fn, target) if fn is not None else None


def _import_bindings(tree: ast.Module) -> dict[str, tuple[str | None, int, str | None]]:
    """Local name -> (module, level, imported name or None for a module)."""
    bindings: dict[str, tuple[str | None, int, str | None]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                bindings[a.asname or a.name] = (node.module, node.level, a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    bindings[a.asname] = (a.name, 0, None)
    return bindings


def _enclosing_class(tree: ast.Module, node: ast.AST) -> ast.ClassDef | None:
    best = None
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef):
            end = getattr(n, "end_lineno", None) or n.lineno
            if n.lineno <= node.lineno <= end and (best is None or n.lineno > best.lineno):
                best = n
    return best


def _resolve_function_ref(
    ref: ast.expr, site: ast.AST, tree: ast.Module, file: str, repo: _RepoFunctions | None,
) -> tuple[FuncDef, str] | None:
    """The function definition a registration call's argument refers to, and
    the file it lives in: a name in the enclosing scope or at module level, a
    name imported from another module in the repo, `self.method` on the
    enclosing class, or `module.func` through an imported module."""
    if isinstance(ref, ast.Name):
        fn = _resolve_direct_call_function(ref.id, _enclosing_scope(tree, site))
        if fn is None:
            fn = _resolve_direct_call_function(ref.id, tree)
        if fn is not None:
            return fn, file
        binding = _import_bindings(tree).get(ref.id)
        if repo is not None and binding and binding[2] is not None:
            module, level, name = binding
            return repo.function(file, module, level, name)
        return None
    if isinstance(ref, ast.Attribute) and isinstance(ref.value, ast.Name):
        if ref.value.id == "self":
            cls = _enclosing_class(tree, site)
            if cls is None:
                return None
            for n in cls.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == ref.attr:
                    return n, file
            return None
        binding = _import_bindings(tree).get(ref.value.id)
        if repo is not None and binding:
            module, level, name = binding
            dotted = module if name is None else ".".join(p for p in (module, name) if p)
            return repo.function(file, dotted, level if name is not None else 0, ref.attr)
    return None


def _fastmcp_tool_classes(tree: ast.Module) -> set[str]:
    """Local names bound to a FastMCP tool class (`Tool`, `FunctionTool`),
    so `Tool.from_function` isn't confused with LangChain's
    `Tool.from_function`/`StructuredTool.from_function`."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level and (
            node.module == "fastmcp" or node.module.startswith(("fastmcp.", "mcp.server.fastmcp"))
        ):
            names.update(a.asname or a.name for a in node.names if a.name.endswith("Tool"))
    return names


def _is_tool_from_function(node: ast.Call, tool_classes: set[str]) -> bool:
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "from_function"):
        return False
    owner = func.value
    if isinstance(owner, ast.Name):
        return owner.id in tool_classes
    root = owner
    while isinstance(root, ast.Attribute):
        root = root.value
    return isinstance(owner, ast.Attribute) and owner.attr.endswith("Tool") and isinstance(root, ast.Name) and root.id == "fastmcp"


def _receiver_name(node: ast.Call) -> str:
    """`mcp` for `mcp.tool(fn)` / `self.mcp.add_tool(fn)` / `mcp.tool()(fn)`."""
    func = node.func.func if isinstance(node.func, ast.Call) else node.func
    value = func.value if isinstance(func, ast.Attribute) else None
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return ""


def _registration_sites(node: ast.Call, tool_classes: set[str]) -> tuple[ast.expr, ast.Call] | None:
    """(function reference, call carrying name=/description= kwargs) for the
    by-reference registration forms FastMCP documents besides the
    decorator: `mcp.tool(fn)`, `mcp.tool(...)(fn)` (the decorator applied
    by hand, e.g. `mcp.tool()(self.get_regions)` in
    openstack-kr/python-openstackmcp-server), `mcp.add_tool(fn)`, and
    `mcp.add_tool(Tool.from_function(fn, ...))`."""
    func = node.func
    if _is_tool_from_function(node, tool_classes):
        # `Tool.from_function(fn, name=...)` defines a tool wherever it's
        # registered: inside add_tool, or collected into a list and added in
        # a loop (fancyboi999/daily-hot-mcp, 31 tools). Counted here, once.
        ref = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg == "fn"), None)
        return (ref, node) if ref is not None else None
    if not node.args:
        return None
    ref = node.args[0]
    if isinstance(func, ast.Call) and isinstance(func.func, ast.Attribute) and func.func.attr in FASTMCP_DECORATOR_NAMES:
        return ref, func
    if isinstance(func, ast.Attribute) and func.attr == "add_tool":
        if isinstance(ref, ast.Call) and _is_tool_from_function(ref, tool_classes):
            return None
        return ref, node
    if isinstance(func, ast.Attribute) and func.attr in FASTMCP_DECORATOR_NAMES:
        return ref, node
    return None


def _find_direct_call_tools(
    tree: ast.Module,
    file: str,
    alias_registry: dict[str, bool] | None = None,
    error_handling_registry: dict[str, bool] | None = None,
    plain_string_ok: bool = True,
    repo: _RepoFunctions | None = None,
    imports_fastmcp: bool = True,
    repo_uses_fastmcp: bool = False,
) -> list[ToolFinding]:
    """FastMCP's `.tool()` also supports a direct call form — the function
    passed as a positional argument rather than used as a decorator:
    `provider.tool(some_func, name="...", description="...")`, confirmed
    directly against FastMCP's own docstring for this method ("direct
    function call" is one of its documented calling patterns) — and real on
    `qdrant/mcp-server-qdrant`, the official Qdrant MCP server, where both of
    its tools are registered this way and neither was detected before this.

    The other by-reference forms (see `_registration_sites`) only count in a
    file that imports FastMCP, since agent frameworks (pydantic-ai, others)
    have their own `.tool(fn)`/`.add_tool(fn)`. Without a literal `name=`, a
    tool is only reported when its function resolves (the name is then the
    function's own, as FastMCP derives it); see `_resolve_function_ref`."""
    findings = []
    tool_classes = _fastmcp_tool_classes(tree)
    # `for tool in (a, b, c): server.tool()(tool)` (tskovlund/mcp-score): a
    # loop variable over a literal tuple/list of function names stands for
    # each of them. Anything else a loop iterates is built at runtime, skipped.
    loop_refs: dict[int, tuple[str, list[ast.expr]]] = {}
    for loop in ast.walk(tree):
        if (isinstance(loop, (ast.For, ast.AsyncFor)) and isinstance(loop.target, ast.Name)
                and isinstance(loop.iter, (ast.Tuple, ast.List))
                and loop.iter.elts and all(isinstance(e, (ast.Name, ast.Attribute)) for e in loop.iter.elts)):
            for stmt in loop.body:
                for inner in ast.walk(stmt):
                    if isinstance(inner, ast.Call):
                        loop_refs[id(inner)] = (loop.target.id, list(loop.iter.elts))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        site = _registration_sites(node, tool_classes)
        if site is None:
            continue
        ref, kw_call = site
        name_override = _kwarg_str(kw_call, "name")
        legacy = (
            kw_call is node and isinstance(node.func, ast.Attribute)
            and node.func.attr in FASTMCP_DECORATOR_NAMES and isinstance(ref, ast.Name)
            and name_override is not None
        )
        # A module that registers onto a server passed in as a parameter
        # often doesn't import FastMCP itself (Raudbjorn/MDMAI:
        # `mcp_server.tool()(start_session)`); accepted when the repo uses
        # FastMCP and the receiver is named like a server.
        receiver_ok = repo_uses_fastmcp and re.search(r"mcp|server", _receiver_name(node), re.I)
        if not (legacy or _is_tool_from_function(kw_call, tool_classes)
                or imports_fastmcp or receiver_ok):
            continue
        if not isinstance(ref, (ast.Name, ast.Attribute)):
            continue
        description_override = _kwarg_str(kw_call, "description") or ""
        refs = [ref]
        loop = loop_refs.get(id(node))
        if loop is not None and isinstance(ref, ast.Name) and ref.id == loop[0]:
            if name_override is not None:
                continue  # one literal name for several functions: ambiguous
            refs = loop[1]
        for one_ref in refs:
            resolved = _resolve_function_ref(one_ref, node, tree, file, repo)
            if resolved is not None:
                fn_node, fn_file = resolved
                excluded_args = _kwarg_str_list(kw_call, "exclude_args")
                findings.append(_analyze_function_as_tool(
                    fn_node, fn_file, description_override or None, alias_registry, name_override,
                    excluded_args, error_handling_registry, plain_string_ok=plain_string_ok,
                ))
            elif name_override is not None:
                findings.append(_bare_direct_call_finding(name_override, description_override, file, node.lineno))
    return findings


def _collect_property_builder_funcs(tree: ast.Module) -> dict[str, ast.Dict]:
    """Same-file registry of zero-arg helper functions shaped like
    `def prop() -> ...: return {...}` — a way to share one JSON-schema
    property (or a small group of them, spread with `**`) across several
    tools' `inputSchema` rather than repeating the dict literal inline —
    verified against `blazickjp/arxiv-mcp-server`'s `_paper_id_property`/
    `_page_properties` helpers. Maps the function's name to its single
    returned dict literal; a function with any parameter, or whose body
    isn't exactly one `return {...}`, is left out — nothing to safely
    resolve without evaluating it."""
    registry: dict[str, ast.Dict] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        a = node.args
        if a.args or a.vararg or a.kwonlyargs or a.kwarg or a.posonlyargs:
            continue
        returns = [n for n in node.body if isinstance(n, ast.Return)]
        if len(returns) != 1 or not isinstance(returns[0].value, ast.Dict):
            continue
        registry[node.name] = returns[0].value
    return registry


def _resolve_property_dict(node: ast.expr | None, builders: dict[str, ast.Dict]) -> ast.Dict | None:
    """Resolves an inline dict literal directly, or a call to a known
    zero-arg property-builder function (`_collect_property_builder_funcs`)
    to its returned dict literal. A call to anything else — an unknown or
    parameterized function, a conditional, ... — is left unresolved rather
    than guessed at."""
    if isinstance(node, ast.Dict):
        return node
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.args and not node.keywords:
        return builders.get(node.func.id)
    return None


def _property_has_desc(pv: ast.Dict) -> bool:
    return any(isinstance(k, ast.Constant) and k.value == "description" for k in pv.keys)


def _find_lowlevel_tools(tree: ast.Module, file: str) -> list[ToolFinding]:
    """Find Tool(name=..., description=..., inputSchema=...) constructor calls."""
    findings = []
    property_builders = _collect_property_builder_funcs(tree)
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
        param_names: list[str] = []
        required_param_names: list[str] = []
        if schema_kw is not None and isinstance(schema_kw.value, ast.Dict):
            for k, v in zip(schema_kw.value.keys, schema_kw.value.values):
                if isinstance(k, ast.Constant) and k.value == "required" and isinstance(v, ast.List):
                    required_param_names = [e.value for e in v.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                    continue
                if not (isinstance(k, ast.Constant) and k.value == "properties" and isinstance(v, ast.Dict)):
                    continue
                for pk, pv in zip(v.keys, v.values):
                    if pk is None:
                        # A `**spread_call()` entry — resolve and count each of
                        # its own properties individually, rather than as one
                        # opaque, always-undocumented property.
                        spread = _resolve_property_dict(pv, property_builders)
                        if spread is None:
                            param_count += 1
                            continue
                        for spk, spv in zip(spread.keys, spread.values):
                            param_count += 1
                            if isinstance(spk, ast.Constant) and isinstance(spk.value, str):
                                param_names.append(spk.value)
                            if isinstance(spv, ast.Dict) and _property_has_desc(spv):
                                typed_param_count += 1
                        continue
                    param_count += 1
                    if isinstance(pk, ast.Constant) and isinstance(pk.value, str):
                        param_names.append(pk.value)
                    resolved = _resolve_property_dict(pv, property_builders)
                    if resolved is not None and _property_has_desc(resolved):
                        typed_param_count += 1

        # required_param_names is only trustworthy when every property name was
        # itself resolved (a dynamic/unresolved property, or an unresolved
        # `**spread_call()`, means param_names is incomplete) — otherwise a
        # required name from an unresolved property wouldn't have a matching
        # entry in param_names, which schema_diff.py would misread as "newly
        # required" rather than "not statically visible".
        param_names_complete = len(param_names) == param_count
        if not param_names_complete:
            required_param_names = []

        finding = ToolFinding(
            name=name,
            file=file,
            line=node.lineno,
            has_description=bool(description.strip()),
            description_len=len(description.strip()),
            description_display_width=description_display_width(description.strip()),
            param_count=param_count,
            typed_param_count=typed_param_count,
            has_docstring_params=typed_param_count >= param_count and param_count > 0,
            has_try_except=True,  # not attributable to a single function body here
            has_bare_except=False,
            description_text=description,
            param_names=param_names,
            required_param_names=required_param_names,
            param_names_complete=param_names_complete,
        )
        if not finding.has_description:
            finding.issues.append(ToolIssue(
                name, file, node.lineno, "description",
                "Tool has no description. An agent cannot decide when to call this.",
                "error",
            ))
        elif finding.description_display_width < 10:
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
            # None (not "") when there's no class docstring, so
            # _analyze_function_as_tool falls back to apply()'s own docstring
            # instead of reporting "no description" — serena's own tools_base.py
            # documents apply()'s docstring, not the class's, as what's actually
            # shown to the model (get_apply_docstring_from_cls), and several real
            # tool classes (e.g. SearchForPatternTool, SafeDeleteSymbol) only ever
            # docstring the apply method, never the class itself.
            description_override=class_doc.strip() if class_doc else None,
            alias_registry=alias_registry,
            name_override=_tool_name_from_class_name(node.name),
            error_handling_registry=error_handling_registry,
        ))
    return findings


_JS_TEST_SUFFIXES = (".test.ts", ".test.tsx", ".test.js", ".test.jsx", ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx")


# Excludes files that can never be part of a tool's reachable execution
# path, so quality/tool-finding never registers a "tool" defined in one and
# the security scans never flag a call inside one as if a model argument
# could reach it. Started as test-file exclusion only; broadened to a
# `scripts/` path component after a real false positive: arxiv-mcp-server's
# scripts/smoke_installed_wheel.py builds a venv and installs a wheel via
# fixed, non-tool-derived subprocess.run() calls purely to smoke-test a
# release — flagged by the dangerous-exec check exactly like tool code
# would be. Breadth-checked before broadening: a `scripts/` directory full
# of release/build subprocess calls unrelated to any tool is a common
# convention repo-wide (aws/chalice, colobot/colobot, diem/diem and others
# all follow it), not a one-off. Deliberately narrow to `scripts/` — not
# `tools/`, which in some repos is genuinely where tool implementations
# live.
# Broadened again to `benchmarks/` after a second real false positive:
# MinishLab/semble's benchmarks/ and benchmarks/baselines/ shell out to
# competing CLI tools (ripgrep-style baselines) purely to compare
# performance — 21 of 22 dangerous-exec flags on that repo were in these
# directories, none of them reachable from either of its 2 real MCP tools.
# Breadth-checked the same way as `scripts/`: a top-level `benchmarks/`
# directory full of subprocess calls unrelated to any tool is a common,
# independently-authored convention (pypa/pipenv, pytorch/xla,
# facebookexperimental/hermit and others all follow it), not a one-off.
# A repo can genuinely contain the same source file at two paths — most
# commonly a vendored/bundled copy shipped alongside the original for
# packaging (e.g. a Blender addon's root-level addon.py also copied into
# src/*/bundled/ so it can be zipped into the distributable plugin).
# Scanning both copies independently double-counts every finding in that
# file: the same eval/exec call, the same tool, the same SSRF hit, each
# reported twice under different paths. Worse, each repo-level security
# check's score penalty is capped by *occurrence count*
# (SECURITY_CHECK_CAP), so a duplicated file can push a single real finding
# over the cap and inflate the penalty, or — for tools — duplicate a clean
# tool's positive contribution to the quality score. Keeping only the first
# occurrence of each byte-identical file (by content hash, first-seen order)
# fixes both directions without needing to know *why* the duplicate exists.
def _dedupe_by_content(files: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result = []
    for f in files:
        try:
            digest = hashlib.sha256(f.read_bytes()).hexdigest()
        except OSError:
            result.append(f)
            continue
        if digest in seen:
            continue
        seen.add(digest)
        result.append(f)
    return result


_PY_TEST_CONTENT = re.compile(
    r"^\s*(?:async\s+)?def test_|^class Test|^\s*(?:import|from)\s+(?:pytest|unittest)\b", re.M
)
_PY_MCP_IMPORT = re.compile(r"^\s*(?:import|from)\s+(?:mcp|fastmcp)\b", re.M)


def _is_auxiliary_file(path: Path) -> bool:
    name = path.name
    if name.endswith("_test.go"):
        return True  # enforced by the Go toolchain
    if name.startswith("test_") or name.endswith("_test.py"):
        # Only a naming convention in Python: oaslananka/kicad-mcp-pro keeps 4
        # real tools in tools/test_points.py (test points on a PCB). Outside a
        # test folder, such a file is source when it imports an MCP server
        # library and holds no tests; a test harness script without pytest
        # functions (pal-mcp-server's communication_simulator_test.py) still isn't.
        if any(part in ("test", "tests") for part in path.parts):
            return True
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            return True
        return not (_PY_MCP_IMPORT.search(text) and not _PY_TEST_CONTENT.search(text))
    if name.endswith(_JS_TEST_SUFFIXES):
        return True
    return any(part in ("test", "tests", "scripts", "benchmarks") for part in path.parts)


def _scan_secrets(py_files: list[Path]) -> list[RepoIssue]:
    issues = []
    for f in py_files:
        if _is_auxiliary_file(f):
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


# Directories that hold installed or generated code, not the repo's own source.
# Scanning them reports third-party code (pip, typing_extensions, ...) as the
# server's own findings, and can hide a missing-tests warning.
_NON_REPO_DIRS = {".git", "venv", ".venv", "node_modules", "site-packages", ".tox", ".nox", "__pycache__"}


def analyze_repo(root: Path) -> Report:
    # Lazy import: ts_analyzer/go_analyzer/security import ToolFinding/ToolIssue
    # from this module, so importing them at module load time would be circular.
    from .go_analyzer import find_go_tools
    from .security import (
        scan_dangerous_exec,
        scan_prompt_injection,
        scan_ssrf,
        scan_unpinned_dependencies,
        scan_unsafe_deserialization,
    )
    from .ts_analyzer import find_ts_tools

    def in_repo(p: Path) -> bool:
        # Only look at the path *inside* the scanned repo, so a repo that itself
        # lives under a folder named venv/ isn't skipped wholesale.
        return not (set(p.relative_to(root).parts[:-1]) & _NON_REPO_DIRS)

    # is_file drops a broken symlink (jiangyi01/SpatialOmicsLab), which crashed the read.
    # sorted: rglob order differs across Python versions (3.13 changed it) and
    # filesystems, and the first of two identical files is the one kept.
    py_files = _dedupe_by_content(sorted(p for p in root.rglob("*.py") if in_repo(p) and p.is_file()))

    trees: list[tuple[str, ast.Module]] = []
    unparseable: list[str] = []
    for f in py_files:
        if _is_auxiliary_file(f):
            continue
        try:
            # utf-8-sig: Python runs a file with a leading byte-order mark, but
            # ast.parse on a str rejects U+FEFF.
            tree = ast.parse(f.read_text(encoding="utf-8-sig", errors="ignore"), filename=str(f))
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

    global _CONTEXT_NAMES
    _CONTEXT_NAMES = _collect_context_aliases(trees)
    error_handling_registry = _build_error_handling_registry(trees, _build_import_aliases(trees))

    plain_string_ok = _plain_string_docs_reach_model(trees)

    repo_functions = _RepoFunctions(trees)
    repo_uses_fastmcp = any(_imports_fastmcp(tree) for _, tree in trees)
    tools: list[ToolFinding] = []
    for rel, tree in trees:
        tools.extend(_find_fastmcp_tools(tree, rel, alias_registry, error_handling_registry, plain_string_ok))
        tools.extend(_find_direct_call_tools(
            tree, rel, alias_registry, error_handling_registry, plain_string_ok,
            repo=repo_functions, imports_fastmcp=_imports_fastmcp(tree), repo_uses_fastmcp=repo_uses_fastmcp,
        ))
        tools.extend(_find_lowlevel_tools(tree, rel))
        tools.extend(_find_class_based_tools(tree, rel, alias_registry, error_handling_registry))

    mount_namespace_map = _build_mount_namespace_map(trees)
    for t in tools:
        namespace = mount_namespace_map.get(t.file)
        if namespace:
            t.name = f"{namespace}_{t.name}"
            for issue in t.issues:
                issue.tool = t.name

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
    standalone_files = _find_standalone_entrypoint_files(trees, set(mount_namespace_map))
    seen_by_group: dict[str, dict[str, int]] = {}
    for t in tools:
        group = t.file if t.file in standalone_files else "__default__"
        bucket = seen_by_group.setdefault(group, {})
        bucket[t.name] = bucket.get(t.name, 0) + 1
    # Tools an agent can't tell apart by description (see lookalike.py for why this is narrow).
    from .lookalike import find_indistinguishable_tools, message as lookalike_message
    by_name = {t.name: t for t in tools}
    for a, b in find_indistinguishable_tools([(t.name, t.description_text) for t in tools]):
        for this, other in ((a, b), (b, a)):
            t = by_name[this]
            t.issues.append(ToolIssue(t.name, t.file, t.line, "indistinguishable_description",
                                      lookalike_message(other), "warning"))

    duplicate_names = sorted({
        n for bucket in seen_by_group.values() for n, count in bucket.items() if count > 1
    })
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

    # is_file: a directory like README_files/ (MaximeRivest/mcp2py) matches too.
    readme = next((p for p in root.glob("README*") if p.is_file()), None)
    readme_text = _readme_and_linked_docs_text(readme, root) if readme else ""
    if not readme:
        repo_issues.append(RepoIssue("readme", "No README found.", "error"))
    else:
        undocumented = [
            t.name for t in tools
            if t.name not in readme_text and not _DEPRECATED_RE.search(t.description_text)
        ]
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
        any(in_repo(p) for pat in ("test_*.py", "*_test.py", "*.test.ts", "*.spec.ts", "*_test.go") for p in root.rglob(pat))
        or (root / "tests").is_dir()
        or any(in_repo(p) for p in root.rglob("__tests__"))
    )
    if not has_tests:
        repo_issues.append(RepoIssue("tests", "No test files found.", "warning"))

    has_py_packaging = (root / "pyproject.toml").exists() or (root / "requirements.txt").exists() or (root / "setup.py").exists()
    has_js_packaging = (root / "package.json").exists()
    has_go_packaging = (root / "go.mod").exists()
    if not has_py_packaging and not has_js_packaging and not has_go_packaging:
        repo_issues.append(RepoIssue("packaging", "No pyproject.toml/requirements.txt/setup.py/package.json/go.mod — dependencies aren't pinned.", "warning"))
    else:
        repo_issues.extend(scan_unpinned_dependencies(root))

    ts_js_files = _dedupe_by_content(sorted(
        p for p in root.rglob("*")
        if p.suffix in (".ts", ".tsx", ".js", ".jsx")
        and not p.name.endswith(".d.ts")  # ambient type declarations — no executable code, ever
        and in_repo(p)
        and not _is_auxiliary_file(p)
    ))
    go_files = _dedupe_by_content(sorted(
        p for p in root.rglob("*.go")
        if "vendor" not in p.relative_to(root).parts[:-1] and in_repo(p)
        and not _is_auxiliary_file(p)
    ))
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
    SECURITY_CHECK_WEIGHT = {
        "secrets": 5, "dangerous_exec": 5, "unsafe_deserialization": 5, "ssrf": 2,
        "unpinned_dependency": 2,
    }
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
