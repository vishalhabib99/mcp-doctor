#!/usr/bin/env python3
"""Handle a "scan my server" issue: parse the requested repo out of the
issue body, scan it with the current checkout's mcp-doctor, and write the
reply comment to a file for the workflow to post.

The issue body is untrusted input from anyone on GitHub, so it never
reaches a shell: the workflow passes it in through an environment variable,
and the repo URL has to match a strict github.com owner/repo pattern before
anything is cloned. mcp-doctor itself only parses source files and never
runs them, which is what makes scanning a stranger's repo in CI safe. The
runtime testers (mcp-fuzz, mcp-reality-check) are deliberately not offered
here, since they would start the stranger's server.

Same disclosure rule as scan.py: an issue comment is public, so security
findings are reported as counts only. Quality findings (missing docs,
unclear params) are safe to list, since they tell an agent-builder nothing
exploitable. The requester gets the exact command to see the security
details on their own machine.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LEADERBOARD_URL = "https://vishalhabib99.github.io/mcp-doctor/"
MAX_QUALITY_ROWS = 15
CLONE_TIMEOUT_S = 180
SCAN_TIMEOUT_S = 300

# GitHub's own rules: owner is alphanumeric plus single hyphens (max 39),
# repo is alphanumeric plus . _ - (max 100). Anything else is rejected
# before it gets near git.
_REPO_URL = re.compile(
    r"^https://github\.com/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100}?)"
    r"(?:\.git)?/?$"
)
_SUBDIR = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_NO_RESPONSE = "_No response_"


def _echo(text: str) -> str:
    """Requester text shown back inside a code span: capped, and with no
    backticks, so it can't close the span and inject markdown or @mentions."""
    return text[:120].replace("`", "'").replace("\n", " ")


class RequestError(ValueError):
    """The issue body can't be turned into a scan; the message is shown to the requester."""


def _form_fields(body: str) -> dict[str, str]:
    """Split a GitHub issue-form body ("### Label\\n\\nvalue") into {label: value}."""
    fields: dict[str, str] = {}
    for chunk in re.split(r"^### ", body, flags=re.MULTILINE)[1:]:
        label, _, value = chunk.partition("\n")
        fields[label.strip().lower()] = value.strip()
    return fields


def parse_request(body: str) -> dict:
    """Return {"owner", "repo", "subdir", "leaderboard"} or raise RequestError."""
    fields = _form_fields(body or "")
    url = fields.get("repository url", "").strip().strip("<>")
    if not url or url == _NO_RESPONSE:
        raise RequestError("I couldn't find a repository URL in this issue. Please use the **Scan my MCP server** issue form.")

    match = _REPO_URL.match(url)
    if not match:
        raise RequestError(
            f"`{_echo(url)}` isn't a public GitHub repository URL I can scan. "
            "It should look like `https://github.com/owner/repo`."
        )
    owner, repo = match.group("owner"), match.group("repo")
    if repo in {".", ".."}:
        raise RequestError("That repository name isn't valid.")

    subdir = fields.get("subdirectory (optional)", "").strip().strip("/")
    if subdir in {"", _NO_RESPONSE}:
        subdir = None
    elif not _SUBDIR.match(subdir) or any(part in {".", ".."} for part in subdir.split("/")):
        raise RequestError(f"`{_echo(subdir)}` isn't a valid subdirectory path. Use a plain relative path like `packages/server`.")

    leaderboard = bool(re.search(r"^- \[[xX]\]", fields.get("leaderboard", ""), flags=re.MULTILINE))
    return {"owner": owner, "repo": repo, "subdir": subdir, "leaderboard": leaderboard}


