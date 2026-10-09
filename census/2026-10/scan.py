#!/usr/bin/env python3
"""Census runner: shallow-clone each discovered repo, run mcp-doctor --json, record one
JSONL row, delete the clone. Resumable: repos already in results.jsonl are skipped.
Usage: scan.py [--workers N] [--rescan repo1,repo2]"""
import json, os, re, shutil, subprocess, sys, tempfile, time, argparse, threading
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from pathlib import Path
HERE = Path(__file__).parent
DISC = HERE/"discovered.jsonl"
RES = Path(os.environ.get("CENSUS_RES", HERE/"results.jsonl")); DET = Path(os.environ.get("CENSUS_DET", HERE/"details.jsonl"))
PY = os.environ.get("CENSUS_PY")  # python 3.12 venv with mcp-doctor editable install
TMP = Path(os.environ["CENSUS_TMP"])
MAX_KB = 400_000
lock = threading.Lock()
def ver():
    r = subprocess.run([PY,"-c","import importlib.metadata as m;print(m.version('mcp-server-lint'))"],capture_output=True,text=True)
    h = subprocess.run(["git","-C",os.environ.get("CENSUS_MD", str(Path.home()/"code/mcp-doctor")),"rev-parse","--short","HEAD"],capture_output=True,text=True)
    return f"{r.stdout.strip()}@{h.stdout.strip()}"
VER = None
SDK_PATTERNS = [
  ("fastmcp", re.compile(r"^\s*\"?fastmcp", re.M|re.I)),
  ("mcp-python", re.compile(r"^\s*\"?mcp(\[[^\]]*\])?\s*([<>=~!]|\"|$|,)", re.M)),
  ("ts-sdk", re.compile(r"@modelcontextprotocol/sdk")),
  ("mcp-go", re.compile(r"mark3labs/mcp-go")),
  ("go-sdk", re.compile(r"modelcontextprotocol/go-sdk")),
  ("fastmcp-ts", re.compile(r"\"fastmcp\"")),
  ("mcp-framework", re.compile(r"\"mcp-framework\"")),
]
SDK_KIND = {"fastmcp":"py","mcp-python":"py","ts-sdk":"ts","mcp-go":"go","go-sdk":"go","fastmcp-ts":"ts","mcp-framework":"ts"}
def detect_sdk(root):
    found = set()
    for name in ["pyproject.toml","requirements.txt","setup.py","package.json","go.mod","uv.lock"]:
        for p in list(root.glob(name)) + list(root.glob("*/"+name)) + list(root.glob("*/*/"+name)):
            if "node_modules" in p.parts: continue
            try: t = p.read_text(errors="ignore")[:400000]
            except Exception: continue
            kind = "ts" if name=="package.json" else "go" if name=="go.mod" else "py"
            for sdk, rx in SDK_PATTERNS:
                if SDK_KIND[sdk]==kind and rx.search(t): found.add(sdk)
    return sorted(found)
def scan(entry):
    repo = entry["repo"]; row = dict(entry); row.update(mcp_doctor=VER, scanned_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    if entry.get("size_kb",0) > MAX_KB:
        row["error"] = "skipped_too_large"; return row, None
    d = Path(tempfile.mkdtemp(dir=TMP))
    try:
        r = subprocess.run(["git","clone","--depth","1","--single-branch","-q",f"https://github.com/{repo}.git",str(d/"r")],
            capture_output=True,text=True,timeout=300,env={**os.environ,"GIT_TERMINAL_PROMPT":"0","GIT_LFS_SKIP_SMUDGE":"1"})
        if r.returncode: row["error"] = "clone_failed: "+r.stderr.strip()[-200:]; return row, None
        root = d/"r"; row["sdk"] = detect_sdk(root)
        try:
            m = subprocess.run([PY,"-m","mcp_doctor.cli",str(root),"--json","--no-color"],capture_output=True,text=True,timeout=600)
        except subprocess.TimeoutExpired:
            row["error"] = "mcp_doctor_timeout"; return row, None
        try: rep = json.loads(m.stdout)
        except json.JSONDecodeError:
            row["error"] = "mcp_doctor_crash: "+(m.stderr.strip()[-400:] or m.stdout[:200]); return row, None
        tools = rep.get("tools", [])
        checks, tool_checks, sev = Counter(), Counter(), Counter()
        nd = []
        for t in tools:
            seen = set()
            for i in t.get("issues", []):
                checks[i["check"]] += 1; sev[i["severity"]] += 1; seen.add(i["check"])
                if i["check"] in ("none_default_type",):
                    nd.append({"tool":t["name"],"file":i["file"],"line":i["line"],"message":i["message"]})
            for c in seen: tool_checks[c] += 1
        for i in rep.get("repo_issues", []) or []:
            checks["repo:"+i.get("check","?")] += 1
        row.update(tools=len(tools), percent=rep.get("percent"), grade=rep.get("grade"),
            security_percent=rep.get("security_percent"), security_grade=rep.get("security_grade"),
            findings=dict(checks), tools_with=dict(tool_checks), severity=dict(sev),
            langs_in_tools=dict(Counter(Path(t["file"]).suffix for t in tools)))
        det = {"repo":repo,"none_default":nd} if nd else None
        return row, det
    except subprocess.TimeoutExpired:
        row["error"] = "clone_timeout"; return row, None
    finally:
        shutil.rmtree(d, ignore_errors=True)
def main():
    global VER
    ap = argparse.ArgumentParser(); ap.add_argument("--workers",type=int,default=6); ap.add_argument("--rescan",default="")
    ap.add_argument("--limit",type=int,default=0); ap.add_argument("--only",default=""); a = ap.parse_args()
    VER = ver(); TMP.mkdir(parents=True, exist_ok=True)
    entries = [json.loads(l) for l in DISC.open()]
    rescan = set(filter(None,a.rescan.split(",")))
    done = set()
    if RES.exists():
        for l in RES.open():
            r = json.loads(l)
            if r["repo"] not in rescan: done.add(r["repo"])
    todo = [e for e in entries if e["repo"] not in done]
    if rescan: todo = [e for e in todo if e["repo"] in rescan]
    if a.only:
        keep = set(Path(a.only).read_text().split()); todo = [e for e in entries if e["repo"] in keep and e["repo"] not in done]
    if a.limit: todo = todo[:a.limit]
    print(f"{VER}: {len(todo)} to scan, {len(done)} done", file=sys.stderr, flush=True)
    n = 0
    with ThreadPoolExecutor(a.workers) as ex, RES.open("a") as rf, DET.open("a") as df:
        for row, det in ex.map(scan, todo):
            with lock:
                rf.write(json.dumps(row)+"\n"); rf.flush()
                if det: df.write(json.dumps(det)+"\n"); df.flush()
                n += 1
                if n % 25 == 0: print(f"{n}/{len(todo)} {time.strftime('%H:%M:%S')}", file=sys.stderr, flush=True)
main()
