# Experiment: do MCP authors use mcp-doctor when there's nothing to install?

**Status:** running, 2026-09-27 to 2026-10-27. Pre-registered 2026-09-28, on day 1, with 0 outside requests. The readout at the bottom is filled in once, on 2026-10-27, whatever the number is.

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

## Readout (fill in on 2026-10-27)

- Outside scan requests: **_**
- Decision: **_**
- Distinct requesters / repeat: _ / _
- Opted into leaderboard: _
- Failed scans (reasons): _
- Maintainer changes after a report: _
- Invited vs. uninvited: _ / _
- What I'd do differently: _
