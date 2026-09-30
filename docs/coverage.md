# Coverage: does mcp-doctor find the tools that are actually there?

Every check mcp-doctor runs depends on first finding a server's tools. A tool it can't see is a tool it can't grade, so a missed registration style is the most expensive kind of bug: the server just gets a quiet, misleading report. This page measures that recall in the open, one framework at a time.

**Method.** For each framework: find every public repo that depends on it (GitHub code search on its manifest file), clone them, and compare the tools mcp-doctor reports against an **independent count**, a plain regex for the framework's literal tool-definition syntax in non-test source files. Every tool the count finds but mcp-doctor doesn't is checked by hand and either fixed or listed below as out of scope. A tool mcp-doctor finds beyond the count (a name the regex can't see, such as a constant) is checked too. Before any fix ships, the full before/after is run on every local repo, and only the intended repos may change.

**Limits.** GitHub code search returns at most a few hundred results, so large ecosystems are a sample, not a census. The independent count only sees literal names, so recall is measured on tools with a literal name. Repos over 150 MB were skipped.

| Framework | Language | Repos swept | Tools found / counted | Before the sweep | Fixed in | Swept |
|---|---|---|---|---|---|---|
| [`@cyanheads/mcp-ts-core`](https://github.com/cyanheads/mcp-ts-core) | TS | 35 (all that list it) | **261 / 261** | 0 (style not supported) | v1.12.5, v1.12.6 | 2026-09-30 |
| [`mcp-framework`](https://github.com/QuantGeekDev/mcp-framework) | TS | 55 with tools, of 82 that list it | **366 / 369** | 0 (style not supported) | v1.12.7 | 2026-09-30 |
| [`mark3labs/mcp-go`](https://github.com/mark3labs/mcp-go) | Go | 127 with tools, of a 175-repo sample (~5,900 dependents) | **1,748 / 1,765** | 954 / 1,765 (54%) | v1.12.8 | 2026-09-30 |
| [`fastmcp`](https://github.com/PrefectHQ/fastmcp) + the official SDK's `FastMCP` | Python | 386 with tools, of 447 cloned from a 607-repo search sample | **8,853 / 9,016** | 8,241 / 9,016 (91.4%) on 3.12; 8,223 (91.2%) on the Action's old 3.11 | v1.12.9 | 2026-09-30 |

## Known misses, all out of scope

- **fastmcp (163):** tools registered from a loop variable (`for fn in tools: mcp.tool()(fn)`) or built by a factory at runtime (`add_tool(_make_tool(...))`, `Tool.from_tool(proxied)`); repo-local wrapper APIs (`self.add_tool(name=..., endpoint=...)`); placeholder names in docstrings and examples (`your_tool`, `my_custom_tool`); and agent-framework tools the regex also matches (LangGraph `self.amap_tool`, spoon-ai `MyCustomTool`). The independent count here is looser than for the other frameworks: it skips byte-identical duplicate files (mcp-doctor reads each once, by design) and credits a mounted tool reported under its namespace prefix (`github_create_issue` for `create_issue`).

- **mcp-framework (3):** template strings in the framework's own project generator (`${toolName}`, `example_tool`), not tools.
- **mcp-go (17):** 16 are in two repos that define a package also named `mcp` (`github-mcp-server`'s `pkg/mcp` in a fork, and a repo-local `internal/mcp`), which mcp-doctor correctly doesn't read as mcp-go. 1 is a name built at runtime (`"postgres-" + ...`), which the regex miscounted.

## What each sweep fixed

- **fastmcp:** FastMCP's by-reference registrations weren't recognized: `mcp.tool(fn)` without `name=`, the decorator applied by hand (`mcp.tool()(self.get_regions)`, 341 counted tools, 10 found before), `mcp.add_tool(fn)`, and `Tool.from_function(fn, ...)` wherever it's registered. The function is now followed into the module it's imported from, to module level from inside a `register(mcp)` function, and to `self.` methods. Only in a file that imports FastMCP, or on a receiver named like a server in a repo that does; LangChain's own `Tool.from_function` is excluded. Also: files starting with a UTF-8 byte-order mark failed to parse; two repos crashed the scan (a `README_files/` directory, a broken `.py` symlink); the Action ran Python 3.11, so any file using 3.12+ syntax was skipped (now 3.14, which also reads `except A, B:`); and the order files were read in differed between Python versions, which could change which of two identical files was reported. Files are now read in sorted order.

- **mcp-ts-core:** the framework's two-argument `tool('name', { description, input, handler })` wasn't recognized; a description built at runtime was reported as missing; and TS files whose name merely *contained* "test" or "spec" (`pentest-*.tool.ts`, `*-species.tool.ts`) were skipped as tests.
- **mcp-framework:** class-based tools (`class FooTool extends MCPTool`), auto-discovered with no registration call, weren't recognized at all; tools extending a repo-local base class are followed too.
- **mcp-go:** a tool built by `mcp.NewTool(...)` in a helper function, another file or a registry was only counted when its `AddTool` call could be traced back to it. Every `mcp.NewTool(...)` (and bare `mcp.Tool{...}` literal) from the mcp-go package is now a definition site on its own.

Next frameworks to sweep: the official SDKs' loop/registry registration styles.
