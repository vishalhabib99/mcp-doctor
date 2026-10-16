# mcp-doctor for Claude Code

Lint the MCP server you're building for what breaks the agent calling it, then fix what it finds. Works on Python, TypeScript and Go servers.

Install:

```
/plugin install mcp-doctor --marketplace vishalhabib99/mcp-doctor
```

Then ask "check my MCP server", or run `/mcp-doctor:mcp-doctor path/to/server`. The skill:

1. runs the `mcp-doctor` CLI on your server's source (it reads code, never starts the server) and reports a quality grade and a separate security grade, plus the top five findings in terms of what goes wrong for the agent;
2. shows the mechanical fixes as a diff and applies them when you say yes;
3. writes the fixes the CLI can't (tool and parameter descriptions, actionable error messages, stdout writes on a stdio server) after reading what each tool actually does, and never describes behavior the code doesn't have;
4. re-scans and shows the grades before and after;
5. if you ask whether the server is ready to ship, offers `mcp-doctor verdict --run "<start command>"`, which launches it, calls its read-only tools with bad and valid input, and returns SHIP, FIX-FIRST or BLOCK. It runs only after you say yes, since it starts your server.

Security findings are explained, not silently rewritten.

### Fixing someone else's server: `fix-upstream`

Cloned an open-source MCP server that mcp-doctor flags? Ask "prepare an upstream fix as a PR draft" (or run `/mcp-doctor:fix-upstream path/to/clone`). The skill checks for an existing issue or PR and the repo's contribution rules. It applies one kind of fix (today: optional arguments that reject an explicit null), leaves out fixtures, examples and unregistered functions, and writes a regression test that fails before the fix and passes after. It runs the repo's own tests before and after, then commits on a local branch and writes the PR description. **It never pushes, opens a PR or posts anything**: you review and send it. Big changes (more than ~30 parameters) become an issue draft instead, so the maintainer chooses the scope.

Why trust the patch: replayed on the code from before 15 merged upstream fixes, the patch matched what the maintainers merged in every case. A 19-repo forward test is what added the fixture filter, the size limit and the snapshot-test step.

## Requirements

The `mcp-doctor` CLI, from the [`mcp-server-lint`](https://pypi.org/project/mcp-server-lint/) package on PyPI. If it isn't installed, the skill tries `uvx` or `pipx` (each asks you first), or tells you how to install it. If the CLI can't run, the skill says so and doesn't make up a grade.

## Evidence

- The CLI finds 15,755 of 15,977 tools across 805 public servers, with every miss listed in [coverage.md](https://github.com/vishalhabib99/mcp-doctor/blob/main/docs/coverage.md).
- The skill ships with an eval suite in [`evals/`](evals/): when it should and shouldn't run, that it reports the real grades, that it doesn't "fix" a clean server, and that it doesn't write tool descriptions the code doesn't back up.

More: [github.com/vishalhabib99/mcp-doctor](https://github.com/vishalhabib99/mcp-doctor). MIT licensed.
