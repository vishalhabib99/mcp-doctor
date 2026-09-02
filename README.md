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

A seventh pass against `GLips/Figma-Context-MCP` (15k+ stars, TypeScript) found the TS analyzer reporting **0 tools** on a repo with two well-built, widely-used tools — a false 80%/B with no tools listed at all. Root cause: both are registered as `server.registerTool(getFigmaDataTool.name, { description: getFigmaDataTool.description, ... }, handler)`, where `getFigmaDataTool` is an exported `{ ... } as const` object literal defined in a separate file — a cross-file member-expression property lookup, not a same-file `const` reference, which is all the resolver previously understood (documented below as a known limitation until now). Fixed by adding a repo-wide, name-based registry of `const NAME = {...}` object literals (same simplification as the Python side's Field-alias registry) and teaching the resolver to follow member-expression property access into it, plus unwrap TypeScript's `as const`/`satisfies` assertions along the way. Re-verified: 0→2 tools found, one clean pass and one correctly-flagged real gap — `download_figma_images`'s description is built by a runtime function call (`getDescription(imageDir)`, not a property access), which is genuinely dynamic and correctly still reported as unresolvable rather than guessed at. 2 new regression tests (49 total), confirmed no regression on the exa-mcp-server and firecrawl-mcp-server repos from earlier passes.

An eighth pass against `hangwin/mcp-chrome` (12k+ stars, TypeScript) found the TS analyzer reporting **0 tools** on a repo with 27 real, well-documented ones — a hollow false 100%/A. Root cause was architectural, not a small parsing gap: the repo builds its server on the low-level `Server` SDK, wiring up `server.setRequestHandler(ListToolsRequestSchema, () => ({ tools: [...TOOL_SCHEMAS, ...dynamicTools] }))` with a static array of raw-JSON-Schema `Tool` objects, rather than any of the `registerTool`/`.tool()`/`.addTool()` call-site styles already supported. Unlike the earlier `playwright-mcp`/`XcodeBuildMCP` cases (correctly ruled out as out of scope — their tool definitions live outside the repo entirely), this repo's tool metadata is fully present and staticaly analyzable, so it was worth adding real support rather than declining: a new code path finds the handler's `{ tools: [...] }` response literal, follows `...constArraySpread`s (repo-wide, via the same registry used for member-expression resolution) into their elements, and checks each tool's description and raw-JSON-Schema `properties[x].description` — while *not* checking error handling for this style, since there's no per-tool handler closure to inspect (one generic dispatcher serves every tool by name here, proxying over native messaging to the Chrome extension process where the real logic lives). Also found and fixed two bugs in the new code while verifying it against the real repo: the same static array is spread into two separate transport entrypoints (stdio and HTTP), which without dedup reported each tool twice; and tool locations were initially reported at the wrong file (the call site instead of the array's actual definition site). 2 new regression tests (51 total). Verified: 0→27 tools found, a real 100%/A this time.

A ninth pass against `BeehiveInnovations/pal-mcp-server` (11k+ stars, Python) surfaced a real bug in mcp-doctor's own low-level-Tool-constructor check, not just another architectural gap. The repo defines each of its 17 real tools as a class (`ChatTool`, `DebugIssueTool`, etc.) registered in a `TOOLS` dict, and builds the actual MCP `Tool(...)` objects in a loop at list-time — `Tool(name=tool.name, description=tool.description, inputSchema=tool.get_input_schema())` — so the one `Tool(...)` call site mcp-doctor found had a genuinely dynamic name it couldn't resolve. Rather than skip it (the correct, established behavior for a dynamic name — see the TS analyzer's identical handling of a `t.name` loop variable), the Python-side check fell back to a fabricated `"<unnamed>"` tool with a nonsensical "no description" error, worse than reporting nothing. Fixed by skipping instead of guessing, with a regression test (52 total). Full support for this class-based tool pattern — resolving `get_name()`/`get_description()` across a real Python class hierarchy, and introspecting `get_input_schema()` when it's built by imperative code rather than a literal dict — was correctly left undone rather than forced: unlike the TS static-array case, this would mean walking method resolution across base classes and interpreting arbitrary schema-building code, a much larger and more failure-prone undertaking than a scoped fix; see Known limitations below.

A tenth pass against `wonderwhy-er/DesktopCommanderMCP` (9k+ stars, TypeScript) — also on the low-level `Server` SDK's `setRequestHandler(ListToolsRequestSchema, ...)` style added in the eighth pass — found two more real gaps in that new support, plus surfaced one genuine, real gap in the target repo itself. First: the handler returns `{ tools: filteredTools }`, where `filteredTools = allTools.filter(tool => shouldIncludeTool(tool.name))` — a runtime filter over the real base array. The resolver didn't know how to look through a `.filter(...)` call, so the whole 26-tool list was invisible (0 tools, a hollow 80%/B). Fixed by resolving straight through `.filter(...)` to its base array — filtering never invents or changes a tool's definition, only its runtime visibility, so for audit purposes the base array is the right thing to check. Second: every tool's description is a template literal with one interpolated suffix (`` `Get the complete server configuration... ${CMD_PREFIX_DESCRIPTION}` ``) — `_string_value` was discarding the *entire* string whenever a template literal had any `${...}`, which, once tools were visible at all, turned into 26 false "no description" errors on a repo that documents its tools extensively. Fixed to join the literal fragments and drop only the interpolated part, so real (if partial) description text is no longer thrown away just because part of it is dynamic. Re-verified: 0→26 tools, real 98%/A. Along the way, also added support for the `zodToJsonSchema(SomeArgsSchema)` idiom (the well-known `zod-to-json-schema` package) — `inputSchema` here isn't a raw JSON-Schema literal but a runtime conversion of a real Zod schema, so param docs unwrap to that Zod schema rather than going blind. That unwrap surfaced a genuine, real gap in the target repo, not a mcp-doctor false positive: none of its Zod schemas use `.describe(...)` on any parameter (their docs live entirely in prose on each tool's top-level description instead) — correctly left as an accurate finding rather than filed upstream, since documenting per-tool instead of per-param is a defensible stylistic choice, not a clear bug. 2 new regression tests (54 total); no change on the four previously-verified TS repos (mcp-chrome, exa-mcp-server, firecrawl-mcp-server, Figma-Context-MCP), re-checked live.

An eleventh pass against `MODSetter/SurfSense` (16k+ stars, Python; audited via its `surfsense_mcp` subdirectory, the actual MCP server component of a larger full-stack app) found every one of its 28 real tools false-flagged for missing error handling — a suspicious 100% hit rate on an actively-maintained repo, worth investigating rather than trusting. The cause: this codebase's entire error-handling architecture is built on delegating to *object methods* — a tool calls a bare helper function, which calls `client.request(...)` or `context.resolve(...)`, where the real try/except actually lives — but the delegation registry's `_direct_call_names` only ever recognized bare `helper(...)` calls (`ast.Name`), never `obj.method(...)` (`ast.Attribute`), so none of that chain was ever followed, even though the registry already indexes methods by name (`ast.walk` doesn't distinguish a class body from module level — only the call-site extraction was the gap). Fixed by also collecting attribute-call names, resolved through the same name-based registry already used for bare functions — a consistent extension of an already-accepted simplification, not a new category of imprecision, though a generic method name (`get`, `run`, `close`) now carries a higher name-collision risk than a distinctively-named bare function, called out explicitly in Known limitations below. Re-verified: 89%→98%/A, all 28 tools correctly cleared. 1 new regression test (55 total); no regression on `ha-mcp` (still 88 tools/97%/A), `pal-mcp-server` (still 0 tools/100%/A — unaffected, correctly), or `tradingview-mcp` (still 39 tools/99%/A, the same two genuine remaining gaps — `top_losers`'s missing param docs and `financial_news`'s helper-passed-as-argument idiom — still correctly flagged, not incorrectly cleared).

## Known limitations

- **AST-based, single-pass.** Tools constructed dynamically in a loop, or schemas built from something other than a dict literal or a `pydantic` `model_json_schema()` call, won't be fully introspected — you'll get the tool detected but a blind spot on its parameter-level checks rather than a false failure. A dynamic tool *name* (not a string literal, e.g. built in a loop) means the tool is skipped entirely rather than misattributed.
- **Class-based tool registries (Python low-level `Server`) aren't introspected at all.** A common pattern for larger servers: one class per tool exposing `get_name()`/`get_description()`/`get_input_schema()`, instantiated into a registry dict, and marshaled into `Tool(...)` objects in a loop at list-time (`Tool(name=tool.name, description=tool.description, ...)`). The name/description/schema are all dynamic at that call site by construction, so — same as any other dynamic name — the tool is correctly skipped rather than misreported, but that means these tools aren't audited at all, not even for description length or param docs. Unlike the TS `ListToolsRequestSchema` static-array style (which *is* supported), the underlying values here are typically returned from real methods across a class hierarchy, sometimes built by imperative code rather than a literal — reliably resolving that is a materially bigger undertaking than a scoped fix, and hasn't been attempted.
- **Parses with the running interpreter's grammar (Python side).** See the spot check above — run under a Python version that matches or exceeds the syntax used in the server you're auditing.
- **Delegation resolution is Python-only, name-based (not fully import-resolved or type-resolved), and only covers direct calls.** If a Python tool hands off (directly or several calls deep, including through an aliased `from x import y as z` import, and including via an object method like `client.request(...)` as well as a bare function) to a locally-defined helper that has its own try/except, the error-handling check follows that chain by name across the whole repo — but two different functions or methods sharing the same name aren't distinguished (same simplification already accepted for the Field alias registry), which is a materially higher risk for a very common method name (`get`, `run`, `close`) than for a distinctively-named bare function, and it's capped at 5 hops. It also only recognizes the helper being *called* directly (`helper(...)`/`obj.helper(...)`) — a helper merely *passed* somewhere, e.g. `asyncio.to_thread(helper, ...)` or `executor.submit(helper, ...)`, isn't resolved, since that covers an open-ended set of "runner" call shapes rather than one well-defined pattern. The TS/JS side has no equivalent yet — a tool that hands off to a helper `.catch()`/try-block still reports a false positive there.
- **TS/JS cross-file resolution is name-based, not fully import-resolved.** A tool's name/config/schema referenced via `fooTool.name`-style member expressions on an exported object literal (including through `as const`/`satisfies`) is resolved repo-wide by matching the object's declared name — two different objects sharing the same name in different files aren't distinguished (same simplification already accepted for the Python side's Field-alias and delegation registries). A description or schema built by a runtime *function call* (e.g. `fooTool.getDescription(x)`) is genuinely dynamic and is correctly left unresolved, not guessed at.
- **The low-level `Server` SDK style (`setRequestHandler(ListToolsRequestSchema, ...)`) is not checked for error handling.** There's no per-tool handler closure in this style — one generic dispatcher, keyed by tool name, serves every tool (and may proxy the real work to an entirely different process, as with a Chrome-extension-backed server), so flagging "no try/catch" per tool would be structurally meaningless. Only description and JSON-Schema `properties[x].description` are checked for this style.
- **Duplicate-tool-name check has no call-graph or runtime-profile awareness.** It flags any two same-named `registerTool`/`tool`/`addTool` calls found anywhere in the source, even when they're on different server instances or gated behind mutually-exclusive runtime branches (e.g. an env-var-selected profile) that can never both register at once — a real pattern in `firecrawl-mcp-server`. Treat this warning as "worth a human glance," not a guaranteed live conflict.
- **`--fix` only fixes the fully-undocumented case, Python only.** If a docstring already documents *some* params but not all, `--fix` leaves it alone rather than risk merging into it incorrectly — you'll still see the warning, just not an auto-stub. TS/JS files aren't touched by `--fix` at all yet.

## Roadmap

- [x] TypeScript/JS server support (the official SDK's dominant language) — `registerTool`/`tool` styles, cross-file const/member-expression resolution, the community `fastmcp` package's `addTool` single-object style, and the low-level `Server` SDK's static `setRequestHandler(ListToolsRequestSchema, ...)` style
- [x] Publish to PyPI
- [x] GitHub Action for one-line CI integration
- [x] `--fix` for the genuinely mechanical stuff (bare `except:`, fully-undocumented `Args:` stubs) — deliberately does *not* auto-wrap function bodies in try/except; generating a correct wrapper for arbitrary code (preserving return semantics, control flow) needs more judgment than a mechanical pass should take on

## Contributing

Issues and PRs welcome. The test suite (`pytest`) covers the analyzer directly and the CLI end-to-end against the fixtures in `examples/` — add a fixture case for anything you fix.

## License

MIT — see [LICENSE](LICENSE).
