# Postmortem: 69 bugs in mcp-doctor, and why the worst ones were silent

**Period:** 2026-08-29 (v0.1.0) to 2026-10-02 (v1.12.13) · **Format:** blameless. The question is what let each class of bug through, not who wrote it (I did). · **Sources:** the git history, the [real-world spot check](../README.md#real-world-spot-check), the [coverage page](coverage.md), and [what the first users taught me](what-users-taught-me.md)

## Summary

In its first five weeks, scanning real MCP servers surfaced 69 bugs in mcp-doctor itself. All are fixed. They came in through 62 fix commits; some commits fix more than one bug, so the class counts below are commits, not bugs.

The most common class was also the most dangerous one: **mcp-doctor couldn't see a server's tools, and nothing told anyone.** A false alarm is loud, because a maintainer complains. A missed tool is silent: the report just looks cleaner. For four weeks I found these one repo at a time. Once I measured recall against an independent count, whole frameworks turned out to be at 0%, and Go at 54%.

## Impact

- **One outside user got a confidently wrong report.** The first outside scan request found 0 of 165 tools and still graded the server A for quality and C for security ([#3](https://github.com/vishalhabib99/mcp-doctor/issues/3)). It was fixed the next morning, and the report was re-run in place.
- **Every server on an unsupported framework got a report about nothing.** Before the sweeps, every public server built on `mcp-framework` (55 repos) scanned as 0 tools, and Go servers using `mark3labs/mcp-go` were missing 811 of 1,765 tools.
- **The public leaderboard wasn't affected in practice.** Its 24 servers were re-scanned after each sweep, and no fix changed a grade. That's partly because most of its servers are ones I had already dogfooded, which is itself a bias worth naming.
- **Security false alarms cost maintainers' trust,** not correctness. For example, 59 false errors on a clean 2-tool server came from scanning its local `.venv`.

## The classes

| Class | Fix commits | How it showed up | Why it got through |
|---|---|---|---|
| **A. Couldn't see the tools** (0 found, or only some) | 26 | A report with fewer tools than the README lists, or none | Tests used registration patterns I already knew. Real servers use many more: wrappers, loops, classes, factories, aliases, other files. **Nothing measured recall.** |
| **B. Saw the tool, misread its docs** (false "undocumented") | 12 | A well-documented tool flagged as undocumented | Each framework documents parameters its own way (`Annotated`, `Field`, `exclude_args`, `Context`, helper functions, `.trim()`). Fixtures covered one style. |
| **C. Security false alarms** | 11 | `dangerous_exec` on code no agent can reach | "The repo" isn't "the server." Benchmarks, scripts, vendored copies, virtualenvs and type files got scanned as if an agent could call them. |
| **D. Counted the wrong things** | 5 | Test fixtures counted as tools, an invented `<unnamed>` tool, a variable resolved to the wrong value | Test-file rules matched too little (only `.test.ts`) or too much ("spec" inside "species"), and a guess was used where a skip was right. |
| **E. False duplicate names** | 3 | Two tools flagged as duplicates that can never run in the same server | The check compared the whole repo, not each running server. |
| **F. Version diff false alarms** | 2 | `--diff-against` reported a removed parameter that wasn't removed | "Not checked" and "has no parameters" were stored the same way. An outside tester found both ([spec thread](https://github.com/modelcontextprotocol/modelcontextprotocol/discussions/3322)). |
| **G. Environment and wording** | 3 | Results changed with the dependency version; a finding claimed a raw traceback that FastMCP never leaks | The parser grammar and Python version weren't pinned or tested, and one message was written without checking the framework's source. |

Class A breaks down by date:
- **2026-09-01 to 09-20:** 16 fixes, each found by dogfooding one repo, each fixing that one repo.
- **2026-09-29 to 10-02:** 10 fixes, after I started measuring recall. The `mcp-go` sweep alone recovered 794 tools across 57 repos in one fix.

## Root causes

1. **My tests could only confirm what I already believed.** Every fixture came from my own idea of how a server registers tools. Real code doesn't follow that idea: 26 fixes for unrecognized registration patterns had to ship after release.
2. **The feedback loop only reported one kind of error.** People report false alarms. Nobody reports a tool the checker didn't mention. So I tuned precision for four weeks, and recall had no number at all.
3. **"Found nothing" was treated as an answer.** A scan that found 0 tools produced a grade, and a perfect-looking one. An empty input should have been a failure state from day one.
4. **The environment was part of the result.** The Python version decided which files parsed (3.11 skipped 238 files in 40 repos), file walk order differed between versions, and a hash seed changed one server's warning count from run to run.
5. **The unit of analysis was wrong.** Duplicate names and security reachability were judged per repo, when what matters is the running server and what an agent can call.

## What changed, and what each change prevents

| Change | Prevents | Where |
|---|---|---|
| **Recall measured per framework against an independent count,** on every public repo that uses the framework, with every miss published | Silent class-A misses at scale | [coverage.md](coverage.md): 11,228 of 11,411 tools, 603 servers, 4 frameworks |
| **Before/after on every local target for every detection fix** (67 repos): only the intended repos may change | Fixing one server by breaking another | Every fix since 09-29 |
| Before/after runs on frozen copies, with the same interpreter for old and new code | A comparison that measures my own edits or the Python version instead of the fix (the first `mcp-go` comparison was invalid for exactly this reason, and was re-run) | Sweep method |
| **0 tools found → "Not graded"** and a `needs-look` label | A confident report about nothing | v1.12.4, v1.12.7 |
| **Weekly tool-count alarm** on the leaderboard: a drop to 0, a drop over 20%, or a server that didn't scan opens an issue | A regression that silently hides tools later | 56824fd |
| **Pre-check before inviting anyone:** scan their server first | Inviting a maintainer to a broken report (7 of the first 10 invite targets found 0 tools) | Invite process since 10-01 |
| CI on Python 3.10–3.14, the Action pinned to 3.14, sorted file walks, a bounded search instead of hash-ordered sets | Results that depend on the machine | c1bca9f, 08ad034 |
| Skip virtualenvs, vendored duplicates, benchmarks, scripts and type files | Security alarms on code no agent can reach | Class C commits |

## What's still open

- **183 tools in the independent count still aren't found,** each class listed with its reason in [coverage.md](coverage.md). Most are fastmcp tools registered from a loop or built by a factory at runtime, which static analysis can't read without guessing. Some are placeholders or other frameworks' tools that the regex count matched by mistake, so the true miss count is lower.
- **Recall is measured on 4 frameworks.** Servers that call the official SDKs directly in unusual loops haven't had a sweep yet.
- **One known gap isn't fixed:** a saved `registerTool` alias called directly with a literal name, outside any wrapper.
- **The prompt-injection heuristic is still noisy on long descriptions.** On 32 servers built with one framework, all 26 of its hits were long, legitimate descriptions. It's a warning, not a grade-breaker, but it's the next precision problem.
- **Most of the leaderboard's servers were chosen by me,** so it overstates how well mcp-doctor reads an average server. The coverage page is the honest number.

## What I'd do differently

- **Build the independent counter before the first check.** A checker has to know what it should have seen before you can trust what it says about what it saw.
- **Treat silence as a failure from day one.** "0 found" and "can't tell" both need their own states, never a grade.
- **Ask what the feedback loop can't see.** Users report what's wrong with a report, not what's missing from it. Any error that nobody can report needs its own measurement.
