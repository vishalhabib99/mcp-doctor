"""Tests for the "scan my server" issue handler — parsing and rendering
only, no clone or network. The issue body is untrusted, so most of these
check that anything other than a plain github.com owner/repo URL and a
plain relative subdirectory is refused before git ever sees it."""

import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "leaderboard"))

from scan_request import RequestError, parse_request, render_comment, summarize  # noqa: E402


def _body(url="https://github.com/octo/weather-mcp", subdir="_No response_", checked=False):
    box = "[X]" if checked else "[ ]"
    return (
        f"### Repository URL\n\n{url}\n\n"
        f"### Subdirectory (optional)\n\n{subdir}\n\n"
        f"### Leaderboard\n\n- {box} Add this repo to the public leaderboard (grades only, never findings)\n"
    )


def test_parses_a_normal_form_submission():
    req = parse_request(_body())
    assert req == {"owner": "octo", "repo": "weather-mcp", "subdir": None, "leaderboard": False}


def test_opt_in_box_and_subdir_are_read():
    req = parse_request(_body(subdir="packages/server", checked=True))
    assert req["subdir"] == "packages/server"
    assert req["leaderboard"] is True


@pytest.mark.parametrize("url", [
    "https://github.com/octo/weather-mcp/",
    "https://github.com/octo/weather-mcp.git",
    "<https://github.com/octo/weather-mcp>",
])
def test_common_url_variants_are_accepted(url):
    req = parse_request(_body(url=url))
    assert (req["owner"], req["repo"]) == ("octo", "weather-mcp")


@pytest.mark.parametrize("url", [
    "https://gitlab.com/octo/weather-mcp",
    "http://github.com/octo/weather-mcp",
    "https://github.com/octo/weather-mcp; rm -rf /",
    "https://github.com/octo/$(whoami)",
    "https://github.com/octo/weather-mcp/tree/main/src",
    "https://github.com/-octo/weather-mcp",
    "https://github.com/octo/..",
    "file:///etc/passwd",
    "--upload-pack=touch /tmp/x",
])
def test_anything_but_a_plain_repo_url_is_refused(url):
    with pytest.raises(RequestError):
        parse_request(_body(url=url))


@pytest.mark.parametrize("subdir", ["../../etc", "a/../../b", "/abs/path", "src; ls", "$(id)", "a//b"])
def test_unsafe_subdirs_are_refused(subdir):
    if subdir == "/abs/path":
        # a leading slash is stripped to a plain relative path, which is safe
        assert parse_request(_body(subdir=subdir))["subdir"] == "abs/path"
        return
    with pytest.raises(RequestError):
        parse_request(_body(subdir=subdir))


def test_missing_url_is_refused_with_a_helpful_message():
    with pytest.raises(RequestError, match="issue form"):
        parse_request("please scan my server thanks")


def _report():
    return {
        "percent": 80, "grade": "B", "security_percent": 70, "security_grade": "C",
        "tools": [
            {"name": "get_weather", "issues": [
                {"tool": "get_weather", "check": "param_docs", "message": "Parameters aren't documented.",
                 "severity": "warning", "category": "quality"},
            ]},
        ],
        "repo_issues": [
            {"check": "dangerous_exec", "message": "SECRET-DETAIL shell=True in handler",
             "severity": "error", "category": "security"},
            {"check": "ssrf", "message": "SECRET-DETAIL unvalidated URL fetch",
             "severity": "warning", "category": "security"},
        ],
    }


def test_summary_splits_quality_from_security():
    summary = summarize(_report())
    assert summary["tool_count"] == 1
    assert len(summary["quality_issues"]) == 1
    assert summary["security_counts"] == Counter({"error": 1, "warning": 1})


def test_security_details_never_reach_the_public_comment():
    comment = render_comment(parse_request(_body()), summarize(_report()))
    assert "SECRET-DETAIL" not in comment
    assert "dangerous_exec" not in comment
    assert "1 error, 1 warning." in comment
    assert "param_docs" in comment  # quality findings are safe to list


def test_leaderboard_note_only_when_opted_in():
    summary = summarize(_report())
    assert "public leaderboard" in render_comment(parse_request(_body(checked=True)), summary)
    assert "public leaderboard" not in render_comment(parse_request(_body(checked=False)), summary)


def test_pipes_in_messages_cannot_break_the_table():
    report = _report()
    report["tools"][0]["issues"][0]["message"] = "a | b\nc"
    comment = render_comment(parse_request(_body()), summarize(report))
    assert "a \\| b c" in comment


def test_backticks_in_a_bad_url_cannot_break_out_of_the_code_span():
    with pytest.raises(RequestError) as exc:
        parse_request(_body(url="`x` @someone **bold**"))
    assert str(exc.value).startswith("`'x' @someone **bold**` isn't")
