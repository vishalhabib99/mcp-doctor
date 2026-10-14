---
name: fix-upstream
description: Prepare a fix for a bug mcp-doctor finds in SOMEONE ELSE'S MCP server — a local clone of an open-source repo the user doesn't own — as a ready-to-review pull-request draft. It reproduces the bug with a failing test, applies one kind of fix, runs the repo's own tests, and writes the PR description in the repo's template, then stops for the user to review. It never pushes, opens a PR, or posts anything. Use when the user asks to fix, patch or contribute a fix upstream to another project's MCP server, or to "draft a PR" for an mcp-doctor finding in a repo they cloned. Not for the user's own server (use the mcp-doctor skill), and not for bugs mcp-doctor can't see (runtime or logic bugs).
argument-hint: "[path to the cloned repo, default: current directory]"
allowed-tools: Bash(mcp-doctor *), Read, Grep, Glob
---

# fix-upstream

Turn one mcp-doctor finding in someone else's MCP server into a pull-request draft a maintainer is likely to merge. The bar is that the user would send it unchanged. **A wrong PR costs more than no PR**: it wastes a stranger's time and spends the user's reputation. When in doubt, stop and say why.

Target: `$ARGUMENTS` if given, else the current directory. It must be a git clone of a repo the user doesn't own. If it's the user's own server, use the `mcp-doctor` skill instead.

Runner: as in the `mcp-doctor` skill (`mcp-doctor`, else `uvx --from mcp-server-lint mcp-doctor`, else `pipx run --spec mcp-server-lint mcp-doctor`), called below `MCPD`. This skill needs `--fix-only`; if `MCPD --help` doesn't list it, ask the user to upgrade `mcp-server-lint` and stop.

## Scope: one kind of fix per PR

Only the **`none-default`** fix is in scope today: a parameter written `name: str = None` advertises `"default": null` on a field whose type excludes null, so a client that sends the default explicitly gets a validation error. This was checked by replaying 15 merged upstream null-default fixes (the patch changed the same parameters the maintainers merged in all 15) and a 19-repo forward test. Other findings (missing descriptions, stdout writes, bare `except:`) need the maintainer's judgment or the user's own words. Mention them in your report, but don't draft them.

## 1. Check before touching anything

Stop with a one-line reason if any of these fail. If you can't run a check (no `gh`, no network), say which one went unchecked in your final report, and keep going only for a local draft:

1. **Not a duplicate.** `gh search issues --repo OWNER/REPO --include-prs "null"` (also `Optional`, `nullable`). An open or recently closed issue/PR on the same bug means no draft. Tell the user and link it.
2. **The user's open-PR rule.** If the user already has an open PR in this repo (`gh pr list --repo OWNER/REPO --author @me`), don't start a second one. Note it as a follow-up.
3. **Contribution rules.** Read CONTRIBUTING (and any PR template, CLA, DCO, or "issues only" / "no outside PRs" note). If outside PRs aren't accepted, stop. If a CLA must be signed, say so: only the user can sign it.

## 2. Plan the patch, then judge every file

Run `MCPD --diff --fix-only none-default <path>`. Read every file in the diff and drop any file that isn't a live server the maintainers ship:

- **Test data, fixtures, benchmark corpora, examples, samples, templates, vendored copies** are not fixes, even if they register real tools. Read the README and the directory names. Example from the forward test: a detection benchmark kept 78 synthetic "benign tool" servers under `source_codes/mcp_servers_0/`, which made 61 of the 64 flagged files fixtures.
- **Not actually an MCP tool**: a function with the same signature that isn't registered (commented-out `mcp.tool(...)` lines, never imported), or a different framework's `@tool` (LangChain etc.). Leave it alone even though the same bug applies. Say so in the report.
- Confirm the registration yourself: the decorator or `mcp.tool(...)` call, and which SDK (`mcp.server.fastmcp`, `mcp.server.mcpserver`, or `fastmcp`).

**Size routing.** If what's left changes more than ~30 parameters or ~10 files, don't draft a PR. Draft an **issue** instead, with the repro, the count, and an offer to send the PR, so the maintainer chooses how big a change to take.

**String parameters that take JSON text.** On the official SDK (`mcp.server.fastmcp`, `mcp.server.mcpserver`), any annotation other than exactly `str` makes the server JSON-decode a string argument. So `str | None` would turn a `'{...}'` or `'[...]'` value that works today into a validation error, and the literal text `"null"` into `None`. The CLI leaves such parameters alone when their name or docs mention JSON, and lists them after "Left N None-default parameter(s) as they are". Still read every `str` parameter in the diff: if it carries JSON (or JSON-looking text) and the docs don't say so, revert that hunk too. Those parameters belong in an issue, not this PR. Standalone `fastmcp` doesn't pre-parse, so this doesn't apply there.

Then apply: `MCPD --fix --fix-only none-default <path>`. Revert (`git checkout -- <file>`) any file you dropped above. The diff must touch only annotations: `T` → `T | None` (or `Optional[T]` where the file already imports it), nothing else.

## 3. Prove it: a failing test first

Write one regression test in the repo's own test style and location: list the server's tools through its real `FastMCP`/`MCPServer` object, and assert that every parameter whose schema says `"default": null` also accepts null (its schema type includes `"null"`, or an in-memory call with that argument set to `None` passes validation). Look at how the repo's tests already build the server and copy that. Never start a real backend, log in to a service, or call a tool that has side effects; schema checks and argument validation are enough.

On the official SDK, also check that a `str` parameter you changed still accepts a JSON-looking string such as `'{"a": 1}'` after the fix, since step 2 explains why it might not. Run it on the original code (`git stash`), and it must **fail**. Then run it on the patch, and it must **pass**. If it doesn't fail on the original, the bug doesn't apply here (wrong SDK path, custom validation): drop the draft and say so.

## 4. Run the repo's tests

Install the way the repo says (uv / pip / its Makefile), into a venv inside the clone, and run the full suite on the patch and on the original:

- Same results on both → fine. Report environment-only errors as such.
- A **schema or contract snapshot** test fails (a stored `tools/list` or input-schema file): the change is intended. Regenerate the snapshot the way CONTRIBUTING says, and name the schema change in the PR body.
- Any other new failure → drop the draft and report it.
- Tests that hang or need hardware or credentials → say they didn't run. Never claim they passed.

## 5. Draft — then stop

Commit on a new branch `fix/optional-args-accept-null`. Use the repo's commit message style and only the user's name as author (no co-author trailer). Write the PR body in the repo's PR template if it has one (match its language), covering:

- what breaks, in one or two sentences, with one concrete tool and argument;
- the fix (annotations only, N parameters in M tools);
- the test (fails before, passes after) and the suite result before and after;
- anything left out on purpose (fixtures, unregistered functions) and why.

Save the body to `PR_BODY.md` in the clone's parent directory, not inside the repo. Then report: repo, the counts, test results, what was dropped and why, and the exact `git push` / `gh pr create` commands the user would run. **Don't push, don't open the PR, don't comment.** The user reviews and sends it.

## What not to do

- Don't bundle fix kinds, reformat, re-sort imports, or "tidy" nearby code.
- Don't touch code outside the MCP server to make tests pass.
- Don't draft for a bug you couldn't reproduce, even if the scanner is sure.
- Don't nudge, follow up, or post on the user's behalf.
