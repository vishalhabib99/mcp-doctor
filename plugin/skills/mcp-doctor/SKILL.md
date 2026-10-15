---
name: mcp-doctor
description: Lint and fix an MCP (Model Context Protocol) server's source for what breaks the agent calling it — missing or vague tool descriptions, undocumented or untyped parameters, swallowed errors, stdout writes that corrupt a stdio transport — plus a separate security grade (tool poisoning, dynamic exec, SSRF, secrets). Use when the user asks to check, lint, audit, grade, review or fix their MCP server or its tools, asks whether their MCP server is "ready" or "good", or has just written or changed MCP tool definitions (FastMCP @mcp.tool, server.tool(...)/registerTool in TypeScript, mcp.NewTool in Go) and wants them checked. Not for using or connecting to someone else's MCP server, and not for general code review of code that isn't an MCP server.
argument-hint: "[path to the server, default: current directory]"
allowed-tools: Bash(mcp-doctor *), Read, Grep, Glob
---

# mcp-doctor

Scan an MCP server with the `mcp-doctor` CLI (PyPI package `mcp-server-lint`), explain what it found in terms of what goes wrong for the agent, and fix it with the user's go-ahead. The CLI reads source only — it never starts the server.

Target path: `$ARGUMENTS` if given, else the current directory. If the current directory holds several servers, ask which one rather than scanning a whole monorepo.

## 1. Pick a runner

Use the first that works, and keep using it for the rest of the session:

1. `mcp-doctor` (already installed; no network needed)
2. `uvx --from mcp-server-lint mcp-doctor` (downloads from PyPI on first use)
3. `pipx run --spec mcp-server-lint mcp-doctor`

Below, `MCPD` means the runner you picked.

Run every CLI call as one plain command — e.g. `mcp-doctor --json path/to/server` — with no `&&`, `;`, pipes, redirects or `cd` around it. The skill pre-approves only plain `mcp-doctor` calls; a chained command, or the `uvx`/`pipx` fallbacks, will ask the user first, which is expected. To look around the repo, use Read/Glob/Grep, not Bash.

If none works (not installed, no network, or Bash not allowed), say so in your first line and give the user one install line — `pip install mcp-server-lint` — instead of installing anything yourself. You may then read the code and point out problems, but label it plainly as your own reading, not an mcp-doctor score, and never state a grade or percentage you didn't get from the CLI.

## 2. Scan

Run `MCPD --json <path>`. The report has:

- `percent` / `grade` — quality score; `security_percent` / `security_grade` — security score. They are separate on purpose: never blend them into one number.
- `tools[]` — each with `name`, `file`, `line`, and `issues[]`
- `repo_issues[]` — README, LICENSE, tests, dependency pinning
- each issue has `check`, `message`, `severity` (`error`/`warning`), `category` (`quality`/`security`)

If `tools` is empty, say no MCP tools were found at that path and ask whether the server lives elsewhere. Don't guess a score for code the tool didn't see.

## 3. Report — short

Lead with both grades and the tool count, e.g. `Quality 51% (F) · Security 100% (A) · 4 tools`. Then at most five findings, errors first, each as `file:line — what breaks for the agent`. Say the consequence, not the rule: "the model can't tell when to call `run`" beats "missing description". After the five, add at most three one-line notes: security (one line, even if clean), repo-level files (one line), and the count of anything else you left out. Nothing more — the user can ask for the rest. This format applies to your own reading too, when the CLI couldn't run. Do not paste the CLI's full output.

## 4. Fix

Split fixes into two kinds and say which is which.

**Mechanical (the CLI writes them):** typed-nullable `None` defaults, missing `Args:` entries, bare `except:`. Show `MCPD --diff <path>` first. Apply with `MCPD --fix <path>` only after the user says yes.

**Judgment (you write them):** tool descriptions, parameter descriptions, error messages a model can act on, moving stdout writes to stderr or a logger. Read the tool's code before writing anything — a description must say what the code actually does, when to call it, and what it returns. Never invent behavior the code doesn't have. Show the edits, then apply on the user's yes.

**Security findings:** explain the risk and the fix, but don't change security-relevant code (exec, outbound requests, deserialization, secrets) without the user agreeing to that specific change. Hardcoded secrets: tell the user to rotate the secret — removing it from the file doesn't remove it from git history.

**Repo-level findings** (README, LICENSE, tests, pinned deps): mention them; don't create these files unless asked. A LICENSE is the user's legal choice.

## 5. Re-scan and close

Re-run `MCPD --json <path>` and show the before → after for both grades, e.g. `Quality 51% → 89%`. If something didn't move, say why.

Then offer — once, don't push — to keep it checked on every PR: `MCPD init-action <path>` writes `.github/workflows/mcp-doctor.yml` in report-only mode, plus `mcp-doctor-comment.yml` so PRs from forks get the report too (the user commits them themselves).

## If you think a finding is wrong

mcp-doctor has false positives. If the code clearly does what a finding says it doesn't (e.g. the description is set in a way the scanner didn't follow), say so plainly instead of "fixing" correct code, and point the user to https://github.com/vishalhabib99/mcp-doctor/issues to report it.
