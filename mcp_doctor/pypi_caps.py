"""Ask PyPI whether a co-installed package keeps `mcp` below 2.

The sdk_v2 check flags a server whose own `mcp` requirement allows 2.x. But a
fresh install can still get mcp 1.x when another requirement in the same
manifest caps it, directly (chainlit: `mcp<2.0.0`) or through a chain of
MCP packages (mcp-proxy-for-aws -> fastmcp<3.5 -> fastmcp-slim[client,server]
-> mcp<2). `sdk_upgrade._CAPPING_PACKAGES` lists the ones seen so far; this
module covers the rest by reading the package's metadata from PyPI.

Approximations, on purpose:
- A package counts only if every final release its specifier allows caps
  mcp: resolvers differ (uv backtracks the other package when `mcp` comes
  first), so anything weaker could hide a real break.
- Only the package's own requirements, and those of dependencies whose name
  contains "mcp", are followed (up to `_MAX_DEPTH` levels), so a lookup
  costs a few requests, not a crawl of boto3's tree.
- Markers other than `extra == ...` are treated as true.

Any failure (no network, Pyodide, timeout, odd metadata) means "unknown",
and the finding stays. `MCP_DOCTOR_OFFLINE=1` turns lookups off.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor

_MAX_DEPTH = 4
_MAX_RELEASES = 80  # uncached metadata reads per top-level requirement
_TIMEOUT = 6.0
_INDEX = "https://pypi.org/pypi"
_cache: dict[str, object] = {}

_FINAL = re.compile(r"^\d+(?:\.\d+)*$")
_CLAUSE = re.compile(r"(===|==|!=|~=|<=|>=|<|>)\s*([0-9][0-9A-Za-z.*+!-]*)")
_REQ = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?\s*\(?([^;()]*)\)?\s*(?:;(.*))?$")
# Keeps mcp below 2: <2, <2.0, <1.x, <=1.x, ==1.x, ~=1.x.
_CAPS_BELOW_2 = re.compile(r"<\s*=?\s*[01](?:\.|\s*$|\s*,)|<\s*2(?:\.0)*(?![.\d])|={2,3}\s*[01]\.|~=\s*[01]\.")


def offline() -> bool:
    return os.environ.get("MCP_DOCTOR_OFFLINE", "").strip().lower() not in ("", "0", "false", "no")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _ver(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in text.split("."))


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    n = max(len(a), len(b))
    a, b = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def satisfies(version: str, spec: str) -> bool:
    """Whether a final release `version` meets `spec` (a PEP 440 subset)."""
    v = _ver(version)
    for op, raw in _CLAUSE.findall(spec):
        raw = raw.rstrip(",")
        if raw.endswith(".*"):
            prefix = _ver(raw[:-2])
            hit = v[: len(prefix)] == prefix
            if (op in ("==", "===") and not hit) or (op == "!=" and hit):
                return False
            continue
        try:
            t = _ver(re.sub(r"[^0-9.].*$", "", raw).rstrip("."))
        except ValueError:
            return False
        c = _cmp(v, t)
        if op in ("==", "===") and c != 0:
            return False
        if op == "!=" and c == 0:
            return False
        if (op == ">=" and c < 0) or (op == "<=" and c > 0) or (op == ">" and c <= 0) or (op == "<" and c >= 0):
            return False
        if op == "~=":
            upper = list(t[:-1]) if len(t) > 1 else list(t)
            upper[-1] += 1
            if c < 0 or _cmp(v, tuple(upper)) >= 0:
                return False
    return True


def _get(url: str):
    if url in _cache:
        return _cache[url]
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "mcp-doctor"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
    except Exception:  # offline, Pyodide, 404, bad JSON: all "unknown"
        data = None
    _cache[url] = data
    return data


def _allowed(name: str, spec: str) -> list[str] | None:
    """Every final, non-yanked release `spec` allows, oldest first (None if unknown)."""
    data = _get(f"{_INDEX}/{_norm(name)}/json")
    if not isinstance(data, dict):
        return None
    versions = [
        v for v, files in (data.get("releases") or {}).items()
        if _FINAL.match(v) and files and not all(f.get("yanked") for f in files) and satisfies(v, spec)
    ]
    return sorted(versions, key=_ver)


def _requires(name: str, version: str) -> list[str] | None:
    data = _get(f"{_INDEX}/{_norm(name)}/{version}/json")
    if not isinstance(data, dict):
        return None
    return (data.get("info") or {}).get("requires_dist") or []


def _applies(marker: str | None, extras: set[str]) -> bool:
    if not marker:
        return True
    wanted = {_norm(e) for e in re.findall(r"""extra\s*==\s*["']([^"']+)["']""", marker)}
    return not wanted or bool(wanted & extras)


class _Budget:
    def __init__(self, n: int):
        self.n = n

    def spend(self) -> bool:
        self.n -= 1
        return self.n >= 0


def _release_caps(name: str, version: str, extras: set[str], depth: int, budget: _Budget) -> str | None:
    """Why `name==version` keeps mcp below 2 ("requires mcp<2"), or None if it doesn't (or unknown)."""
    if f"{_INDEX}/{_norm(name)}/{version}/json" not in _cache and not budget.spend():
        return None  # only reads that hit the network count against the budget
    reqs = _requires(name, version)
    if reqs is None:
        return None
    deeper = []
    for line in reqs:
        m = _REQ.match(line)
        if not m or not _applies(m.group(4), extras):
            continue
        dep, dep_extras, dep_spec = _norm(m.group(1)), m.group(2) or "", (m.group(3) or "").strip()
        if dep == "mcp":
            if _CAPS_BELOW_2.search(dep_spec):
                return f"requires mcp{dep_spec.replace(' ', '')}"
        elif "mcp" in dep:
            deeper.append((dep, dep_spec, {_norm(e) for e in dep_extras.split(",") if e.strip()}))
    for dep, dep_spec, dep_extras in deeper:
        why = caps_mcp(dep, dep_spec, dep_extras, depth + 1, budget)
        if why:
            return f"-> {why}"
    return None


