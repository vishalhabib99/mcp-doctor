# Coverage: does mcp-doctor find the tools that are actually there?

Every check mcp-doctor runs depends on first finding a server's tools. A tool it can't see is a tool it can't grade, so a missed registration style is the most expensive kind of bug: the server just gets a quiet, misleading report. This page measures that recall in the open, one framework at a time.

**Method.** For each framework: find every public repo that depends on it (GitHub code search on its manifest file), clone them, and compare the tools mcp-doctor reports against an **independent count**, a plain regex for the framework's literal tool-definition syntax in non-test source files. Every tool the count finds but mcp-doctor doesn't is checked by hand and either fixed or listed below as out of scope. A tool mcp-doctor finds beyond the count (a name the regex can't see, such as a constant) is checked too. Before any fix ships, the full before/after is run on every local repo, and only the intended repos may change.

**Limits.** GitHub code search returns at most a few hundred results, so large ecosystems are a sample, not a census. The independent count only sees literal names, so recall is measured on tools with a literal name. Repos over 150 MB were skipped.

| Framework | Language | Repos swept | Tools found / counted | Before the sweep | Fixed in | Swept |
|---|---|---|---|---|---|---|
| [`@cyanheads/mcp-ts-core`](https://github.com/cyanheads/mcp-ts-core) | TS | 35 (all that list it) | **261 / 261** | 0 (style not supported) | v1.12.5, v1.12.6 | 2026-09-30 |
| [`mcp-framework`](https://github.com/QuantGeekDev/mcp-framework) | TS | 55 with tools, of 82 that list it | **366 / 369** | 0 (style not supported) | v1.12.7 | 2026-09-30 |
| [`mark3labs/mcp-go`](https://github.com/mark3labs/mcp-go) | Go | 127 with tools, of a 175-repo sample (~5,900 dependents) | **1,748 / 1,765** | 954 / 1,765 (54%) | v1.12.8 | 2026-09-30 |

## Known misses, all out of scope

- **mcp-framework (3):** template strings in the framework's own project generator (`${toolName}`, `example_tool`), not tools.
- **mcp-go (17):** 16 are in two repos that define a package also named `mcp` (`github-mcp-server`'s `pkg/mcp` in a fork, and a repo-local `internal/mcp`), which mcp-doctor correctly doesn't read as mcp-go. 1 is a name built at runtime (`"postgres-" + ...`), which the regex miscounted.

## What each sweep fixed

- **mcp-ts-core:** the framework's two-argument `tool('name', { description, input, handler })` wasn't recognized; a description built at runtime was reported as missing; and TS files whose name merely *contained* "test" or "spec" (`pentest-*.tool.ts`, `*-species.tool.ts`) were skipped as tests.
- **mcp-framework:** class-based tools (`class FooTool extends MCPTool`), auto-discovered with no registration call, weren't recognized at all; tools extending a repo-local base class are followed too.
- **mcp-go:** a tool built by `mcp.NewTool(...)` in a helper function, another file or a registry was only counted when its `AddTool` call could be traced back to it. Every `mcp.NewTool(...)` (and bare `mcp.Tool{...}` literal) from the mcp-go package is now a definition site on its own.

Next frameworks to sweep: standalone `fastmcp` (Python) and the official SDKs' loop/registry registration styles.
