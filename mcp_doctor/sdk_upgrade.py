"""Python SDK v2 readiness: a server that crashes on a fresh install.

`mcp` 2.0.0 (2026-07-28) removed `mcp.server.fastmcp` (FastMCP is now
`MCPServer` in `mcp.server.mcpserver`), a handful of other v1 imports, and the
low-level `Server`'s handler decorators (`@server.list_tools()`). A server
that still uses them and declares `mcp` without an upper bound
(`mcp>=1.6`, `mcp[cli]`) gets mcp 2.x from pip/uvx today and fails to start
with `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`. The eight
newest reports of this (2026-10-13..18) were all from users who installed a
server and couldn't start it.

The removed names below were taken by diffing the public modules of mcp
1.30.0 and 2.3.0, keeping only what a server or client actually imports.
Imports inside `try: ... except ImportError:` are a deliberate v1/v2 shim
and aren't flagged. The dependency is read from the nearest pyproject.toml,
setup.py or requirements.txt that declares `mcp` (the files the in-browser
check also downloads); a server that declares none isn't flagged, since
which `mcp` it gets can't be known.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .analyzer import RepoIssue

# Modules that no longer exist in mcp 2.x (any submodule too).
_REMOVED_MODULES = (
    "mcp.server.fastmcp",
    "mcp.server.websocket",
    "mcp.client.websocket",
    "mcp.shared.session",
    "mcp.shared.version",
    "mcp.shared.progress",
    "mcp.shared.response_router",
    "mcp.server.experimental",
    "mcp.shared.experimental",
    "mcp.server.lowlevel.experimental",
    "mcp.server.lowlevel.func_inspection",
)
_MESSAGE_UNIONS = {"ClientNotification", "ClientRequest", "ClientResult", "ServerNotification", "ServerRequest", "ServerResult"}
# Names a v1 module exported that its v2 version doesn't.
_REMOVED_NAMES = {
    "mcp": {"McpError"} | _MESSAGE_UNIONS,
    "mcp.types": {"Content", "Cursor", "ResourceReference", "JSONRPCMessage"} | _MESSAGE_UNIONS,
    "mcp.server": {"FastMCP", "fastmcp"},
    "mcp.shared.exceptions": {"McpError"},
    "mcp.client.streamable_http": {"streamablehttp_client"},
    "mcp.shared.memory": {"create_connected_server_and_client_session"},
}
# The low-level Server's handler decorators, all gone in 2.x (handlers are now
# constructor arguments, `Server(on_list_tools=...)`). `@server.list_tools()`
# runs when the module loads, so it fails at start with an AttributeError
# (BeehiveInnovations/pal-mcp-server's server.py).
_LOWLEVEL_SERVER_MODULES = ("mcp.server", "mcp.server.lowlevel", "mcp.server.lowlevel.server")
_LOWLEVEL_DECORATORS = {
    "list_tools", "call_tool", "list_resources", "read_resource", "list_prompts", "get_prompt",
    "list_resource_templates", "completion", "set_logging_level", "subscribe_resource",
    "unsubscribe_resource", "progress_notification",
}
_MIGRATION_URL = "https://py.sdk.modelcontextprotocol.io/v2/migration/"

_MANIFESTS = ("pyproject.toml", "setup.py", "requirements.txt")
# A requirement on the SDK itself: `mcp`, `mcp[cli]`, not `mcp-server-git` or `fastmcp`.
_QUOTED_REQ = re.compile(r"""["']mcp(?:\[[^\]]*\])?\s*([<>=!~^][^"';]*)?(?:;[^"']*)?["']""")
_POETRY_REQ = re.compile(r"""^\s*["']?mcp["']?\s*=\s*(?:["']([^"']*)["']|\{[^}]*?version\s*=\s*["']([^"']*)["'])""", re.M)
_REQ_LINE = re.compile(r"^\s*mcp(?:\[[^\]]*\])?\s*([<>=!~^][^;#]*)?(?:[;#].*)?$", re.M)
# Any specifier that keeps mcp below 2: <2, <2.0, <1.9, <=1.x, ==1.x, ~=1.x, poetry ^1 / ~1.
_CAPPED = re.compile(r"<\s*=?\s*[01](?:\.|\s*$|\s*,)|<\s*2(?:\.0)*(?![.\d])|={2,3}\s*[01]\.|~=\s*[01]\.|\^\s*[01]\b|(?<![=<>!])~\s*[01]\b")


