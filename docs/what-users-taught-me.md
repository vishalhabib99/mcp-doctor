# What the first outside users taught me

**As of 2026-10-02.** Every source is public: the scan-request issues, the [MCP spec feedback thread](https://github.com/modelcontextprotocol/modelcontextprotocol/discussions/3322), and the upstream issues linked below. The sample is tiny, and this page says so wherever it matters.

## The short version

- **Two outside people have used mcp-doctor directly.** One tested it with an AI agent and found two real bugs. The other suggested a new check, then sent three of their own servers through the no-install scan.
- **The first real scan exposed the worst bug the tool has had:** it found 0 of 165 tools on a server and still gave it a grade. I had measured false alarms carefully, but never whether the tool could *see* a server at all.
- **That one report changed the roadmap.** I stopped adding checks and started measuring recall, framework by framework: from 0% on some frameworks to [15,755 of 15,977 tools across 805 servers](coverage.md) (5 frameworks, as of 2026-10-09).
- **Maintainers acted on findings when the evidence was specific:** the exact tool, what an agent would do wrong, and the fix. Five outside repos changed their code after a report.
- **Still unproven:** whether maintainers will come on their own. All three scan requests so far came from one person I invited. The [pre-registered bar](experiments/2026-09-scan-by-issue.md) is 10 outside requests by 2026-10-27.

## Who has used it

| Who | How they found it | What they did |
|---|---|---|
| **@izgorodin**, co-founder of an MCP memory-server company | My feedback post on the MCP spec repo | Had an AI agent (Codex) attack `--diff-against`, the version-to-version comparison. Found a real bug, re-tested my fix against the release, then found a second edge case. Both are fixed. |
| **@bruchris**, maintainer of several MCP servers | The same thread | Pointed out that the spec calls `readOnlyHint: true` with `destructiveHint: true` a contradiction, which no check caught. Two weeks later, after a direct invite, they opened 3 scan requests ([#3](https://github.com/vishalhabib99/mcp-doctor/issues/3), [#4](https://github.com/vishalhabib99/mcp-doctor/issues/4), [#5](https://github.com/vishalhabib99/mcp-doctor/issues/5)) and opted all three into the public leaderboard. |

Separately, five maintainers changed their own servers after I ran mcp-doctor (or `mcp-fuzz`) on their repo and sent a report. They acted on its findings without using it themselves:

| Repo | Finding | What the maintainer did |
|---|---|---|
| [`codebase-memory-mcp`](https://github.com/DeusData/codebase-memory-mcp/issues/2118) (45.7K★) | 12 read-only tools labeled destructive, so a careful agent asks permission just to search | Relabeled all 12 and added a test so a new tool can't ship with the wrong default |
| [`mcp-server-chart`](https://github.com/antvis/mcp-server-chart/issues/323) (4.4K★) | All 27 tools crash on missing or wrong-type input, instead of returning an error the agent can act on | Fixed; a rescan went to A, 100% |
| [`agent-inspect`](https://github.com/rajudandigam/agent-inspect/issues/362) | A graceful rejection was counted as a failed run | Shipped a new `--preset behavioral-session` for it |
| [`ha-mcp`](https://github.com/homeassistant-ai/ha-mcp/pull/2327) (4.9K★) | Undocumented parameters on `ha_call_service` | Merged my PR |
| `excel-mcp-server` (4.2K★) | Missing parameter docs, an invalid pivot-table default | Reimplemented both in v1.0.0 and [credited them](https://github.com/haris-musa/excel-mcp-server/releases/tag/v1.0.0) |

## What changed because of them

| What they hit | What I changed | Where |
|---|---|---|
| `--diff-against` reported a removed parameter that wasn't removed, when a tool moved between two Python registration styles | Read parameter names from raw schemas instead of leaving them blank, so "not checked" no longer looks like "has no parameters" | v1.9.3, with the exact repro as a test |
| The same comparison, with a schema key built at runtime | Report that the parameter list is incomplete instead of guessing a removal | v1.9.5 |
| Contradictory `readOnlyHint`/`destructiveHint` | New `annotation_contradiction` check | c3cb6e2 |
| **Scan #3 found 0 of 165 tools and still graded the server** | Recognize tools registered in a loop. A scan that finds no tools now says "Not graded" instead of showing a grade (and, from v1.12.7, gets a `needs-look` label). | v1.12.4 |
| Scan #5 got two bot replies after the requester edited the issue | A re-scan updates the earlier reply instead of posting a new one. Reopening an issue re-scans it. | v1.12.4 |
| (Indirect) If one real server was invisible, how many others were? | Measured recall on every public repo using each framework: 4 frameworks, 603 servers. 22 more bugs fixed (numbers 48–69 in the build log). Every remaining miss is [published](coverage.md). | v1.12.5–v1.12.13 |
| (Indirect) The same fear, before inviting anyone else | Scanned every invite target first: **7 of the first 10 came back with 0 tools.** Fixed what was in scope, and dropped 4 targets the tool honestly can't read. | v1.12.10–v1.12.13 |
| (Indirect) A server could quietly drop to 0 tools later | A weekly re-scan opens an alarm issue if a server's tool count falls to 0 or by more than 20% | 56824fd |

## What I got wrong

1. **I thought installation was the barrier.** It was one barrier. The bigger one: for most servers I invited people to scan, mcp-doctor would have shown nothing. A no-install scan that returns "0 tools" is worse than no scan, because it looks like an answer.
2. **I measured the wrong error first.** I'd spent weeks keeping false alarms down, which is what a maintainer complains about. A tool that can't see a server raises no false alarms at all. It took one real user to show that recall was the number that mattered.
3. **I counted downloads as users.** PyPI showed thousands of downloads, but 86% reported no operating system, which usually means bots and mirrors. I've stopped quoting them.
4. **I didn't design for normal issue behavior.** People edit issues. The bot treated an edit as a new request.

## What one user can't tell me

- **Whether people come on their own.** Every scan request so far is from one invited person. Six more invites and one follow-up went out on 2026-10-02, and the [readout](experiments/2026-09-scan-by-issue.md) on 2026-10-27 will report invited and uninvited requests separately.
- **Whether a report changes a requester's server.** None of the three scanned repos has changed since its report (checked 2026-10-02). All three graded A, and the findings were mostly parameter descriptions. That suggests the report is most useful for B- and C-grade servers. It's a hypothesis, not a finding.
- **Why they opted into the leaderboard.** I didn't ask. The next time a requester replies, I'll ask what they wanted from the scan: a badge, a check before release, or curiosity.
