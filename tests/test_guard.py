"""mcp-doctor guard against a fake server that copies five real issues
(tests/fixtures/guard_server.py), each with a healthy twin that must stay quiet."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from mcp_doctor.guard import PREFIX, Tool, check_arguments, plan_reads

FIXTURE = Path(__file__).parent / "fixtures" / "guard_server.py"
ROOT = Path(__file__).resolve().parents[1]


class Session:
    def __init__(self, scenario, *opts, journal=None):
        cmd = [sys.executable, "-c", "import sys; from mcp_doctor.cli import main; sys.exit(main())",
               "guard", *opts, "--", sys.executable, str(FIXTURE), scenario]
        # Each session gets its own leak journal unless a test shares one on purpose.
        env = dict(os.environ, MCP_DOCTOR_GUARD_JOURNAL=str(journal or Path(tempfile.mkdtemp()) / "j.jsonl"))
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT, env=env)
        self.n = 0
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        self.p.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.tools = self.request("tools/list", {})["result"]["tools"]

    def request(self, method, params):
        self.n += 1
        self.p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}) + "\n").encode())
        self.p.stdin.flush()
        while True:
            msg = json.loads(self.p.stdout.readline())
            if msg.get("id") == self.n:
                return msg

    def call(self, name, args):
        res = self.request("tools/call", {"name": name, "arguments": args})["result"]
        return res, [c["text"] for c in res["content"] if c["text"].startswith(PREFIX)]

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)
        return self.p.stderr.read().decode()


@pytest.fixture
def session():
    opened = []

    def make(scenario, *opts, journal=None):
        s = Session(scenario, *opts, journal=journal)
        opened.append(s)
        return s
    yield make
    for s in opened:
        if s.p.poll() is None:
            s.close()


# servers#4138
def test_write_that_never_happened_is_flagged(session):
    res, warns = session("fs_broken").call("write_file", {"path": "/tmp/report.md", "content": "# Report\n"})
    assert res["content"][0]["text"] == "Successfully wrote to /tmp/report.md"  # the original result is kept
    assert len(warns) == 1 and "write_file reported success" in warns[0] and "ENOENT" in warns[0]
    assert res["isError"] is False  # never changed


def test_write_that_happened_is_quiet(session):
    s = session("fs_ok")
    assert s.call("write_file", {"path": "/tmp/report.md", "content": "# Report\r\n"})[1] == []
    assert s.call("create_directory", {"path": "/tmp/out"})[1] == []


# github-mcp-server#3160
def test_dropped_labels_are_flagged(session):
    _, warns = session("gh_broken").call("issue_write", {"method": "create", "owner": "o", "repo": "r", "title": "Bug", "body": "It broke", "labels": ["enhancement"]})
    assert len(warns) == 1 and "labels ('enhancement')" in warns[0] and "issue_read(method='get'" in warns[0]


def test_kept_labels_are_quiet(session):
    _, warns = session("gh_ok").call("issue_write", {"method": "create", "owner": "o", "repo": "r", "title": "Bug", "body": "It broke", "labels": ["enhancement"]})
    assert warns == []


# python-sdk#3067
def test_unknown_argument_is_flagged(session):
    res, warns = session("unknown_args").call("read_doc", {"path": "intro.md"})
    assert res["content"][0]["text"] == "topic=''"
    assert len(warns) == 1 and "'path'" in warns[0] and "takes: topic" in warns[0]


def test_known_argument_is_quiet(session):
    assert session("known_args").call("read_doc", {"topic": "intro"})[1] == []


# openai-agents-python#5170
def test_flat_args_to_kwargs_tool_are_flagged(session):
    res, warns = session("kwargs").call("with_kwargs", {"a": 1, "b": "x"})
    assert res["content"][0]["text"] == "got=[]"
    assert len(warns) == 1 and "'a', 'b'" in warns[0]


# playwright-mcp#1767
def test_short_wait_is_flagged(session):
    _, warns = session("wait_capped").call("browser_wait_for", {"time": 2})
    assert len(warns) == 1 and "asked to wait 2 s" in warns[0]


def test_full_wait_is_quiet(session):
    assert session("wait_ok").call("browser_wait_for", {"time": 1})[1] == []


def test_error_returned_as_success_is_flagged(session):
    _, warns = session("error_text").call("query_db", {"sql": "select 1"})
    assert len(warns) == 1 and "database is locked" in warns[0]
    assert session("error_ok").call("query_db", {"sql": "select 1"})[1] == []


def test_no_verify_skips_read_back(session):
    assert session("fs_broken", "--no-verify").call("write_file", {"path": "/x", "content": "y"})[1] == []


def test_log_file_gets_one_line_per_finding(session, tmp_path):
    log = tmp_path / "guard.jsonl"
    s = session("unknown_args", "--log", str(log))
    s.call("read_doc", {"path": "a"})
    s.close()
    rows = [json.loads(l) for l in log.read_text().splitlines()]
    assert [r["check"] for r in rows] == ["unknown_arguments"]


# unit-level guards on what the checks must leave alone
def test_open_schema_is_not_checked():
    t = Tool("t", {"type": "object", "properties": {"a": {}}, "additionalProperties": True}, None)
    assert check_arguments(t, {"b": 1}) == []


def test_never_verifies_with_a_tool_the_server_marks_not_read_only():
    w = Tool("write_note", {"properties": {"id": {}, "text": {}}, "required": ["id", "text"]}, {"readOnlyHint": False})
    r = Tool("get_note", {"properties": {"id": {}}, "required": ["id"]}, {"readOnlyHint": False})
    assert plan_reads(w, {"id": "1", "text": "x"}, {}, {"write_note": w, "get_note": r}, server_annotates=True) == []


def test_a_metadata_read_does_not_count_against_the_write():
    """filesystem server: get_file_info (size, dates) sorts before read_text_file."""
    w = Tool("write_file", {"properties": {"path": {}, "content": {}}, "required": ["path", "content"]}, {"readOnlyHint": False})
    info = Tool("get_file_info", {"properties": {"path": {}}, "required": ["path"]}, {"readOnlyHint": True})
    text = Tool("read_text_file", {"properties": {"path": {}}, "required": ["path"]}, {"readOnlyHint": True})
    plans = plan_reads(w, {"path": "/a", "content": "x"}, {}, {t.name: t for t in (w, info, text)}, server_annotates=True)
    assert {p.tool.name for p in plans} == {"get_file_info", "read_text_file"}


def test_a_wait_that_says_it_was_capped_is_quiet():
    """playwright-mcp after the #1767 fix reports the real wait."""
    from mcp_doctor.guard import check_wait
    honest = {"content": [{"type": "text", "text": "Waited for 30 seconds (requested 40, maximum is 30)"}]}
    claims = {"content": [{"type": "text", "text": "Waited for 40"}]}
    assert check_wait("browser_wait_for", {"time": 40}, 30.0, honest) == []
    assert len(check_wait("browser_wait_for", {"time": 40}, 30.0, claims)) == 1