def _is_removed_module(name: str) -> bool:
    return any(name == m or name.startswith(m + ".") for m in _REMOVED_MODULES)


def _removed_import(node: ast.AST) -> str | None:
    """The removed v1 import a node makes, as it's written, or None."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            if _is_removed_module(alias.name):
                return f"import {alias.name}"
        return None
    if isinstance(node, ast.ImportFrom) and node.module and not node.level:
        mod = node.module
        if _is_removed_module(mod):
            return f"from {mod} import ..."
        gone = sorted(_REMOVED_NAMES.get(mod, set()) & {a.name for a in node.names})
        if gone:
            return f"from {mod} import {', '.join(gone)}"
    return None


def _catches_import_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(t, ast.Name) and t.id in ("ImportError", "ModuleNotFoundError", "Exception") for t in types)


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _lowlevel_decorators(tree: ast.Module) -> list[tuple[int, str]]:
    """`@server.list_tools()`-style handlers on a low-level `Server(...)` instance."""
    server_names = {
        a.asname or a.name for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module in _LOWLEVEL_SERVER_MODULES and not n.level
        for a in n.names if a.name == "Server"
    }
    if not server_names:
        return []
    instances: set[str] = set()
    for n in ast.walk(tree):
        value = n.value if isinstance(n, (ast.Assign, ast.AnnAssign)) else None
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in server_names:
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            instances.update(d for d in map(_dotted, targets) if d)
    # A file that checks for or installs the decorators itself has a v2 shim
    # (maven-decoder-mcp: `if not hasattr(self.server, "list_tools"): self.server.list_tools = ...`).
    for n in ast.walk(tree):
        if (
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "hasattr"
            and len(n.args) == 2 and isinstance(n.args[1], ast.Constant) and n.args[1].value in _LOWLEVEL_DECORATORS
        ) or (
            isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store) and n.attr in _LOWLEVEL_DECORATORS
        ):
            return []
    hits = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in n.decorator_list:
                func = dec.func if isinstance(dec, ast.Call) else None
                if isinstance(func, ast.Attribute) and func.attr in _LOWLEVEL_DECORATORS and _dotted(func.value) in instances:
                    hits.append((dec.lineno, f"@{_dotted(func)}()"))
    return hits


def _first_removed_import(tree: ast.Module) -> tuple[int, str] | None:
    guarded: set[int] = set()
    for node in ast.walk(tree):
        # Either branch of the shim may hold the v1 import.
        if isinstance(node, ast.Try) and any(_catches_import_error(h) for h in node.handlers):
            guarded.update(id(n) for n in ast.walk(node))
    hits = [
        (node.lineno, what) for node in ast.walk(tree)
        if id(node) not in guarded and (what := _removed_import(node))
    ] + _lowlevel_decorators(tree)
    return min(hits) if hits else None


def _pyproject_requirements(text: str) -> list[str] | None:
    """Requirement strings from a pyproject's dependency tables, or None without a TOML parser."""
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return None
    try:
        data = tomllib.loads(text)
    except Exception:
        return []
    reqs: list[str] = []
    project = data.get("project") or {}
    reqs += project.get("dependencies") or []
    for group in (project.get("optional-dependencies") or {}).values():
        reqs += group or []
    for group in (data.get("dependency-groups") or {}).values():
        reqs += [r for r in group or [] if isinstance(r, str)]
    poetry = (data.get("tool") or {}).get("poetry") or {}
    tables = [poetry.get("dependencies") or {}] + [
        g.get("dependencies") or {} for g in (poetry.get("group") or {}).values() if isinstance(g, dict)
    ]
    for table in tables:
        spec = table.get("mcp")
        if isinstance(spec, dict):
            spec = spec.get("version", "")
        if isinstance(spec, str):
            reqs.append("mcp" + ("" if spec.strip() == "*" else spec))
    return [r for r in reqs if isinstance(r, str)]


