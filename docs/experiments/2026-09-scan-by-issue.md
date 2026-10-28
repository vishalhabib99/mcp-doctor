# Experiment: do MCP authors use mcp-doctor when there's nothing to install?

**Status:** closed. It ran 2026-09-27 to 2026-10-27, and the result is 4 outside requests against a bar of 10 (see the readout). Pre-registered 2026-09-28, on day 1, with 0 outside requests. The readout at the bottom is filled in once, on 2026-10-27, whatever the number is.

## Why

PyPI download counts looked like usage, but 86% report no operating system, which usually means bots and mirrors. No outside repo runs the mcp-doctor or mcp-trust-check GitHub Action. So there was no honest evidence anyone uses these tools. [Scan by issue](../../README.md) removes the install step: open an issue, paste an MCP server's repo URL, and a bot replies with the report. If people still don't use it with no install, installation wasn't the barrier.

## What this is, and isn't

This is a pre-registered launch test: one bar, set before looking, and a decision tied to it. It is **not** an A/B test. There is no control group, so it can show whether people use the scan, not what caused them to.

## Hypothesis

MCP server authors will request a scan when it takes one issue and no install.

## Primary metric (the only one the decision uses)

**Outside scan requests:** issues in `vishalhabib99/mcp-doctor` that
- carry the `scan-request` label,
- were opened from 2026-09-27 through 2026-10-27 (UTC), and
- were **not** opened by `vishalhabib99`.

Counted once per issue, whether or not the scan succeeded. One person opening several issues counts each one, and repeat requesters are reported separately below.

```bash
gh issue list -R vishalhabib99/mcp-doctor --label scan-request --state all \
  --search "created:2026-09-27..2026-10-27 -author:vishalhabib99" \
  --json number --jq length
```

## Bar and decision (set 2026-09-27, written down here 2026-09-28)

| Result | Decision |
|---|---|
| **10 or more** | Keep investing: promote it further and consider the next scan-by-issue feature. |
| **Under 10** | Stop investing in reach for these tools. The workflow stays live because it costs nothing to run, but no more promotion or features. Publish the number either way. |

## Reported, but not used for the decision

- Distinct requesters, and how many came back for a second scan
- How many opted into the public leaderboard
- Scans that failed, and why (bad URL, missing subfolder, unsupported language)
- Any maintainer who changed their server after a report (links)
- Where requesters came from, when they say (LinkedIn post, direct invite, search)

## Known threats to reading the result

- **Invites inflate the count.** Maintainers invited directly are more likely to request. Invited requests count, since getting them is the point of distribution, but they're labeled as invited in the readout.
- **One channel.** Promotion so far is one LinkedIn post. A miss could mean the channel was wrong, not the idea.
- **30 days is short** for developer tools, and the MCP author population is small.
- **Bots and tests.** Issues opened by bots or by test accounts are excluded and listed.

## Changes after pre-registration

- **2026-10-15: the decision rule was dropped, before the readout.** At 3 outside requests, I decided to keep growing mcp-doctor in the open whatever the number, so "under 10 → stop promoting" no longer applies. The metric, the counting rule and the 2026-10-27 readout are unchanged, and the readout will say the rule was dropped.
- **2026-10-16: two requests the metric doesn't count.** #6 and #7 (one outside maintainer, same repo) were opened from the GitHub CLI, which can't apply the `scan-request` label without triage permission, so the bot never ran. The requester closed both. They fail the label rule above and aren't counted; they're reported here because they show a real gap: a request that doesn't come through the issue form gets no reply.

## Readout (filled in on 2026-10-27)

- Outside scan requests: **4. The bar was 10, so this is a miss.** #3, #4 and #5 from [@bruchris](https://github.com/bruchris) (2026-09-29) and #8 from [@rajudandigam](https://github.com/rajudandigam) (2026-10-16). The command above returns exactly these four.
- Decision: **none triggered.** The "under 10, stop promoting" rule was dropped on 2026-10-15 (see Changes), so mcp-doctor keeps growing in the open. The miss is recorded here as it stands.
- Distinct requesters / repeat: 2 / 0. bruchris filed three repos in one sitting. Neither requester came back later for a second scan.
- Opted into leaderboard: 3 (#3, #4, #5).
- Failed scans (reasons): 2 of 4 first came back with 0 tools, and both were mcp-doctor misses, not the requesters' mistakes. #3 (`canvas-lms-mcp`: tools registered by a loop) was fixed as bug #46 in v1.12.4. #8 (`agent-inspect`: a `tools/list` array built with `.map(...)`) was fixed as bug #117 in v1.27.2. Both were re-scanned with the right tool count. The two uncounted CLI attempts (#6, #7) never got a scan.
- Maintainer changes after a report: none confirmed. I didn't track the requesters' repos after the reports.
- Invited vs. uninvited: 4 / 0. Every counted request came from a maintainer I'd invited directly. None came from the LinkedIn post, the profile or search.
- What I'd do differently: removing the install step wasn't enough to bring in strangers. Distribution was the barrier, not installation. Next time I'd put the check where maintainers already are, in a registry or a CI step, before relying on an issue form. I'd also make a request that doesn't come through the form (like #6 and #7) still get a reply.
