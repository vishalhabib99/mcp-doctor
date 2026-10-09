#!/usr/bin/env python3
"""Numbers for the State of MCP Servers report. Reads results.jsonl (last row per repo wins),
refuses to run on mixed scanner versions unless --allow-mixed, writes report-numbers.md."""
import json, sys, statistics
from collections import Counter, defaultdict
from pathlib import Path
H = Path(__file__).parent
rows = {}
for l in (H/"results.jsonl").open(): r = json.loads(l); rows[r["repo"]] = r
R = list(rows.values())
vers = Counter(r["mcp_doctor"].split("@")[0] for r in R)  # 3bcc91e and f92cfff are both 0.15.6 code (f92cfff is docs-only)
mixed = len(vers) > 1
if mixed and "--allow-mixed" not in sys.argv:
    sys.exit(f"mixed scanner versions, refusing: {dict(vers)}")
disc = sum(1 for _ in (H/"discovered.jsonl").open())
err = [r for r in R if r.get("error")]
ok = [r for r in R if not r.get("error")]
W = [r for r in ok if r.get("tools")]
zero = [r for r in ok if not r.get("tools")]
T = sum(r["tools"] for r in W)
def pct(a, b): return f"{a/b*100:.1f}%" if b else "n/a"
def med(xs): xs = [x for x in xs if x is not None]; return f"{statistics.median(xs):g}" if xs else "-"
QUALITY = ["param_docs","error_handling","description","types","bare_except","url_format_hint",
           "none_default_type","indistinguishable_description","prompt_injection"]
SEC_PRECISE = ["repo:unsafe_deserialization","repo:secrets","repo:unpinned_dependency"]
SEC_HEURISTIC = ["repo:ssrf","repo:dangerous_exec"]
REPO_QUALITY = ["repo:readme","repo:tests","repo:license","repo:packaging","repo:tool_name","repo:parse_error"]
GO = {"mcp-go","go-sdk"}
L = [f"# Report numbers ({'MIXED VERSIONS, DO NOT QUOTE' if mixed else 'single version'})\n",
     f"Scanner: {dict(vers)}\n"]
L += ["## Totals", f"- Discovered: {disc}", f"- Scanned: {len(R)} (errors/skipped: {len(err)})",
      f"- With ≥1 tool: {len(W)} repos, {T} tools (median {med([r['tools'] for r in W])} tools/repo, max {max((r['tools'] for r in W), default=0)})",
      f"- 0 tools: {len(zero)}, of which {sum(1 for r in zero if r.get('sdk'))} declare an MCP SDK (upper bound on scanner misses)",
      f"- Errors: {dict(Counter(r['error'].split(':')[0] for r in err))}\n"]
L += ["## Grades (repos with ≥1 tool)",
      "- Quality: " + ", ".join(f"{g}: {n} ({pct(n,len(W))})" for g, n in sorted(Counter(r.get('grade') for r in W).items(), key=str)),
      "- Security: " + ", ".join(f"{g}: {n} ({pct(n,len(W))})" for g, n in sorted(Counter(r.get('security_grade') for r in W).items(), key=str)), ""]
def go_only(r): return set(r.get("sdk") or []) and set(r["sdk"]) <= GO
L += ["## Per-tool quality checks", "error_handling denominators exclude Go-only repos (check not applied to Go).",
      "| check | repos | % repos | tools flagged | % tools |", "|---|---|---|---|---|"]
for c in QUALITY:
    pool = [r for r in W if not (c == "error_handling" and go_only(r))]
    pt = sum(r["tools"] for r in pool)
    nr = sum(1 for r in pool if (r.get("findings") or {}).get(c))
    nt = sum((r.get("tools_with") or {}).get(c, 0) for r in pool)
    L.append(f"| {c} | {nr} | {pct(nr,len(pool))} | {nt} | {pct(nt,pt)} |")
L += ["", "## Repo-level checks (repos with ≥1 tool)", "| check | repos | % |", "|---|---|---|"]
for c in REPO_QUALITY:
    n = sum(1 for r in W if (r.get("findings") or {}).get(c)); L.append(f"| {c} | {n} | {pct(n,len(W))} |")
L += ["", "## Security (repos with ≥1 tool)", "| check | kind | repos | % |", "|---|---|---|---|"]
for kind, cs in [("precise", SEC_PRECISE), ("heuristic: worth a look", SEC_HEURISTIC), ("mixed: trigger phrase (precise) + >500 chars (heuristic), split below", ["prompt_injection"])]:
    for c in cs:
        n = sum(1 for r in W if (r.get("findings") or {}).get(c)); L.append(f"| {c} | {kind} | {n} | {pct(n,len(W))} |")
