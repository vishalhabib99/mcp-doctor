"""Serves tests/fixtures/guard_server.py over Streamable HTTP for tests/test_guard.py:
JSON replies to even request ids, event-stream replies to odd ones, a session id
from initialize on, 202 for notifications, 405 for the optional GET stream.

    python http_bridge.py <scenario>   (prints the port on its first line)
"""
import json
import queue
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SERVER = subprocess.Popen([sys.executable, str(Path(__file__).parent / "guard_server.py"), sys.argv[1]],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE)
waiting: dict = {}
lock = threading.Lock()
SESSION = "test-session-1"
VERSION_KEY = "io.modelcontextprotocol/protocolVersion"


def read_server():
    for line in SERVER.stdout:
        msg = json.loads(line)
        with lock:
            box = waiting.pop(msg.get("id"), None)
        if box is not None:
            box.put(msg)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        meta = (msg.get("params") or {}).get("_meta") or {}
        if VERSION_KEY in meta:
            # 2026-07-28: no session; the version, method and name ride as headers that must match the body.
            name = (msg.get("params") or {}).get("name")
            if (self.headers.get("MCP-Protocol-Version") != meta[VERSION_KEY] or self.headers.get("Mcp-Method") != msg.get("method")
                    or (name is not None and self.headers.get("Mcp-Name") != name)):
                self.send_response(400)
                self.end_headers()
                return
        elif msg.get("method") != "initialize" and self.headers.get("Mcp-Session-Id") != SESSION:
            self.send_response(400)
            self.end_headers()
            return
        if "id" not in msg:
            SERVER.stdin.write((json.dumps(msg) + "\n").encode())
            SERVER.stdin.flush()
            self.send_response(202)
            self.end_headers()
            return
        box = queue.Queue(1)
        with lock:
            waiting[msg["id"]] = box
        SERVER.stdin.write((json.dumps(msg) + "\n").encode())
        SERVER.stdin.flush()
        reply = json.dumps(box.get(timeout=30))
        sse = isinstance(msg["id"], int) and msg["id"] % 2 == 1
        body = (f"event: message\ndata: {reply}\n\n" if sse else reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if sse else "application/json")
        self.send_header("Mcp-Session-Id", SESSION)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.send_response(405)
        self.end_headers()

    def do_DELETE(self):
        self.send_response(200)
        self.end_headers()


threading.Thread(target=read_server, daemon=True).start()
httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
print(httpd.server_address[1], flush=True)
httpd.serve_forever()
