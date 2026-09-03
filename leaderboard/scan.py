#!/usr/bin/env python3
"""Scan the seed list of real MCP servers with the current checkout's
mcp-doctor and write leaderboard/data.json.

Deliberately invokes mcp-doctor via `python -m mcp_doctor.cli` against the
local, editable-installed source (not a pinned PyPI version) so that every
analyzer improvement immediately re-scores every repo on the next run.

Only the aggregate score/grade per repo is published — never the raw
per-issue message text for a repo mcp-doctor's own author doesn't own.
Publishing exact exploitable specifics about someone else's real security
gaps would be irresponsible disclosure; a grade is useful without doing
that. See README's Security checks section for what each grade means.
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REPOS_FILE = Path(__file__).resolve().parent / "repos.json"
DATA_FILE = Path(__file__).resolve().parent / "data.json"


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def fetch_star_count(owner: str, repo: str) -> int | None:
    result = _run(["gh", "api", f"repos/{owner}/{repo}", "--jq", ".stargazers_count"])
    if result.returncode != 0:
        print(f"warning: could not fetch star count for {owner}/{repo}: {result.stderr.strip()}", file=sys.stderr)
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def clone_repo(owner: str, repo: str, dest: Path) -> bool:
    url = f"https://github.com/{owner}/{repo}.git"
    result = _run(["git", "clone", "--depth", "1", url, str(dest)])
    if result.returncode != 0:
        print(f"warning: could not clone {owner}/{repo}: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


def run_mcp_doctor(target: Path) -> dict | None:
    result = _run([sys.executable, "-m", "mcp_doctor.cli", str(target), "--json"], cwd=REPO_ROOT)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"warning: mcp-doctor did not return valid JSON for {target}: {result.stdout[:200]}", file=sys.stderr)
        return None


def scan_one(entry: dict, tmp_root: Path) -> dict | None:
    owner, repo = entry["owner"], entry["repo"]
    dest = tmp_root / f"{owner}__{repo}"
    if not clone_repo(owner, repo, dest):
        return None

    target = dest / entry["subdir"] if "subdir" in entry else dest
    report = run_mcp_doctor(target)
    if report is None:
        return None

    stars = fetch_star_count(owner, repo)

    return {
        "repo": f"{owner}/{repo}",
        "url": f"https://github.com/{owner}/{repo}",
        "stars": stars,
        "language": entry["language"],
        "quality_percent": report["percent"],
        "quality_grade": report["grade"],
        "security_percent": report["security_percent"],
        "security_grade": report["security_grade"],
        "tool_count": len(report["tools"]),
        "last_scanned": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def main() -> int:
    entries = json.loads(REPOS_FILE.read_text())
    results = []

    with tempfile.TemporaryDirectory(prefix="mcp-leaderboard-") as tmp:
        tmp_root = Path(tmp)
        for entry in entries:
            print(f"Scanning {entry['owner']}/{entry['repo']}...", file=sys.stderr)
            row = scan_one(entry, tmp_root)
            if row is not None:
                results.append(row)

    DATA_FILE.write_text(json.dumps(results, indent=2) + "\n")
    print(f"Wrote {len(results)}/{len(entries)} repo(s) to {DATA_FILE}", file=sys.stderr)
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
