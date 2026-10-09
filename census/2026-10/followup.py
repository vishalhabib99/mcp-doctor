#!/usr/bin/env python3
"""Detail pass for the report. results.jsonl only keeps counts, so this reruns mcp-doctor on
every repo with a prompt_injection or tool_name finding and records which variant fired:
  prompt_injection: trigger phrase (error, precise) vs >500-char description (warning, heuristic)
  tool_name: invalid characters/length (objective) vs duplicate name (no call-graph awareness)
Resumable: repos already in followup.jsonl are skipped. Usage: followup.py [--workers N] [--limit N]"""
import json, os, shutil, subprocess, tempfile, argparse, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
H = Path(__file__).parent
OUT = H/"followup.jsonl"
PY = os.environ["CENSUS_PY"]; TMP = Path(os.environ["CENSUS_TMP"])
lock = threading.Lock()
def detail(repo):
    row = {"repo": repo, "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    d = Path(tempfile.mkdtemp(dir=TMP))
    try:
        r = subprocess.run(["git","clone","--depth","1","--single-branch","-q",f"https://github.com/{repo}.git",str(d/"r")],
            capture_output=True, text=True, timeout=300, env={**os.environ,"GIT_TERMINAL_PROMPT":"0","GIT_LFS_SKIP_SMUDGE":"1"})
        if r.returncode: row["error"] = "clone_failed"; return row
        m = subprocess.run([PY,"-m","mcp_doctor.cli",str(d/"r"),"--json","--no-color"], capture_output=True, text=True, timeout=600)
        rep = json.loads(m.stdout)
        trig, long_ = [], []
        for t in rep.get("tools", []):
            for i in t.get("issues", []):
                if i["check"] != "prompt_injection": continue
                ex = {"tool": t["name"], "file": i["file"], "line": i["line"]}
                (trig if i["severity"] == "error" else long_).append(ex)
        tn = [i["message"] for i in rep.get("repo_issues") or [] if i.get("check") == "tool_name"]
        row.update(tools=len(rep.get("tools", [])), pi_trigger=trig, pi_long=long_,
                   tn_invalid=[x for x in tn if "violate the spec's Tool Names" in x],
                   tn_duplicate=[x for x in tn if "declared more than once" in x])
        return row
    except subprocess.TimeoutExpired: row["error"] = "timeout"; return row
    except json.JSONDecodeError: row["error"] = "mcp_doctor_crash"; return row
    finally: shutil.rmtree(d, ignore_errors=True)
def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--workers", type=int, default=8); ap.add_argument("--limit", type=int, default=0); a = ap.parse_args()
    rows = {}
    for l in (H/"results.jsonl").open(): r = json.loads(l); rows[r["repo"]] = r
    vers = {r["mcp_doctor"].split("@")[0] for r in rows.values()}
    if len(vers) > 1: raise SystemExit(f"results.jsonl still mixed: {vers}")
    want = sorted(r["repo"] for r in rows.values() if (r.get("findings") or {}).get("prompt_injection") or (r.get("findings") or {}).get("repo:tool_name"))
    done = {json.loads(l)["repo"] for l in OUT.open()} if OUT.exists() else set()
    todo = [x for x in want if x not in done][: a.limit or None]
    TMP.mkdir(parents=True, exist_ok=True)
    print(f"{len(todo)} to detail, {len(done)} done", flush=True)
    n = 0
    with ThreadPoolExecutor(a.workers) as ex, OUT.open("a") as f:
        for row in ex.map(detail, todo):
            with lock:
                f.write(json.dumps(row) + "\n"); f.flush(); n += 1
                if n % 50 == 0: print(f"{n}/{len(todo)} {time.strftime('%H:%M:%S')}", flush=True)
    print("FOLLOWUP_DONE", flush=True)
main()
