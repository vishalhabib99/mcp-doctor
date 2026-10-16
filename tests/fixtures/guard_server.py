"""A tiny stdio MCP server for tests/test_guard.py. Each scenario copies the
behavior described in a real issue, plus a healthy twin that must stay quiet.

    python guard_server.py <scenario>
"""
import json
import sys
import time

SCENARIO = sys.argv[1]
RO = {"readOnlyHint": True}
RW = {"readOnlyHint": False, "destructiveHint": False}


def tool(name, props, required=(), annotations=None, **schema_extra):
    t = {"name": name, "description": name, "inputSchema": {"type": "object", "properties": props, "required": list(required), **schema_extra}}
    if annotations is not None:
        t["annotations"] = annotations
    return t


def text(s, is_error=False):
    return {"content": [{"type": "text", "text": s}], "isError": is_error}


files = {}
issues = {}

# servers#4138: write_file says "Successfully wrote" on Windows and writes nothing.
FS = [
    tool("write_file", {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"], RW),
    tool("get_file_info", {"path": {"type": "string"}}, ["path"], RO),  # metadata only, like the real server
    tool("read_text_file", {"path": {"type": "string"}, "head": {"type": "number"}}, ["path"], RO),
    tool("create_directory", {"path": {"type": "string"}}, ["path"], RW),
    tool("list_directory", {"path": {"type": "string"}}, ["path"], RO),
]
# github-mcp-server#3160: issue_write drops labels without permission, still succeeds.
METHODS_W = {"type": "string", "enum": ["create", "update"]}
METHODS_R = {"type": "string", "enum": ["get", "get_comments", "get_labels"]}
GH = [
    tool("issue_write", {"method": METHODS_W, "owner": {"type": "string"}, "repo": {"type": "string"}, "issue_number": {"type": "number"},
                         "title": {"type": "string"}, "body": {"type": "string"}, "labels": {"type": "array", "items": {"type": "string"}}},
         ["method", "owner", "repo"], RW),
    tool("issue_read", {"method": METHODS_R, "owner": {"type": "string"}, "repo": {"type": "string"}, "issue_number": {"type": "number"}},
         ["method", "owner", "repo", "issue_number"], RO),
]
# python-sdk#3067: unknown argument names are dropped and the tool runs with defaults.
DOC = [tool("read_doc", {"topic": {"type": "string", "default": ""}})]
# openai-agents-python#5170: a **kwargs tool's schema nests everything under `kwargs`.
KW = [tool("with_kwargs", {"kwargs": {"type": "object", "additionalProperties": True}})]
# playwright-mcp#1767: browser_wait_for caps the wait but reports the requested time.
WAIT = [tool("browser_wait_for", {"time": {"type": "number"}, "text": {"type": "string"}}, [], RO)]
ERR = [tool("query_db", {"sql": {"type": "string"}}, ["sql"], RO)]

TOOLS = {"fs_broken": FS, "fs_ok": FS, "gh_broken": GH, "gh_ok": GH, "unknown_args": DOC, "known_args": DOC,
         "kwargs": KW, "wait_capped": WAIT, "wait_ok": WAIT, "error_text": ERR, "error_ok": ERR}[SCENARIO]


def call(name, a):
    if name == "write_file":
        if SCENARIO == "fs_ok":
            files[a["path"]] = a["content"]
        return text(f"Successfully wrote to {a['path']}")
    if name == "read_text_file":
        if a["path"] in files:
            return text(files[a["path"]])
        return text(f"Error: ENOENT: no such file or directory, open '{a['path']}'", True)
    if name == "get_file_info":
        if a["path"] in files:
            return text(f"size: {len(files[a['path']])}\nisFile: true")
        return text(f"Error: ENOENT: no such file or directory, stat '{a['path']}'", True)
    if name == "create_directory":
        files[a["path"] + "/"] = ""
        return text(f"Successfully created directory {a['path']}")
    if name == "list_directory":
        return text("[DIR] " + a["path"])
    if name == "issue_write":
        n = a.get("issue_number") or len(issues) + 1
        labels = a.get("labels", []) if SCENARIO == "gh_ok" else []
        issues[n] = {"number": n, "title": a.get("title", ""), "body": a.get("body", ""), "labels": [{"name": l} for l in labels]}
        return text(json.dumps({"id": "I_kw" + str(n), "url": f"https://github.com/{a['owner']}/{a['repo']}/issues/{n}"}))
    if name == "issue_read":
        issue = issues.get(a["issue_number"])
        return text(json.dumps(issue)) if issue else text("Error: issue not found", True)
    if name == "read_doc":
        return text(f"topic={a.get('topic', '')!r}")
    if name == "with_kwargs":
        return text(f"got={sorted((a.get('kwargs') or {}).items())}")
    if name == "browser_wait_for":
        t = a.get("time", 0)
        time.sleep(min(t, 0.5) if SCENARIO == "wait_capped" else t)
        return text(f"Waited for {t}")
    if name == "query_db":
        return text("Error: database is locked") if SCENARIO == "error_text" else text("3 rows")
    return text(f"unknown tool {name}", True)


for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    m, p = msg.get("method"), msg.get("params") or {}
    if m == "initialize":
        res = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "0"}}
    elif m == "tools/list":
        res = {"tools": TOOLS}
    elif m == "tools/call":
        res = call(p["name"], p.get("arguments") or {})
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "no"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}), flush=True)
