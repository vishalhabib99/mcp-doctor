"""Tests for comment/post.py: the workflow_run job that posts a fork PR's
report. The artifact is untrusted, so the PR comes from the event and the
review is rebuilt field by field."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("post", ROOT / "comment" / "post.py")
post = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(post)

SHA = "a" * 40
SUGGESTION = "```suggestion\ndef f(x: str | None = None):\n```"


class FakeGitHub:
    def __init__(self, pulls, comments=()):
        self.pulls, self.comments, self.calls = pulls, list(comments), []

    def call(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "GET" and path.startswith("pulls?"):
            return self.pulls
        if method == "GET" and path.startswith("issues/"):
            return self.comments
        return {}


def _handoff(tmp_path, comment=None, review=None):
    d = tmp_path / "handoff"
    d.mkdir()
    if comment is not None:
        (d / "comment.md").write_text(comment)
    if review is not None:
        (d / "review.json").write_text(json.dumps(review))
    return d


def test_posts_comment_on_the_pr_whose_head_was_scanned(tmp_path):
    gh = FakeGitHub([{"number": 7, "head": {"sha": "b" * 40}}, {"number": 9, "head": {"sha": SHA}}])
    post.run(gh, _handoff(tmp_path, comment="### mcp-doctor report"), "someone", "fix/x&y", SHA)
    method, path, payload = gh.calls[-1]
    assert (method, path) == ("POST", "issues/9/comments")
    assert payload["body"].startswith(post.MARKER)
    assert "head=someone:fix%2Fx%26y" in gh.calls[0][1]  # branch name is URL-quoted


def test_edits_its_own_earlier_comment(tmp_path):
    mine = {"id": 55, "user": {"login": "github-actions[bot]"}, "body": post.MARKER + "\nold"}
    theirs = {"id": 56, "user": {"login": "someone"}, "body": post.MARKER}
    gh = FakeGitHub([{"number": 9, "head": {"sha": SHA}}], [mine, theirs])
    post.run(gh, _handoff(tmp_path, comment="new"), "someone", "b", SHA)
    assert gh.calls[-1][:2] == ("PATCH", "issues/comments/55")


def test_stale_report_is_dropped(tmp_path):
    gh = FakeGitHub([{"number": 9, "head": {"sha": "c" * 40}}])
    post.run(gh, _handoff(tmp_path, comment="x"), "someone", "b", SHA)
    assert all(m == "GET" for m, _, _ in gh.calls)


def test_no_artifact_does_nothing(tmp_path):
    gh = FakeGitHub([])
    d = tmp_path / "missing"
    assert post.run(gh, d, "o", "b", SHA) == 0
    assert gh.calls == []


def test_mentions_are_defused_and_body_capped():
    body = post.comment_body("ping @octocat and team@ example " + "x" * 70000)
    assert "@octocat" not in body and "@​octocat" in body
    assert len(body) < 65536 and body.endswith("_(report truncated)_\n")


def test_review_is_rebuilt_and_pinned_to_event_sha():
    raw = {
        "commit_id": "f" * 40, "event": "APPROVE", "body": "hi @admin",
        "comments": [
            {"path": "server.py", "line": 4, "start_line": 3, "body": SUGGESTION, "extra": 1},
            {"path": "server.py", "line": "4", "body": SUGGESTION},       # wrong type
            {"path": "server.py", "line": 5, "body": "plain text, no suggestion"},
            {"path": "server.py", "line": 6, "start_line": 9, "body": SUGGESTION},  # bad range
            "junk",
        ],
    }
    review = post.clean_review(raw, SHA)
    assert review["commit_id"] == SHA and review["event"] == "COMMENT"
    assert review["body"] == "hi @​admin"
    assert review["comments"] == [
        {"path": "server.py", "line": 4, "side": "RIGHT", "body": SUGGESTION, "start_line": 3, "start_side": "RIGHT"},
        {"path": "server.py", "line": 6, "side": "RIGHT", "body": SUGGESTION},
    ]


def test_garbage_review_posts_nothing(tmp_path):
    gh = FakeGitHub([{"number": 9, "head": {"sha": SHA}}])
    d = _handoff(tmp_path)
    (d / "review.json").write_text("not json")
    post.run(gh, d, "o", "b", SHA)
    assert not any(m == "POST" for m, _, _ in gh.calls)
    assert post.clean_review({"comments": "nope"}, SHA) is None
