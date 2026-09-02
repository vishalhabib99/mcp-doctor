# mcp-doctor

[![CI](https://github.com/vishalhabib99/mcp-doctor/actions/workflows/ci.yml/badge.svg)](https://github.com/vishalhabib99/mcp-doctor/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mcp-server-lint.svg)](https://pypi.org/project/mcp-server-lint/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![GitHub Marketplace](https://img.shields.io/badge/Marketplace-mcp--doctor-blue?logo=github)](https://github.com/marketplace/actions/mcp-doctor)

A static analysis CLI that audits **MCP (Model Context Protocol) server** implementations for the things that actually break an agent calling them: missing tool descriptions, undocumented parameters, no error handling, no README coverage.

The MCP ecosystem is growing faster than the conventions around building a *good* server have settled. Most servers are hand-written in an afternoon and never checked against anything. `mcp-doctor` is a linter for that gap — point it at a repo, get a score and a concrete list of what to fix.

```
$ mcp-doctor examples/bad_server
mcp-doctor report
Score: 13%  Grade: F  (2 tool(s) found)

  [FAIL] do_thing (server.py:9)
      ERROR  Tool has no description. An agent cannot decide when to call this.
      WARNING  2/2 parameters have no type annotation.
      WARNING  Parameters aren't documented in an Args: section — the model only sees names, not intent.
      WARNING  No try/except — an exception here will raise a raw traceback back through the MCP transport.
  [FAIL] run (server.py:15)
      ERROR  Tool has no description. An agent cannot decide when to call this.
      WARNING  1/1 parameters have no type annotation.
      WARNING  Parameters aren't documented in an Args: section — the model only sees names, not intent.
      ERROR  Bare 'except:' swallows all errors including cancellation — catch specific exceptions.

Repo-level
  ERROR  No README found.
  WARNING  No LICENSE file — undermines adoption.
  WARNING  No test files found.
  WARNING  No pyproject.toml/requirements.txt/setup.py — dependencies aren't pinned.
```

```
$ mcp-doctor examples/good_server
mcp-doctor report
Score: 100%  Grade: A  (1 tool(s) found)

  [OK] get_forecast (server.py:9)
```

## Install

```bash
pip install mcp-server-lint
```

(The PyPI project is named `mcp-server-lint` — `mcp-doctor` and every close variant of it were already taken or blocked by PyPI's anti-typosquat check — but the installed command is still `mcp-doctor`.)

Or install straight from the repo:

```bash
pip install git+https://github.com/vishalhabib99/mcp-doctor.git
```

or clone it and install locally:

```bash
git clone https://github.com/vishalhabib99/mcp-doctor.git
cd mcp-doctor
pip install -e .
```

## Usage

```bash
mcp-doctor .                      # audit the current directory
mcp-doctor path/to/server         # audit a specific path
mcp-doctor . --json               # machine-readable output
mcp-doctor . --fail-under 80      # exit 1 if score drops below 80% — wire into CI
mcp-doctor . --fix                # apply safe, mechanical fixes in place, then re-report
```

`--fix` only touches what's safe to fix without human judgment: narrowing a bare `except:` to `except Exception:`, and stubbing an `Args:` docstring section (with `TODO: describe this parameter.` placeholders) for a tool whose params have *no* documentation at all. It never fabricates a missing description, guesses at types, wraps a function body in try/except, or touches a docstring that already documents some but not all of its params — those still need a human.

## GitHub Action

Gate PRs on server quality without installing anything yourself:

```yaml
- uses: vishalhabib99/mcp-doctor@v1
  with:
    path: .              # default: repo root
    fail-under: 70        # default: 0 (report only, don't fail the build)
    comment: true          # default: true — posts/updates a PR comment with the report
```

The report also gets written to the job summary either way. `@v1` tracks the latest `v1.x` release; pin an exact tag or commit SHA instead if you need stricter reproducibility.

## What it checks

Audits both Python and TypeScript/JavaScript servers in the same repo. Python detects the FastMCP `@mcp.tool()` decorator style and the low-level SDK's `Tool(name=..., description=..., inputSchema=...)` style; TS/JS detects the official SDK's `server.registerTool(name, config, handler)` and `server.tool(name, description, schema, handler)` styles, including the common pattern where the config object or Zod schema is a same-file `const` reference rather than inline. The same checks apply either way — a description, per-parameter docs (`Args:`/`Field(description=...)` in Python, `.describe(...)` on each Zod field in TS), and a try/except (or try/catch).

**Per tool**:

| Check | Why it matters |
|---|---|
| Has a description | An agent picks tools by reading descriptions. No description, no calls. |
| Description isn't trivially short | A 3-character description is functionally the same as none. |
| Parameters are type-annotated | Untyped params usually mean the schema exposed to the model is untyped too. |
| Parameters are documented (`Args:` section, or schema `description` fields) | The model sees parameter names but not intent unless you spell it out. |
| Has error handling | FastMCP catches an unhandled exception and returns a structured error either way — this check is about message quality, not transport safety: a tool-level catch can raise a specific, actionable message instead of leaving the model with generic exception text. |
| No bare `except:` | Swallows everything, including cancellation — a real production bug pattern, not just a style nit. |

**Repo-level:**

- README exists, and mentions every tool you export
- LICENSE exists
- Tests exist
- Dependencies are declared (`pyproject.toml` / `requirements.txt` / `setup.py` / `package.json`)
- No hardcoded-looking API keys/secrets/tokens in source
- Tool names conform to the [spec's Tool Names guidance](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#tool-names) (1–128 chars, `A-Z a-z 0-9 _ - .` only, unique within the server)

## Real-world spot check

Run against three servers from the official [`modelcontextprotocol/servers`](https://github.com/modelcontextprotocol/servers) repo:

- **`src/fetch`** — **100% / A**. Clean.
- **`src/git`**, **`src/time`** — flagged as **parse errors**, not false passes. Both use Python `match` statements (3.10+ syntax); `mcp-doctor`'s AST parser follows the grammar of whatever Python interpreter runs it, so under Python 3.9 those files can't be parsed. Rather than silently skip them and report a misleadingly clean score, `mcp-doctor` surfaces this as an explicit error: *"N file(s) could not be parsed and were skipped."* Run it under Python ≥3.10 to analyze those files correctly.

Later spot-checked against 4 more real, in-the-wild servers (awslabs' `aws-documentation-mcp-server`, `mcp-google-ads`, `sv-excel-agent`, and Home Assistant's `ha-mcp`, an 88-tool server). That run caught two real precision bugs: the secret scanner was flagging test fixtures and identifier-style constant names (`SERVICE_GET_CALLER_TOKEN = "get_caller_token"`) as hardcoded credentials, and the param-docs check didn't recognize `Annotated[T, Field(description=...)]` — a completely valid, schema-level way to document a parameter — as documentation at all, since it only looked for a docstring `Args:` section. Both fixed.

A maintainer on `ha-mcp` reviewed the resulting report in detail and pushed back further, correctly: the param-docs check still missed descriptions reached through a shared, cross-file type alias (`Annotated[..., Field(description=...)]` assigned to a name and imported elsewhere) and prose under non-`Args:` headings (e.g. `**Parameters:**`, including bulleted `- param: ...` lines), and — more importantly — the error-handling check's own message was wrong. It claimed a missing try/except lets a raw traceback leak through the MCP transport; FastMCP's `call_tool` dispatcher actually wraps every call and converts any exception into a structured error regardless, which the pushback prompted me to verify directly against FastMCP's source. Both the alias/heading gaps and the error-handling message are now fixed — see [homeassistant-ai/ha-mcp#2324](https://github.com/homeassistant-ai/ha-mcp/issues/2324) for the full exchange.

The maintainer offered to leave a follow-up issue open if it were grounded in the actual spec and FastMCP's own guidelines rather than another pass of the same heuristics. Read the [current spec's Tools page](https://modelcontextprotocol.io/specification/2026-07-28/server/tools) end to end looking for exactly that: one concrete, checkable gap emerged — the normative **Tool Names** section (length, character set, uniqueness), which mcp-doctor didn't check at all — now added. Checked it against ha-mcp's real 88 tool names before claiming anything: all of them already comply, so this doesn't reopen anything there — it's a real gap closed for the next server that isn't as careful, not a finding to hand back.

A third real-world pass against `mendableai/firecrawl-mcp-server` (7k+ stars, TypeScript) found a genuine gap: it reported **0 tools** on a 28-tool server. The repo registers every tool through the community [`fastmcp`](https://github.com/punkpeye/fastmcp) package's `server.addTool({ name, description, parameters, execute })` — a single-object call shape mcp-doctor's TS analyzer didn't recognize at all, having only ever seen the official SDK's positional-arg `registerTool`/`tool` styles. Added support for it (verified against fastmcp's own docs, not just this one repo's usage), re-ran, and got a real 84%/B report on all 28 tools. Also surfaced a duplicate-name warning (`firecrawl_search` registered twice) that turned out to be a false positive: the two registrations are read from source, but the code documents and structurally enforces that they're mutually exclusive by runtime profile and land on separate server instances — the check has no way to know that statically. Left as-is rather than filing anything upstream; see Known limitations below.

A fourth pass against `exa-labs/exa-mcp-server` (TypeScript) found the TS analyzer silently dropping tools named with the common `toolName || "default-name"` optional-override idiom — treated as fully dynamic (like a genuinely unattributable name from a loop) rather than resolved to the literal fallback actually used at runtime. Its two headline tools, `web_search_exa` and `web_fetch_exa`, were invisible: 9 tools reported instead of the real 11, still showing a false 100%/A. Fixed by unwrapping a `||` binary expression to its literal right-hand side during name resolution, with a regression test confirming a genuinely dynamic name (`t.name` from a loop) still correctly falls through to skipped.

A fifth pass against `stickerdaniel/linkedin-mcp-server` (Python) found 13 of 19 tools falsely flagged as undocumented. All 13 use FastMCP's `@mcp.tool(exclude_args=[...])` to keep an internal-only parameter out of the exposed schema (verified against FastMCP's own docs: an excluded arg literally can't be passed by an agent) — every one had complete `Args:` docs for every parameter an agent can actually pass, but the param-docs check was still requiring documentation for the excluded one too. Fixed by excluding `exclude_args` names from the parameter count and doc requirement entirely; 93%/A on that repo corrected to the true 100%/A.

That same session, a sixth pass against `atilaahmettaner/tradingview-mcp` (39 tools) turned up something worth fixing in the tool itself rather than a new false positive: the error-handling check's known delegation blind spot (below) had by then shown up on two separate real repos (firecrawl and this one — `market_sentiment` delegates through `analyze_sentiment` → `_get_articles` → `_request`, three calls deep across files, before reaching the actual try/except around the network call). Built a repo-wide, name-based resolution (same simplification already used for the Field alias registry — resolved by function name, not by which file it's imported from) that transitively follows a tool's direct calls to locally-defined functions, so a tool that delegates to a helper that itself has real error handling — however many calls deep — is no longer flagged. Re-checking the same repo surfaced one more real gap in the fix itself: `compare_strategies` delegates through `_compare_strategies`, a `from strategies import compare_strategies as _compare_strategies` alias — the registry was keyed by the def name (`compare_strategies`), so the aliased call site didn't resolve. Added alias resolution (`from x import y as z` mapped back to `y`) so the registry itself understands the alias. Verified it doesn't just widen the check: a tool calling only unhandled or genuinely external code is still correctly flagged, and one real remaining gap was left honestly undone rather than papered over — `financial_news` passes its helper as an *argument* to `asyncio.to_thread(fetch_news_summary, ...)` rather than calling it by name directly, a different idiom this resolution doesn't cover; still flagged.

## Known limitations

- **AST-based, single-pass.** Tools constructed dynamically in a loop, or schemas built from something other than a dict literal or a `pydantic` `model_json_schema()` call, won't be fully introspected — you'll get the tool detected but a blind spot on its parameter-level checks rather than a false failure. A dynamic tool *name* (not a string literal, e.g. built in a loop) means the tool is skipped entirely rather than misattributed.
- **Parses with the running interpreter's grammar (Python side).** See the spot check above — run under a Python version that matches or exceeds the syntax used in the server you're auditing.
- **Delegation resolution is Python-only, name-based (not fully import-resolved), and only covers direct calls.** If a Python tool hands off (directly or several calls deep, including through an aliased `from x import y as z` import) to a locally-defined helper that has its own try/except, the error-handling check follows that chain by function name across the whole repo — but two different functions sharing the same name in different files aren't distinguished (same simplification already accepted for the Field alias registry), and it's capped at 5 hops. It also only recognizes the helper being *called* directly (`helper(...)`) — a helper merely *passed* somewhere, e.g. `asyncio.to_thread(helper, ...)` or `executor.submit(helper, ...)`, isn't resolved, since that covers an open-ended set of "runner" call shapes rather than one well-defined pattern. The TS/JS side has no equivalent yet — a tool that hands off to a helper `.catch()`/try-block still reports a false positive there.
- **TS/JS const resolution is same-file only.** Unlike the Python side's cross-file `Field` type-alias resolution, a TS config object or Zod schema referenced via an import from another file won't be resolved — only same-file `const` references.
- **Duplicate-tool-name check has no call-graph or runtime-profile awareness.** It flags any two same-named `registerTool`/`tool`/`addTool` calls found anywhere in the source, even when they're on different server instances or gated behind mutually-exclusive runtime branches (e.g. an env-var-selected profile) that can never both register at once — a real pattern in `firecrawl-mcp-server`. Treat this warning as "worth a human glance," not a guaranteed live conflict.
- **`--fix` only fixes the fully-undocumented case, Python only.** If a docstring already documents *some* params but not all, `--fix` leaves it alone rather than risk merging into it incorrectly — you'll still see the warning, just not an auto-stub. TS/JS files aren't touched by `--fix` at all yet.

## Roadmap

- [x] TypeScript/JS server support (the official SDK's dominant language) — `registerTool`/`tool` styles, same-file const resolution, plus the community `fastmcp` package's `addTool` single-object style
- [x] Publish to PyPI
- [x] GitHub Action for one-line CI integration
- [x] `--fix` for the genuinely mechanical stuff (bare `except:`, fully-undocumented `Args:` stubs) — deliberately does *not* auto-wrap function bodies in try/except; generating a correct wrapper for arbitrary code (preserving return semantics, control flow) needs more judgment than a mechanical pass should take on

## Contributing

Issues and PRs welcome. The test suite (`pytest`) covers the analyzer directly and the CLI end-to-end against the fixtures in `examples/` — add a fixture case for anything you fix.

## License

MIT — see [LICENSE](LICENSE).