def caps_mcp(name: str, spec: str, extras: set[str], depth: int = 0, budget: _Budget | None = None) -> str | None:
    """Why `name<spec>[extras]` keeps mcp below 2 whatever the resolver picks, or None.

    True only if EVERY release `spec` allows caps mcp (directly, or through a
    required package named *mcp*): a resolver that takes the newest mcp first
    backtracks to the oldest allowed release, so one uncapped release lets
    mcp 2.x in (uv does this when `mcp` is listed first). Releases are tried
    oldest and newest first, where uncapped ones usually are, and the lookup
    gives up ("unknown", finding kept) past `budget` metadata reads.
    """
    budget = budget or _Budget(_MAX_RELEASES)
    if depth > _MAX_DEPTH or _norm(name) == "mcp":
        return None
    versions = _allowed(name, spec)
    if not versions:
        return None
    order = [versions[0], versions[-1]] + versions[1:-1] if len(versions) > 1 else versions
    why = None
    for v in order:
        why = _release_caps(name, v, extras, depth, budget)
        if not why:
            return None
    lo, hi = versions[0], versions[-1]
    span = lo if lo == hi else f"{lo}..{hi}"
    return f"{_norm(name)} {span} {why}"


def newest_caps_mcp(name: str, spec: str, extras: set[str]) -> str | None:
    """Why the newest release `spec` allows (what pip and uv pick first) keeps mcp below 2, or None."""
    versions = _allowed(name, spec)
    if not versions:
        return None
    why = _release_caps(name, versions[-1], extras, 0, _Budget(_MAX_RELEASES))
    return f"{_norm(name)} {versions[-1]} {why}" if why else None


def first_capping(reqs: list[tuple[str, str, set[str]]]) -> tuple[str, bool] | None:
    """Of a manifest's other requirements, the first chain that keeps mcp below 2 (looked up in parallel).

    (why, True) if every release a requirement allows caps mcp; else (why, False)
    if the newest allowed release of one does; else None.
    """
    if offline() or not reqs:
        return None
    with ThreadPoolExecutor(max_workers=min(8, len(reqs))) as ex:
        results = list(ex.map(lambda r: caps_mcp(*r), reqs))
        sure = next((r for r in results if r), None)
        if sure:
            return sure, True
        newest = list(ex.map(lambda r: newest_caps_mcp(*r), reqs))
    partial = next((r for r in newest if r), None)
    return (partial, False) if partial else None
