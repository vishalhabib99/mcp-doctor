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
    def __init__(self, scenario, *opts, journal=None, http=False):
        self.bridge = None
        if http:
            # The same fake server, reached over Streamable HTTP (guard --url).
            self.bridge = subprocess.Popen([sys.executable, str(FIXTURE.parent / "http_bridge.py"), scenario],
                                           stdout=subprocess.PIPE)
            port = int(self.bridge.stdout.readline())
            target = ["--url", f"http://127.0.0.1:{port}/mcp", "--header", "X-Test=1"]
        else:
            target = ["--", sys.executable, str(FIXTURE), scenario]
        cmd = [sys.executable, "-c", "import sys; from mcp_doctor.cli import main; sys.exit(main())",
               "guard", *opts, *target]
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
        if self.bridge is not None:
            self.bridge.kill()
        return self.p.stderr.read().decode()


@pytest.fixture
def session():
    opened = []

    def make(scenario, *opts, journal=None, http=False):
        s = Session(scenario, *opts, journal=journal, http=http)
        opened.append(s)
        return s
    yield make
    for s in opened:
        if s.p.poll() is None:
            s.close()
        elif s.bridge is not None:
            s.bridge.kill()


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


# ---- is the answer real (folded in from mcp-reality-check) ---------------------

def test_refusal_empty_read_and_schema_break_are_flagged(session):
    s = session("reality_bad")
    assert "refusal" in s.call("get_weather", {"city": "Paris"})[1][0]
    assert "returned nothing" in s.call("read_notes", {"topic": "q3"})[1][0]
    assert "outputSchema" in s.call("count_items", {"kind": "open"})[1][0] and "count should be integer" in s.call("count_items", {"kind": "open"})[1][0]


def test_real_answers_quoted_refusals_and_empty_deletes_are_quiet(session):
    s = session("reality_ok")
    for name, args in [("get_weather", {"city": "Paris"}), ("read_notes", {"topic": "q3"}), ("count_items", {"kind": "open"}),
                       ("read_readme", {}), ("delete_note", {"id": "n1"})]:
        assert s.call(name, args)[1] == [], name


def test_refusal_phrasings_carried_over_from_reality_check():
    from mcp_doctor.guard import check_refusal

    def flags(text):
        return bool(check_refusal("t", {"content": [{"type": "text", "text": text}]}))
    for refusal in ["I don't have access to real-time data.", "I'm sorry, but I cannot do that.",
                    "As an AI, I have no way to browse the web.", "Unfortunately, I cannot process this request.",
                    "As an AI language model, I cannot browse the internet.", "I have no access to the user's calendar.",
                    '{"result": "I\'m sorry, but I can\'t provide that."}']:
        assert flags(refusal), refusal
    for answer in ["The weather in Paris is sunny and 72F.", "Sorry for the delay, here are your results: 3 items found.",
                   "Structured record of work as an AI Product Manager.", "An agent has no access to server source, only metadata.",
                   "x" * 300 + " I'm sorry, but I can't help with that."]:
        assert not flags(answer), answer
    assert not check_refusal("t", {"isError": True, "content": [{"type": "text", "text": "I'm sorry, but no."}]})


