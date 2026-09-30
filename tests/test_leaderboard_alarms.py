"""Tests for the leaderboard's tool-count alarm: pure comparison, no network."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "leaderboard"))

from scan import tool_count_alarms  # noqa: E402


def _row(repo, tools):
    return {"repo": repo, "url": f"https://github.com/{repo}", "tool_count": tools}


def test_drop_to_zero_alarms():
    # bruchris/canvas-lms-mcp: 166 tools, then a registration style mcp-doctor
    # couldn't read would show 0 and still get a grade.
    alarms = tool_count_alarms([_row("bruchris/canvas-lms-mcp", 0)], {"bruchris/canvas-lms-mcp": {"tool_count": 166}})
    assert alarms == ["- [`bruchris/canvas-lms-mcp`](https://github.com/bruchris/canvas-lms-mcp): 166 → 0 tools"]


def test_large_drop_alarms_small_drop_does_not():
    previous = {"a/big-drop": {"tool_count": 100}, "a/small-drop": {"tool_count": 100}}
    alarms = tool_count_alarms([_row("a/big-drop", 79), _row("a/small-drop", 80)], previous)
    assert len(alarms) == 1 and "a/big-drop" in alarms[0]


def test_growth_and_new_repos_are_quiet():
    previous = {"a/grew": {"tool_count": 10}}
    assert tool_count_alarms([_row("a/grew", 25), _row("a/new", 3)], previous) == []


def test_repo_missing_from_this_scan_alarms():
    # A clone or scan failure drops the row from data.json without a word.
    alarms = tool_count_alarms([], {"a/gone": {"tool_count": 12}})
    assert alarms == ["- [`a/gone`](https://github.com/a/gone): not scanned this run (had 12 tools)"]


def test_repo_that_already_had_zero_tools_is_not_repeated():
    assert tool_count_alarms([_row("a/zero", 0)], {"a/zero": {"tool_count": 0}}) == []
