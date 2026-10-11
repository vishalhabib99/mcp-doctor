# Plugin evals

Eval suite for the Claude Code plugin (`skills/mcp-doctor`), run with `claude plugin eval` (Claude Code 2.1.269+).

| Case | Checks |
|---|---|
| `scan-bad-server` | Skill fires, runs the CLI, reports Quality 13% F and Security 100% A separately, ≤5 findings, edits nothing |
| `fix-bad-server` | Applies fixes, shows before → after, writes descriptions the code backs up (`run` must not be described as executing a command — it only returns `"ran"`), creates no LICENSE |
| `clean-good-server` | Reports A / A on a clean server and proposes no fixes |
| `not-mcp-code` | Skill does not fire on unrelated Python work |
| `mcp-config-not-server` | Skill does not fire when the user wants to *use* an MCP server, not build one |

Each scan case seeds its workspace from the repo's `examples/` through `scaffold.sh` (files inlined, so the case doesn't depend on where the scaffold runs). The scan cases need Bash for the CLI and the fix case needs Edit, both granted on the command line:

The eval sandbox blocks network access and, in our runs, executing the installed CLI, so the report graders accept either the real grades or a plain "scanner didn't run" with no invented grade (the skill's rule for that case). The real-grade path is covered end to end in a normal session (see below). To give the CLI its best chance, have the `mcp-server-lint` package from PyPI installed so the skill finds `mcp-doctor` on PATH, then from this repo:

```
cd plugin
claude plugin eval . --scaffold \
  --allow-tools "Bash(mcp-doctor *)" Edit
```

Every run is a full Claude session on your own account. The default is 3 runs per case with and without the plugin, so about 30 sessions; add `--runs 1` for a quick smoke test.

## End-to-end check (real session, real grades)

```
# in an empty folder holding a copy of examples/bad_server/server.py:
claude -p "Check the MCP server in this folder and fix everything you safely can. You have my go-ahead to apply the fixes without asking first." \
  --plugin-dir path/to/mcp-doctor/plugin --allowedTools "Bash(mcp-doctor *)" Edit Read Glob Grep Skill --permission-mode dontAsk
```

Last run (2026-10-11): reported Quality 13% F / Security 100% A, fixed both tools, re-scanned to 42% (remaining points are the missing README/LICENSE/tests/deps, which it listed but didn't create), and described `run` as a placeholder that ignores `cmd`. The same run on `clean-good-server` reported A / A and changed nothing.