def test_schema_checker_agrees_on_the_common_keywords():
    from mcp_doctor.guard import schema_error
    schema = {"type": "object", "required": ["count"], "properties": {"count": {"type": "integer"},
              "tags": {"type": "array", "items": {"type": "string"}}, "id": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}
    assert schema_error({"count": 3, "tags": ["a"], "id": None}, schema) is None
    assert "missing required field 'count'" in schema_error({}, schema)
    assert "count should be integer" in schema_error({"count": True}, schema)
    assert "tags[1]" in schema_error({"count": 1, "tags": ["a", 2]}, schema)
    assert "anyOf" in schema_error({"count": 1, "id": 5}, schema)
    assert schema_error({"count": 1.5}, {"properties": {"count": {"type": "number"}}}) is None
    assert schema_error({"x": 1}, {"type": "object", "patternProperties": {"^x": {"type": "string"}}}) is None  # unknown keyword: accept


# --url: the same checks against a server reached over Streamable HTTP
# (tests/fixtures/http_bridge.py: JSON and event-stream replies, a session id).
def test_http_write_that_never_happened_is_flagged_and_twin_is_quiet(session):
    res, warns = session("fs_broken", http=True).call("write_file", {"path": "/tmp/report.md", "content": "# Report\n"})
    assert res["content"][0]["text"] == "Successfully wrote to /tmp/report.md"
    assert len(warns) == 1 and "write_file reported success" in warns[0]
    assert session("fs_ok", http=True).call("write_file", {"path": "/tmp/report.md", "content": "# Report\n"})[1] == []


def test_http_dropped_labels_and_unknown_arguments_are_flagged(session):
    _, warns = session("gh_broken", http=True).call("issue_write", {"method": "create", "owner": "o", "repo": "r", "title": "Bug", "body": "It broke", "labels": ["enhancement"]})
    assert len(warns) == 1 and "labels ('enhancement')" in warns[0]
    _, warns = session("unknown_args", http=True).call("read_doc", {"path": "intro.md"})
    assert len(warns) == 1 and "'path'" in warns[0]


def test_http_refusal_empty_and_schema_break_are_flagged(session):
    s = session("reality_bad", http=True)
    assert "refusal" in s.call("get_weather", {"city": "Paris"})[1][0]
    assert "returned nothing" in s.call("read_notes", {"topic": "q3"})[1][0]
    assert "outputSchema" in s.call("count_items", {"kind": "open"})[1][0]


def test_http_leak_is_held(session):
    s = session("gh_leak", "--hold-leaks", http=True)
    s.call("get_issue", {**PUBLIC, "issue_number": 1})
    s.call("get_file_contents", {**PRIVATE, "path": "README.md"})
    res, warns = s.call("add_issue_comment", {**PUBLIC, "issue_number": 1, "body": PRIVATE_README})
    assert res["isError"] is True and "held and not sent" in warns[0]
    stored, _ = s.call("list_issue_comments", {**PUBLIC, "issue_number": 1})
    assert stored["content"][0]["text"] == "[]"


def test_http_unreachable_server_answers_with_an_error(tmp_path):
    cmd = [sys.executable, "-c", "import sys; from mcp_doctor.cli import main; sys.exit(main())",
           "guard", "--url", "http://127.0.0.1:9/mcp"]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT)
    out, _ = p.communicate(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n', timeout=30)
    msg = json.loads(out.splitlines()[0])
    assert msg["id"] == 1 and "could not reach" in msg["error"]["message"]


# --otel: one span per call with guard's verdict, in OTel MCP semantic-convention shape
def spans_of(path):
    out = []
    for line in Path(path).read_text().splitlines():
        for rs in json.loads(line)["resourceSpans"]:
            for ss in rs["scopeSpans"]:
                out += ss["spans"]
    return out


def attr(span, key):
    for a in span["attributes"]:
        if a["key"] == key:
            v = a["value"]
            return v.get("stringValue", v.get("arrayValue", {}).get("values"))
    return None


def test_otel_span_per_call_carries_the_verdict(session, tmp_path):
    out = tmp_path / "spans.jsonl"
    s = session("fs_ok", "--otel", str(out))
    s.call("write_file", {"path": "/tmp/report.md", "content": "# Report\n"})
    s.call("list_directory", {"path": "/tmp"})
    s.call("read_text_file", {"path": "/nope"})
    s.close()
    s = session("fs_broken", "--otel", str(out))
    s.call("write_file", {"path": "/tmp/report.md", "content": "# Report\n"})
    s.close()
    spans = spans_of(out)
    assert [(sp["name"], attr(sp, "mcp_doctor.guard.verdict")) for sp in spans] == [
        ("tools/call write_file", "confirmed"), ("tools/call list_directory", "passed"),
        ("tools/call read_text_file", "error"), ("tools/call write_file", "flagged")]
    ok, listed, err, bad = spans
    assert attr(ok, "mcp.method.name") == "tools/call" and attr(ok, "gen_ai.tool.name") == "write_file"
    assert attr(ok, "network.transport") == "pipe" and attr(ok, "mcp.protocol.version") == "2025-06-18"
    assert ok["kind"] == 3 and ok["status"] == {} and int(ok["endTimeUnixNano"]) >= int(ok["startTimeUnixNano"])
    assert attr(err, "error.type") == "tool_error" and err["status"]["code"] == 2
    assert bad["status"]["code"] == 2 and "write_file reported success" in bad["status"]["message"]
    assert [e["name"] for e in bad["events"]] == ["mcp_doctor.guard.finding"]
    assert [v["stringValue"] for v in attr(bad, "mcp_doctor.guard.checks")] == [bad["events"][0]["attributes"][0]["value"]["stringValue"]]


def test_otel_never_exports_arguments_or_results(session, tmp_path):
    out = tmp_path / "spans.jsonl"
    s = session("fs_ok", "--otel", str(out))
    s.call("write_file", {"path": "/tmp/a.md", "content": "TOP-SECRET-CONTENT"})
    s.call("read_text_file", {"path": "/tmp/a.md"})
    s.close()
    assert "TOP-SECRET-CONTENT" not in out.read_text()


def test_otel_span_joins_the_clients_trace(session, tmp_path):
    out = tmp_path / "spans.jsonl"
    s = session("fs_ok", "--otel", str(out))
    tp = "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01"
    s.request("tools/call", {"name": "list_directory", "arguments": {"path": "/"}, "_meta": {"traceparent": tp}})
    s.call("list_directory", {"path": "/"})
    s.close()
    joined, fresh = spans_of(out)
    assert joined["traceId"] == "ab" * 16 and joined["parentSpanId"] == "cd" * 8
    assert "parentSpanId" not in fresh and len(fresh["traceId"]) == 32 and fresh["traceId"] != joined["traceId"]


def test_otel_held_leak_is_exported_as_held(session, tmp_path):
    out = tmp_path / "spans.jsonl"
    s = session("gh_leak", "--hold-leaks", "--otel", str(out))
    s.call("get_issue", {**PUBLIC, "issue_number": 1})
    s.call("get_file_contents", {**PRIVATE, "path": "README.md"})
    s.call("add_issue_comment", {**PUBLIC, "issue_number": 1, "body": PRIVATE_README})
    s.close()
    held = spans_of(out)[-1]
    assert attr(held, "gen_ai.tool.name") == "add_issue_comment" and attr(held, "mcp_doctor.guard.verdict") == "held"
    assert held["status"]["code"] == 2


def test_otel_posts_to_an_otlp_http_endpoint_from_env(tmp_path):
    import http.server
    import threading

    got = []

    class Collector(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append((self.path, self.headers.get("Content-Type"), self.headers.get("Authorization"),
                        json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Collector)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://127.0.0.1:{srv.server_port}"
    os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = "Authorization=Bearer%20t0k"
    try:
        s = Session("fs_ok", "--otel")
        s.call("list_directory", {"path": "/"})
        s.close()
    finally:
        del os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"], os.environ["OTEL_EXPORTER_OTLP_HEADERS"]
        srv.shutdown()
    path, ctype, auth, body = got[0]
    assert path == "/v1/traces" and ctype == "application/json" and auth == "Bearer t0k"
    span = body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["name"] == "tools/call list_directory"


def test_otel_dead_collector_never_costs_a_call(session):
    s = session("fs_ok", "--otel", "http://127.0.0.1:9")
    res, warns = s.call("list_directory", {"path": "/"})
    assert res["content"][0]["text"] == "[DIR] /" and warns == []
    assert s.close().count("couldn't export spans") == 1
