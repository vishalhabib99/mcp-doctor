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
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from badge import badge_filename, render_badge_svg
from scan_request import RequestError, parse_request

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
    badge_svg = render_badge_svg("mcp-doctor", (f"{report['grade']} {report['percent']}%" if report.get("graded", True) else "not graded"), report["grade"])
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


# A tool count that collapses between scans is the silent failure that
# matters most: mcp-doctor graded bruchris/canvas-lms-mcp after seeing 0 of
# its 166 tools. Scores alone don't show it, since a server with 0 tools
# found can still get a grade.
TOOL_DROP_FRACTION = 0.2


def tool_count_alarms(results: list[dict], previous_by_repo: dict[str, dict]) -> list[str]:
    """One markdown line per repo whose tool count fell to 0 or by more than
    TOOL_DROP_FRACTION since the last published scan, or that the last scan
    had but this one couldn't scan at all."""
    alarms = []
    scanned = {row["repo"] for row in results}
    for row in results:
        before = (previous_by_repo.get(row["repo"]) or {}).get("tool_count")
        after = row["tool_count"]
        if before and (after == 0 or after < before * (1 - TOOL_DROP_FRACTION)):
            alarms.append(f"- [`{row['repo']}`]({row['url']}): {before} → {after} tools")
    for repo, prev in sorted(previous_by_repo.items()):
        if repo not in scanned and prev.get("tool_count"):
            alarms.append(f"- [`{repo}`](https://github.com/{repo}): not scanned this run (had {prev['tool_count']} tools)")
    return alarms


_LANGUAGE_LABELS = {"Python": "Python", "TypeScript": "TS", "JavaScript": "TS", "Go": "Go"}


def opted_in_entries(seen: set[str]) -> list[dict]:
    """Repos whose owners asked to be listed through a scan-request issue.

    Only the bot adds the `leaderboard` label (after a successful scan of a
    request that ticked the opt-in box), and removing the label takes a repo
    off again. Reading the issues at build time means CI never has to commit
    back to repos.json."""
    result = _run(["gh", "issue", "list", "--label", "leaderboard", "--state", "all",
                   "--limit", "200", "--json", "body"])
    if result.returncode != 0:
        print(f"warning: could not list opted-in scan requests: {result.stderr.strip()}", file=sys.stderr)
        return []
    entries = []
    for issue in json.loads(result.stdout or "[]"):
        try:
            req = parse_request(issue["body"])
        except RequestError:
            continue
        key = f"{req['owner']}/{req['repo']}".lower()
        if key in seen:
            continue
        seen.add(key)
        lang = _run(["gh", "api", f"repos/{req['owner']}/{req['repo']}", "--jq", ".language"]).stdout.strip()
        entry = {"owner": req["owner"], "repo": req["repo"], "language": _LANGUAGE_LABELS.get(lang, lang or "?")}
        if req["subdir"]:
            entry["subdir"] = req["subdir"]
        entries.append(entry)
    return entries


def main() -> int:
    entries = json.loads(REPOS_FILE.read_text())
    entries += opted_in_entries({f"{e['owner']}/{e['repo']}".lower() for e in entries})
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

    alarms = tool_count_alarms(results, previous_by_repo)
    if alarms:
        print("Tool-count alarms:\n" + "\n".join(alarms), file=sys.stderr)
        alarms_file = os.environ.get("ALARMS_FILE")
        if alarms_file:
            Path(alarms_file).write_text("\n".join(alarms) + "\n")
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