def _mcp_requirement(manifest: Path) -> str | None:
    """The version specifier `manifest` gives `mcp` ("" for none), or None if it doesn't declare mcp."""
    try:
        text = manifest.read_text(errors="ignore")
    except OSError:
        return None
    if manifest.name == "requirements.txt":
        m = _REQ_LINE.search(text)
        return (m.group(1) or "").strip() if m else None
    if manifest.name == "pyproject.toml":
        reqs = _pyproject_requirements(text)
        if reqs is None:
            # No TOML parser (Python < 3.11 without tomli): drop the lists that
            # name "mcp" without being dependencies before matching.
            text = re.sub(r"(?ms)^\s*(?:keywords|classifiers)\s*=\s*\[.*?\]", "", text)
            m = _QUOTED_REQ.search(text) or _POETRY_REQ.search(text)
            if not m:
                return None
            spec = next((g for g in m.groups() if g is not None), "")
            return "" if spec.strip() == "*" else spec.strip()
        for r in reqs:
            m = _REQ_LINE.match(r)
            if m:
                return (m.group(1) or "").strip()
        return None
    # setup.py: only install_requires / extras_require, not keywords=["mcp", ...].
    blocks = re.findall(r"(?s)(?:install_requires|extras_require)\s*=\s*([\[{].*?[\]}])", text)
    m = _QUOTED_REQ.search("\n".join(blocks))
    return (m.group(1) or "").strip() if m else None


def _declaring_manifest(path: Path, root: Path) -> tuple[Path, str] | None:
    d = path.parent
    while True:
        for name in _MANIFESTS:
            manifest = d / name
            if manifest.is_file():
                spec = _mcp_requirement(manifest)
                if spec is not None:
                    return manifest, spec
        if d == root or root not in d.parents:
            return None
        d = d.parent


def scan_sdk_v2_breakage(root: Path, trees: list[tuple[str, ast.Module]]) -> list[RepoIssue]:
    """One issue per manifest whose uncapped `mcp` lets a v1-only import crash on install."""
    by_manifest: dict[Path, tuple[str, str, int, str]] = {}
    for rel, tree in trees:
        hit = _first_removed_import(tree)
        if not hit:
            continue
        found = _declaring_manifest(root / rel, root)
        if not found:
            continue
        manifest, spec = found
        if _CAPPED.search(spec):
            continue
        by_manifest.setdefault(manifest, (rel, spec, *hit))
    issues = []
    for manifest, (rel, spec, line, what) in sorted(by_manifest.items()):
        req = f"mcp{spec}" if spec else "mcp"
        if "fastmcp" in what or "FastMCP" in what:
            fix = "migrate to MCPServer (`from mcp.server.mcpserver import MCPServer`)"
        elif what.startswith("@"):
            fix = "pass the handlers to the constructor (`Server(on_list_tools=...)`)"
        else:
            fix = "migrate the import"
        issues.append(RepoIssue(
            "sdk_v2",
            f"{rel}:{line} `{what}` was removed in mcp 2.x, and {manifest.relative_to(root)} asks for "
            f"'{req}' with no upper bound, so a fresh install gets mcp 2.x and the server fails to start "
            f"with an {'AttributeError' if what.startswith('@') else 'ImportError'}. Pin 'mcp<2', or {fix}: {_MIGRATION_URL}",
            "error",
        ))
    return issues