# ---- leak check ---------------------------------------------------------------

PUBLIC = {"owner": "acme", "repo": "public-site"}
PRIVATE = {"owner": "acme", "repo": "private-api"}
PRIVATE_README = ("# private-api (internal only)\n"
                  "Billing service for enterprise accounts, runs on the prod-west cluster.\n"
                  "Salary bands and the 2027 hiring plan live in docs/compensation.md, do not share outside the company.\n")


def leaks(warns):
    return [w for w in warns if "prompt-injection" in w]


# GitHub MCP (Invariant, 2025): public issue -> private repo -> public PR
def test_private_repo_text_in_a_public_pr_is_flagged(session):
    s = session("gh_leak")
    s.call("get_issue", {**PUBLIC, "issue_number": 1})
    s.call("get_file_contents", {**PRIVATE, "path": "README.md"})
    _, warns = s.call("create_pull_request", {**PUBLIC, "title": "Docs", "head": "a", "base": "main",
                                              "body": "Here is what I found:\n" + PRIVATE_README})
    assert len(leaks(warns)) == 1 and "acme/private-api" in warns[0] and "acme/public-site" in warns[0]


# GitLost (Noma, 2026): public issue -> private file -> public comment
def test_private_file_in_a_public_comment_is_flagged_and_can_be_held(session):
    s = session("gh_leak", "--hold-leaks")
    s.call("get_issue", {**PUBLIC, "issue_number": 1})
    s.call("get_file_contents", {**PRIVATE, "path": "README.md"})
    res, warns = s.call("add_issue_comment", {**PUBLIC, "issue_number": 1, "body": PRIVATE_README})
    assert res["isError"] is True and "held and not sent" in warns[0]
    stored, _ = s.call("list_issue_comments", {**PUBLIC, "issue_number": 1})
    assert stored["content"][0]["text"] == "[]"  # the server never got the comment