def summarize(report: dict) -> dict:
    """Pull what the public comment may show out of a full --json report."""
    all_issues = list(report.get("repo_issues", []))
    for tool in report.get("tools", []):
        all_issues.extend(tool.get("issues", []))

    quality = [i for i in all_issues if i.get("category", "quality") == "quality"]
    security = [i for i in all_issues if i.get("category") == "security"]
    severity_rank = {"error": 0, "warning": 1, "info": 2}
    quality.sort(key=lambda i: (severity_rank.get(i.get("severity"), 3), i.get("tool", ""), i.get("check", "")))

    return {
        "quality_percent": report["percent"],
        "quality_grade": report["grade"],
        "security_percent": report["security_percent"],
        "security_grade": report["security_grade"],
        "tool_count": len(report.get("tools", [])),
        "quality_issues": quality,
        "security_counts": Counter(i.get("severity", "warning") for i in security),
    }


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_comment(req: dict, summary: dict) -> str:
    target = f"{req['owner']}/{req['repo']}" + (f"/{req['subdir']}" if req["subdir"] else "")
    lines = [
        f"## mcp-doctor report for `{target}`",
        "",
    ]
    if summary["tool_count"] == 0:
        # No grade on nothing: an A for "no tools, so no bad descriptions" read
        # as a real result on scan request #3 (a 165-tool server mcp-doctor
        # couldn't parse yet).
        lines += [
            "**Not graded: no tools found.**",
            "",
            "Either the server lives in a subfolder (open a new request and fill in **Subdirectory**), "
            "or it registers tools in a way mcp-doctor can't read yet. mcp-doctor reads Python, TypeScript "
            "and Go servers. If this repo does have tools, reply here: that's a mcp-doctor bug worth fixing.",
        ]
    else:
        lines += [
            "| | Grade | Score |",
            "|---|---|---|",
            f"| Quality (can an agent tell what each tool does?) | **{summary['quality_grade']}** | {summary['quality_percent']}% |",
            f"| Security (risky patterns in the code an agent can reach) | **{summary['security_grade']}** | {summary['security_percent']}% |",
            "",
            f"Found **{summary['tool_count']}** tool(s).",
        ]

    quality = summary["quality_issues"]
    lines += ["", f"### Quality findings ({len(quality)})"]
    if quality:
        lines += ["", "| Severity | Where | Check | What an agent would hit |", "|---|---|---|---|"]
        for issue in quality[:MAX_QUALITY_ROWS]:
            where = issue.get("tool") or issue.get("file") or "repo"
            lines.append(
                f"| {issue.get('severity', '')} | `{_cell(where)}` | {_cell(issue.get('check', ''))} | {_cell(issue.get('message', ''))} |"
            )
        if len(quality) > MAX_QUALITY_ROWS:
            lines.append(f"\n…and {len(quality) - MAX_QUALITY_ROWS} more. Run it locally (below) for the full list.")
    else:
        lines += ["", "None. Every tool is documented well enough for an agent to use."]

    counts = summary["security_counts"]
    total_security = sum(counts.values())
    lines += ["", f"### Security findings ({total_security})"]
    if total_security:
        breakdown = ", ".join(f"{n} {sev}{'' if n == 1 else 's'}" for sev, n in sorted(counts.items()))
        lines += [
            "",
            f"{breakdown}. This comment is public, so security details aren't posted here. "
            "See them on your own machine:",
        ]
    else:
        lines += ["", "None found. See the full report on your own machine:"]

    local_path = f"{req['repo']}/{req['subdir']}" if req["subdir"] else req["repo"]
    lines += [
        "",
        "```bash",
        "pip install mcp-server-lint",
        f"git clone --depth 1 https://github.com/{req['owner']}/{req['repo']}.git",
        f"mcp-doctor {local_path}",
        "```",
        "",
        "To check every pull request automatically, add the "
        "[mcp-doctor GitHub Action](https://github.com/marketplace/actions/mcp-doctor). "
        "This report reads code only. To also test how the running server behaves (crashes on bad input, "
        "\"successful\" responses that aren't), run [mcp-trust-check](https://github.com/vishalhabib99/mcp-trust-check) "
        "against your own server.",
    ]
    if req["leaderboard"]:
        lines += [
            "",
            f"📊 This repo will appear on the [public leaderboard]({LEADERBOARD_URL}) after its next rebuild "
            "(grades only, never findings).",
        ]
    lines += [
        "",
        "---",
        "<sub>Scanned by reading the code only; nothing in the repo was run. "
        "Found a false positive? Reply here. That's how most of mcp-doctor's fixes started.</sub>",
    ]
    return "\n".join(lines) + "\n"


def render_error(message: str) -> str:
    return f"## Couldn't scan this one\n\n{message}\n\nEdit this issue or open a new one and I'll try again.\n"


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True,
                              timeout=CLONE_TIMEOUT_S, env=env, cwd=cwd)
    except subprocess.TimeoutExpired:
        raise RequestError("Cloning the repository took too long. Is it unusually large?") from None


def _clone(owner: str, repo: str, dest: Path, subdir: str | None) -> None:
    """Shallow clone. With a subdirectory, fetch only that folder's files
    (sparse, blobless), so a server inside a large monorepo stays fast."""
    url = f"https://github.com/{owner}/{repo}.git"
    args = ["clone", "--depth", "1", "--no-recurse-submodules"]
    if subdir:
        args += ["--filter=blob:none", "--sparse"]
    result = _git([*args, "--", url, str(dest)])
    if result.returncode != 0:
        raise RequestError(f"I couldn't clone `{url[:-4]}`. Is it public?")
    if subdir and _git(["sparse-checkout", "set", "--", subdir], cwd=dest).returncode != 0:
        raise RequestError(f"I couldn't check out the subdirectory `{subdir}`.")


def _scan(target: Path) -> dict:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "mcp_doctor.cli", str(target), "--json"],
            capture_output=True, text=True, timeout=SCAN_TIMEOUT_S, cwd=REPO_ROOT,
        )
    except subprocess.TimeoutExpired:
        raise RequestError("The scan took too long and was stopped.") from None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RequestError("mcp-doctor couldn't produce a report for this repository.") from None


def handle(body: str) -> tuple[str, dict]:
    """Return (comment markdown, outcome) for one issue body."""
    try:
        req = parse_request(body)
        with tempfile.TemporaryDirectory(prefix="mcp-scan-request-") as tmp:
            dest = Path(tmp) / "repo"
            _clone(req["owner"], req["repo"], dest, req["subdir"])
            target = dest / req["subdir"] if req["subdir"] else dest
            if not target.is_dir():
                raise RequestError(f"The subdirectory `{req['subdir']}` doesn't exist in that repository.")
            summary = summarize(_scan(target))
    except RequestError as exc:
        return render_error(str(exc)), {"ok": False, "leaderboard": False}
    ok_for_leaderboard = req["leaderboard"] and summary["tool_count"] > 0
    return render_comment(req, summary), {
        "ok": True,
        "leaderboard": ok_for_leaderboard,
        # 0 tools is usually an mcp-doctor miss (scan request #3), so the
        # workflow labels it for a human look instead of letting it close quietly.
        "needs_look": summary["tool_count"] == 0,
        "repo": f"{req['owner']}/{req['repo']}",
    }


def main() -> int:
    comment, outcome = handle(os.environ.get("ISSUE_BODY", ""))
    Path(os.environ.get("COMMENT_FILE", "scan-comment.md")).write_text(comment)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as fh:
            fh.write(f"ok={str(outcome['ok']).lower()}\n")
            fh.write(f"leaderboard={str(outcome['leaderboard']).lower()}\n")
            fh.write(f"needs_look={str(outcome.get('needs_look', False)).lower()}\n")
    print(json.dumps(outcome))
    return 0


if __name__ == "__main__":
    sys.exit(main())