L.append("")
# concentration: per-tool flags (quality checks only) held by the top 10% of repos
per = sorted((sum((r.get("tools_with") or {}).get(c, 0) for c in QUALITY), r["repo"]) for r in W)[::-1]
tot = sum(n for n, _ in per); k = max(1, len(per)//10)
L += ["## Concentration", f"- Top 10% of repos ({k}) hold {pct(sum(n for n,_ in per[:k]), tot)} of all per-tool quality flags ({tot} total).",
      f"- Repos with 0 per-tool flags: {sum(1 for n,_ in per if n == 0)} ({pct(sum(1 for n,_ in per if n == 0), len(per))})\n"]
L += ["## Stars buckets", "| stars | repos | median quality % | % null-default | % any error-severity | % param_docs |", "|---|---|---|---|---|---|"]
for lo, hi in [(20,99),(100,499),(500,999),(1000,4999),(5000,10**9)]:
    v = [r for r in W if lo <= r["stars"] <= hi]
    f = lambda c: pct(sum(1 for x in v if (x.get("findings") or {}).get(c)), len(v))
    L.append(f"| {lo}–{hi if hi < 10**9 else '∞'} | {len(v)} | {med([x.get('percent') for x in v])} | {f('none_default_type')} | {pct(sum(1 for x in v if (x.get('severity') or {}).get('error')), len(v))} | {f('param_docs')} |")
L += ["", "## By SDK (declared dependency, repos can have several)", "| sdk | repos | tools | median quality % | % null-default | % param_docs | % error_handling |", "|---|---|---|---|---|---|---|"]
bs = defaultdict(list)
for r in W:
    for s in (r.get("sdk") or ["none detected"]): bs[s].append(r)
for s, v in sorted(bs.items(), key=lambda kv: -len(kv[1])):
    f = lambda c: pct(sum(1 for x in v if (x.get("findings") or {}).get(c)), len(v))
    eh = "n/a (Go)" if s in GO else f("error_handling")
    L.append(f"| {s} | {len(v)} | {sum(x['tools'] for x in v)} | {med([x.get('percent') for x in v])} | {f('none_default_type')} | {f('param_docs')} | {eh} |")
# robustness: a few giant registries dominate per-tool counts, so report per-repo shares too
def share(r, c): return (r.get("tools_with") or {}).get(c, 0) / r["tools"]
top1 = {r["repo"] for r in sorted(W, key=lambda r: -r["tools"])[: max(1, len(W)//100)]}
L += ["", "## Robustness (giant repos)", f"- Largest repo: {max(W, key=lambda r: r['tools'])['repo']} with {max(r['tools'] for r in W)} tools; top 10 repos hold {pct(sum(r['tools'] for r in sorted(W, key=lambda r: -r['tools'])[:10]), T)} of all tools.",
      "| check | tool % (all) | tool % excl. top 1% repos by tools | mean per-repo share |", "|---|---|---|---|"]
for c in QUALITY:
    pool = [r for r in W if not (c == "error_handling" and go_only(r))]
    rest = [r for r in pool if r["repo"] not in top1]
    a = sum((r.get("tools_with") or {}).get(c, 0) for r in pool) / sum(r["tools"] for r in pool)
    b = sum((r.get("tools_with") or {}).get(c, 0) for r in rest) / sum(r["tools"] for r in rest)
    L.append(f"| {c} | {a:.1%} | {b:.1%} | {sum(share(r, c) for r in pool)/len(pool):.1%} |")
py = [r for r in W if (r.get("langs_in_tools") or {}).get(".py")]
ndp = sum(1 for r in py if (r.get("findings") or {}).get("none_default_type"))
L += ["", f"- none_default_type is a Python-only check: {ndp} of {len(py)} repos with Python tools ({pct(ndp, len(py))}), {sum(r['langs_in_tools']['.py'] for r in py)} Python tools.",
      "", "## Stars, controlled for size", "| stars | repos | median tools | % any param_docs | mean per-repo param_docs share |", "|---|---|---|---|---|"]
for lo, hi in [(20,99),(100,499),(500,999),(1000,4999),(5000,10**9)]:
    v = [r for r in W if lo <= r["stars"] <= hi]
    L.append(f"| {lo}–{hi if hi < 10**9 else '∞'} | {len(v)} | {med([r['tools'] for r in v])} | {pct(sum(1 for r in v if share(r,'param_docs') > 0), len(v))} | {sum(share(r,'param_docs') for r in v)/len(v):.1%} |")
# followup.py split (prompt_injection and tool_name variants), if it has run
FU = H/"followup.jsonl"
if FU.exists():
    fu = [json.loads(l) for l in FU.open()]; fok = [r for r in fu if not r.get("error")]
    L += ["", "## Detail pass (followup.py)", f"- Repos detailed: {len(fu)} (errors: {len(fu)-len(fok)})",
          f"- prompt_injection, trigger phrase: {sum(1 for r in fok if r['pi_trigger'])} repos, {sum(len(r['pi_trigger']) for r in fok)} tools (every hit read by hand; see report)",
          f"- prompt_injection, description >500 chars: {sum(1 for r in fok if r['pi_long'])} repos, {sum(len(r['pi_long']) for r in fok)} tools",
          f"- tool_name, invalid characters/length: {sum(1 for r in fok if r['tn_invalid'])} repos (includes confirmed false positives: definition objects whose registered name differs from their `name` field)",
          f"- tool_name, duplicate name: {sum(1 for r in fok if r['tn_duplicate'])} repos (no call-graph awareness; can be separate server instances)"]
(H/"report-numbers.md").write_text("\n".join(L) + "\n")
print(f"wrote report-numbers.md ({'MIXED' if mixed else 'single version'}): {len(W)} repos, {T} tools")