# Supabase MCP (2025): ticket -> tokens table -> token written back out
def test_secret_from_one_query_written_by_another_is_flagged(session):
    s = session("sql_leak")
    s.call("execute_sql", {"query": "select * from support_tickets"})
    s.call("execute_sql", {"query": "select * from integration_tokens"})
    _, warns = s.call("execute_sql", {"query": "insert into support_messages(body) values ('stripe: sk_live_" + "Z" * 24 + "')"})
    assert len(leaks(warns)) == 1 and "secret" in warns[0]


def test_outside_content_read_through_another_server_counts(session, tmp_path):
    journal = tmp_path / "shared.jsonl"
    session("web", journal=journal).call("fetch", {"url": "https://docs.example"})
    s = session("gh_leak", journal=journal)
    s.call("get_file_contents", {**PRIVATE, "path": "README.md"})
    _, warns = s.call("add_issue_comment", {**PUBLIC, "issue_number": 1, "body": PRIVATE_README})
    assert len(leaks(warns)) == 1 and "fetch" in warns[0]


# normal work that must stay quiet
def test_quoting_code_from_the_same_repo_is_quiet(session):
    s = session("gh_leak")
    s.call("get_issue", {**PUBLIC, "issue_number": 1})
    build, _ = s.call("get_file_contents", {**PUBLIC, "path": "build.sh"})
    _, warns = s.call("add_issue_comment", {**PUBLIC, "issue_number": 1, "body": "The failing step:\n" + build["content"][0]["text"]})
    assert leaks(warns) == []


def test_quoting_the_issue_itself_is_quiet(session):
    s = session("gh_leak")
    s.call("get_issue", {**PUBLIC, "issue_number": 1})
    _, warns = s.call("add_issue_comment", {**PUBLIC, "issue_number": 1, "body":
        "> The docs build is broken on main and the deploy script fails halfway through.\n"
        "> Leaked public demo key for the sandbox: ghp_PUBLICDEMO000000000000000000000000000\nLooking into it."})
    assert leaks(warns) == []


def test_copying_between_repos_without_outside_content_is_quiet(session):
    s = session("gh_leak")
    s.call("get_file_contents", {**PRIVATE, "path": "README.md"})
    _, warns = s.call("create_pull_request", {"owner": "acme", "repo": "private-api-v2", "title": "Port", "head": "a", "base": "main", "body": PRIVATE_README})
    assert leaks(warns) == []


def test_one_short_shared_line_is_quiet(session):
    s = session("gh_leak")
    s.call("get_issue", {**PUBLIC, "issue_number": 1})
    s.call("get_file_contents", {**PRIVATE, "path": "README.md"})
    _, warns = s.call("add_issue_comment", {**PUBLIC, "issue_number": 1, "body": "Billing service for enterprise accounts, runs on the prod-west cluster."})
    assert leaks(warns) == []


def test_journal_holds_hashes_not_text(session, tmp_path):
    journal = tmp_path / "j.jsonl"
    s = session("gh_leak", journal=journal)
    s.call("get_file_contents", {**PRIVATE, "path": "README.md"})
    s.close()
    raw = journal.read_text()
    assert "Salary" not in raw and "prod-west" not in raw
