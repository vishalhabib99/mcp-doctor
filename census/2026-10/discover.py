#!/usr/bin/env python3
"""Discover public MCP server repos via GitHub repo search.
Search returns at most 1000 results per query, so each query is sliced by star range
until every slice is under 1000. Writes discovered.jsonl (deduped, no forks/archived)."""
import json, subprocess, sys, time, urllib.parse
from pathlib import Path
OUT = Path(__file__).parent / "discovered.jsonl"
BASES = [
  "topic:mcp-server", "topic:model-context-protocol", "topic:mcp-servers",
  "topic:modelcontextprotocol", "topic:mcp",
  "mcp server in:name,description,readme", "fastmcp in:name,description,readme",
  "model context protocol in:name,description,readme",
]
LANGS = ["Python", "TypeScript", "JavaScript", "Go"]
def search(q):
    items, page = [], 1
    while True:
        r = subprocess.run(["gh","api","-X","GET","search/repositories","-f",f"q={q}","-f","per_page=100","-f",f"page={page}"],capture_output=True,text=True)
        if r.returncode:
            if "rate limit" in r.stderr.lower() or "403" in r.stderr:
                time.sleep(60); continue
            print("ERR", q, r.stderr[:200], file=sys.stderr); return None, items
        d = json.loads(r.stdout); items += d["items"]
        total = d["total_count"]
        if len(d["items"]) < 100 or page >= 10: return total, items
        page += 1; time.sleep(2.2)
def slices(base, lang, lo, hi):
    q = f"{base} language:{lang} fork:false archived:false stars:{lo}..{hi}"
    total, items = search(q)
    if total is None: return items
    if total > 1000 and hi - lo > 0:
        mid = (lo + hi) // 2
        return slices(base, lang, lo, mid) + slices(base, lang, mid + 1, hi)
    print(f"{q} -> {total} (got {len(items)})", file=sys.stderr)
    return items
seen = {}
if OUT.exists():
    for l in OUT.open(): seen[json.loads(l)["repo"]] = 1
with OUT.open("a") as f:
    for base in BASES:
        for lang in LANGS:
            for lo, hi in [(20, 99), (100, 999), (1000, 9999), (10000, 1000000)]:
                for it in slices(base, lang, lo, hi):
                    k = it["full_name"]
                    if k in seen or it["fork"] or it["archived"]: continue
                    seen[k] = 1
                    f.write(json.dumps({"repo": k, "stars": it["stargazers_count"], "lang": it["language"],
                        "pushed": it["pushed_at"], "size_kb": it["size"], "topics": it.get("topics", []),
                        "found_by": base}) + "\n"); f.flush()
print("total", len(seen), file=sys.stderr)
