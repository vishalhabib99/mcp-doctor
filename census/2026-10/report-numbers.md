# Report numbers (single version)

Scanner: {'0.15.6': 6054}

## Totals
- Discovered: 6054
- Scanned: 6054 (errors/skipped: 139)
- With ≥1 tool: 3923 repos, 147646 tools (median 12 tools/repo, max 10170)
- 0 tools: 1992, of which 715 declare an MCP SDK (upper bound on scanner misses)
- Errors: {'clone_failed': 21, 'skipped_too_large': 110, 'mcp_doctor_crash': 8}

## Grades (repos with ≥1 tool)
- Quality: A: 2842 (72.4%), B: 843 (21.5%), C: 148 (3.8%), D: 72 (1.8%), F: 18 (0.5%)
- Security: A: 3094 (78.9%), B: 468 (11.9%), C: 161 (4.1%), D: 83 (2.1%), F: 117 (3.0%)

## Per-tool quality checks
error_handling denominators exclude Go-only repos (check not applied to Go).
| check | repos | % repos | tools flagged | % tools |
|---|---|---|---|---|
| param_docs | 1883 | 48.0% | 36038 | 24.4% |
| error_handling | 1525 | 41.1% | 23009 | 16.1% |
| description | 513 | 13.1% | 3665 | 2.5% |
| types | 45 | 1.1% | 312 | 0.2% |
| bare_except | 39 | 1.0% | 120 | 0.1% |
| url_format_hint | 352 | 9.0% | 1736 | 1.2% |
| none_default_type | 151 | 3.8% | 1387 | 0.9% |
| indistinguishable_description | 109 | 2.8% | 985 | 0.7% |
| prompt_injection | 823 | 21.0% | 5885 | 4.0% |

## Repo-level checks (repos with ≥1 tool)
| check | repos | % |
|---|---|---|
| repo:readme | 2096 | 53.4% |
| repo:tests | 928 | 23.7% |
| repo:license | 303 | 7.7% |
| repo:packaging | 340 | 8.7% |
| repo:tool_name | 390 | 9.9% |
| repo:parse_error | 37 | 0.9% |

## Security (repos with ≥1 tool)
| check | kind | repos | % |
|---|---|---|---|
| repo:unsafe_deserialization | precise | 139 | 3.5% |
| repo:secrets | precise | 175 | 4.5% |
| repo:unpinned_dependency | precise | 155 | 4.0% |
| repo:ssrf | heuristic: worth a look | 2219 | 56.6% |
| repo:dangerous_exec | heuristic: worth a look | 1559 | 39.7% |
| prompt_injection | mixed: trigger phrase (precise) + >500 chars (heuristic), split below | 823 | 21.0% |

## Concentration
- Top 10% of repos (392) hold 65.4% of all per-tool quality flags (73143 total).
- Repos with 0 per-tool flags: 971 (24.8%)

## Stars buckets
| stars | repos | median quality % | % null-default | % any error-severity | % param_docs |
|---|---|---|---|---|---|
| 20–99 | 2482 | 94 | 3.7% | 11.1% | 45.9% |
| 100–499 | 918 | 94 | 3.5% | 15.6% | 48.6% |
| 500–999 | 202 | 95 | 4.5% | 11.4% | 51.0% |
| 1000–4999 | 213 | 94 | 6.6% | 20.7% | 60.6% |
| 5000–∞ | 108 | 94 | 2.8% | 20.4% | 62.0% |

## By SDK (declared dependency, repos can have several)
| sdk | repos | tools | median quality % | % null-default | % param_docs | % error_handling |
|---|---|---|---|---|---|---|
| ts-sdk | 1691 | 66949 | 95 | 0.4% | 42.7% | 38.4% |
| mcp-python | 1285 | 63520 | 93 | 8.2% | 60.0% | 43.7% |
| fastmcp | 515 | 32600 | 93 | 12.0% | 55.3% | 50.9% |
| none detected | 507 | 12509 | 93 | 2.0% | 53.5% | 40.4% |
| mcp-go | 128 | 6036 | 100 | 1.6% | 8.6% | n/a (Go) |
| go-sdk | 104 | 4072 | 100 | 1.0% | 21.2% | n/a (Go) |
| fastmcp-ts | 26 | 2079 | 91 | 3.8% | 42.3% | 84.6% |
| mcp-framework | 3 | 22 | 100 | 0.0% | 0.0% | 0.0% |

## Robustness (giant repos)
- Largest repo: mcparmory/registry with 10170 tools; top 10 repos hold 19.4% of all tools.
| check | tool % (all) | tool % excl. top 1% repos by tools | mean per-repo share |
|---|---|---|---|
| param_docs | 24.4% | 26.5% | 25.9% |
| error_handling | 16.1% | 20.0% | 22.9% |
| description | 2.5% | 3.4% | 5.4% |
| types | 0.2% | 0.3% | 0.3% |
| bare_except | 0.1% | 0.1% | 0.1% |
| url_format_hint | 1.2% | 1.2% | 1.7% |
| none_default_type | 0.9% | 1.0% | 0.8% |
| indistinguishable_description | 0.7% | 0.4% | 0.3% |
| prompt_injection | 4.0% | 5.0% | 5.7% |

- none_default_type is a Python-only check: 151 of 1765 repos with Python tools (8.6%), 70320 Python tools.

## Stars, controlled for size
| stars | repos | median tools | % any param_docs | mean per-repo param_docs share |
|---|---|---|---|---|
| 20–99 | 2482 | 12 | 45.9% | 25.7% |
| 100–499 | 918 | 13 | 48.6% | 25.7% |
| 500–999 | 202 | 12 | 51.0% | 24.6% |
| 1000–4999 | 213 | 17 | 60.6% | 29.6% |
| 5000–∞ | 108 | 16.5 | 62.0% | 28.1% |

## Detail pass (followup.py)
- Repos detailed: 1111 (errors: 0)
- prompt_injection, trigger phrase: 13 repos, 17 tools (every hit read by hand; see report)
- prompt_injection, description >500 chars: 816 repos, 5874 tools
- tool_name, invalid characters/length: 33 repos (includes confirmed false positives: definition objects whose registered name differs from their `name` field)
- tool_name, duplicate name: 367 repos (no call-graph awareness; can be separate server instances)
