"""Post the mcp-doctor report saved by a pull-request run that couldn't post it.

A PR from a fork gets a read-only token, so the scan run uploads its comment
and review suggestions as an artifact instead. This script runs in a
`workflow_run` job with a write token. It treats the artifact as untrusted
text: the PR is looked up from the event (head owner, branch and SHA), never
from the artifact, and the review is rebuilt field by field. Stdlib only, so
the comment workflow installs nothing.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MARKER = "<!-- mcp-doctor-report -->"
MAX_COMMENT = 60000  # GitHub caps a comment at 65,536 characters
MAX_SUGGESTIONS = 50
BOT = "github-actions[bot]"

# A zero-width space after "@" keeps the text readable but stops it pinging anyone.
_MENTION = re.compile(r"@(?=[A-Za-z0-9])")


def defuse_mentions(text: str) -> str:
    return _MENTION.sub("@​", text)


def comment_body(raw: str) -> str:
    body = defuse_mentions(raw.replace(MARKER, ""))
    if len(body) > MAX_COMMENT:
        body = body[:MAX_COMMENT] + "\n\n_(report truncated)_\n"
    return f"{MARKER}\n{body}"


def clean_review(raw: object, head_sha: str) -> dict | None:
    """Keep only well-formed suggestion comments, pinned to the event's head SHA."""
    if not isinstance(raw, dict) or not isinstance(raw.get("comments"), list):
        return None
    comments = []
    for c in raw["comments"][:MAX_SUGGESTIONS]:
        if not isinstance(c, dict):
            continue
        path, line, body = c.get("path"), c.get("line"), c.get("body")
        if not (isinstance(path, str) and path and isinstance(line, int) and line > 0
                and isinstance(body, str) and "```suggestion" in body and len(body) < 20000):
            continue
        clean = {"path": path, "line": line, "side": "RIGHT", "body": defuse_mentions(body)}
        start = c.get("start_line")
        if isinstance(start, int) and 0 < start < line:
            clean.update(start_line=start, start_side="RIGHT")
        comments.append(clean)
    if not comments:
        return None
    body = raw.get("body") if isinstance(raw.get("body"), str) else ""
    return {"commit_id": head_sha, "event": "COMMENT",
            "body": defuse_mentions(body[:2000]), "comments": comments}


def pick_pr(pulls: list[dict], head_sha: str) -> int | None:
    """The open PR whose head is exactly the commit that was scanned. A newer
    push means a newer run will post, so a stale report is dropped."""
    for p in pulls:
        if p.get("head", {}).get("sha") == head_sha:
            return p["number"]
    return None


class GitHub:
    def __init__(self, token: str, repo: str, api: str = "https://api.github.com"):
        self.token, self.repo, self.api = token, repo, api.rstrip("/")

    def call(self, method: str, path: str, payload: dict | None = None):
        req = urllib.request.Request(
            f"{self.api}/repos/{self.repo}/{path}", method=method,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read()
        return json.loads(text) if text else None


def warn(msg: str) -> None:
    print(f"::warning::mcp-doctor: {msg}")


def run(gh: GitHub, handoff: Path, head_owner: str, head_branch: str, head_sha: str) -> int:
    comment_file, review_file = handoff / "comment.md", handoff / "review.json"
    if not comment_file.exists() and not review_file.exists():
        print("mcp-doctor: no saved report for this run (the scan posted its own comment, or there was no PR).")
        return 0

    head = urllib.parse.quote(f"{head_owner}:{head_branch}", safe=":")
    pr = pick_pr(gh.call("GET", f"pulls?state=open&head={head}&per_page=100") or [], head_sha)
    if pr is None:
        print(f"mcp-doctor: no open PR has head {head_sha[:7]} any more; skipping the stale report.")
        return 0

    if comment_file.exists():
        body = comment_body(comment_file.read_text(errors="replace"))
        mine = [c for c in gh.call("GET", f"issues/{pr}/comments?per_page=100") or []
                if c.get("user", {}).get("login") == BOT and MARKER in (c.get("body") or "")]
        try:
            if mine:
                gh.call("PATCH", f"issues/comments/{mine[-1]['id']}", {"body": body})
            else:
                gh.call("POST", f"issues/{pr}/comments", {"body": body})
            print(f"mcp-doctor: report posted on PR #{pr}.")
        except urllib.error.HTTPError as e:
            warn(f"couldn't post the report on PR #{pr} ({e.code}); the workflow needs pull-requests: write")

    if review_file.exists():
        try:
            review = clean_review(json.loads(review_file.read_text()), head_sha)
        except ValueError:
            review = None
        if review:
            try:
                gh.call("POST", f"pulls/{pr}/reviews", review)
                print(f"mcp-doctor: {len(review['comments'])} fix suggestion(s) posted on PR #{pr}.")
            except urllib.error.HTTPError as e:
                warn(f"couldn't post fix suggestions on PR #{pr} ({e.code})")
    return 0


def main() -> int:
    env = os.environ
    gh = GitHub(env["GH_TOKEN"], env["REPO"], env.get("GITHUB_API_URL", "https://api.github.com"))
    return run(gh, Path(env.get("HANDOFF_DIR", "mcp-doctor-handoff")),
               env["HEAD_OWNER"], env["HEAD_BRANCH"], env["HEAD_SHA"])


if __name__ == "__main__":
    sys.exit(main())
