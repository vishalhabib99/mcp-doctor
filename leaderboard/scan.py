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
import urllib.error
import urllib.request
from pathlib import Path

from badge import badge_filename, render_badge_svg

REPO_ROOT = Path(__file__).resolve().parent.parent
REPOS_FILE = Path(__file__).resolve().parent / "repos.json"
DATA_FILE = Path(__file__).resolve().parent / "data.json"
BADGES_DIR = Path(__file__).resolve().parent / "badges"
LIVE_DATA_URL = "https://vishalhabib99.github.io/mcp-doctor/data.json"


def fetch_previous_scan() -> dict[str, dict]:
    """Reads whatever's currently live as the drift baseline, rather than
    keeping a separate committed history file — the deployed data.json
    already *is* "the last scan," and treating it as the baseline avoids
    ever needing CI to commit back to the repo. Returns {} on the very
    first run (nothing deployed yet) or any fetch failure — drift just
    doesn't get reported that round rather than failing the whole scan."""
    try:
        with urllib.request.urlopen(LIVE_DATA_URL, timeout=15) as resp:
            previous = json.loads(resp.read())
        return {row["repo"]: row for row in previous}
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        print(f"warning: could not fetch previous scan for drift comparison: {exc}", file=sys.stderr)
        return {}


def compute_drift(current_percent: int, previous: dict | None, key: str) -> int | None:
    """None (not 0) when there's no prior scan to compare against (a
    brand-new repo on the leaderboard, or the live fetch failed) — the
    frontend needs to tell "unchanged" apart from "nothing to compare yet"
    rather than showing a misleading "±0%" on a repo's very first scan."""
    if previous is None or previous.get(key) is None:
        return None
    return current_percent - previous[key]


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


def scan_one(entry: dict, tmp_root: Path, previous_by_repo: dict[str, dict]) -> dict | None:
    owner, repo = entry["owner"], entry["repo"]
    dest = tmp_root / f"{owner}__{repo}"
    if not clone_repo(owner, repo, dest):
        return None

    target = dest / entry["subdir"] if "subdir" in entry else dest
    report = run_mcp_doctor(target)
    if report is None:
        return None

    stars = fetch_star_count(owner, repo)

    BADGES_DIR.mkdir(exist_ok=True)
    badge_svg = render_badge_svg("mcp-doctor", f"{report['grade']} {report['percent']}%", report["grade"])
    (BADGES_DIR / badge_filename(owner, repo)).write_text(badge_svg)

    previous = previous_by_repo.get(f"{owner}/{repo}")
    quality_change = compute_drift(report["percent"], previous, "quality_percent")
    security_change = compute_drift(report["security_percent"], previous, "security_percent")

    return {
        "repo": f"{owner}/{repo}",
        "url": f"https://github.com/{owner}/{repo}",
        "stars": stars,
        "language": entry["language"],
        "quality_percent": report["percent"],
        "quality_percent_change": quality_change,
        "quality_grade": report["grade"],
        "security_percent": report["security_percent"],
        "security_percent_change": security_change,
        "security_grade": report["security_grade"],
        "tool_count": len(report["tools"]),
        "badge": f"badges/{badge_filename(owner, repo)}",
        "last_scanned": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "previous_scanned": previous["last_scanned"] if previous else None,
    }


def main() -> int:
    entries = json.loads(REPOS_FILE.read_text())
    previous_by_repo = fetch_previous_scan()
    results = []

    with tempfile.TemporaryDirectory(prefix="mcp-leaderboard-") as tmp:
        tmp_root = Path(tmp)
        for entry in entries:
            print(f"Scanning {entry['owner']}/{entry['repo']}...", file=sys.stderr)
            row = scan_one(entry, tmp_root, previous_by_repo)
            if row is not None:
                results.append(row)

    DATA_FILE.write_text(json.dumps(results, indent=2) + "\n")
    print(f"Wrote {len(results)}/{len(entries)} repo(s) to {DATA_FILE}", file=sys.stderr)
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
