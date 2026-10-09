# MCP server census, October 2026

Scripts and aggregate numbers behind the report ["I scanned 3,923 MCP servers. 1 in 4 tools leaves the model guessing."](https://dev.to/vishalhabib99/i-scanned-3923-mcp-servers-1-in-4-tools-leaves-the-model-guessing-1n99)

**What it is:** one static mcp-doctor pass over every public MCP server repo that GitHub search could find: Python, TypeScript, JavaScript and Go; 20+ stars; no forks or archived repos. 6,054 repos discovered on 2026-10-08, of which 3,923 have at least one tool (147,646 tools). Scanned 2026-10-08 and 2026-10-09 (ET); every row comes from mcp-doctor 0.15.6 (v1.15.6).

**What it isn't:** a runtime test, a security audit, or a ranking. No code from the scanned repos was executed and no hosted endpoint was called. Per-repo results aren't published here, so this doesn't read as a public grade list of other people's projects. If your repo is in the data and you want its report, [open a scan request](https://github.com/vishalhabib99/mcp-doctor/issues/new?template=scan-request.yml).

## Files

| file | does |
|---|---|
| `discover.py` | GitHub repo search over MCP topics and phrases, sliced by star range to stay under the 1,000-result cap → `discovered.jsonl` |
| `scan.py` | shallow-clones each repo, runs `mcp-doctor --json`, records counts per check, deletes the clone → `results.jsonl` (resumable) |
| `analyze_report.py` | the report's numbers → `report-numbers.md`. Refuses to run if rows come from more than one mcp-doctor release |
| `followup.py` | reruns repos with a `prompt_injection` or `tool_name` finding to split each into its two variants (trigger phrase vs long description; invalid characters vs duplicate name) |
| `report-numbers.md` | the aggregate output used in the report (generated 2026-10-09) |

## Reproduce

Needs `gh` (authenticated), git, and Python 3.12+ with mcp-doctor and tree-sitter. Python 3.11 can't parse PEP 695 syntax, so it silently drops tools.

```bash
python3.12 -m venv venv && venv/bin/pip install mcp-server-lint==0.15.6 tree_sitter tree_sitter_typescript
python3 discover.py
CENSUS_PY=$PWD/venv/bin/python CENSUS_TMP=/tmp/census_clones python3 scan.py --workers 8
python3 analyze_report.py
CENSUS_PY=$PWD/venv/bin/python CENSUS_TMP=/tmp/census_clones python3 followup.py
```

GitHub search results and default branches change daily, so a rerun will differ slightly.

## Known gaps

- 139 repos weren't scanned: 110 over the 400 MB size limit, 21 failed to clone, and 8 crash mcp-doctor (a recursion limit on very deeply nested TypeScript/Go, being fixed).
- 715 repos with no tools found still declare an MCP SDK. That's the upper bound on servers mcp-doctor missed; many are clients. See [coverage](../../docs/coverage.md) for measured recall.
