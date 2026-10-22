"""Guard verdicts as OpenTelemetry spans, so they land in the trace store a team
already runs (any OTLP endpoint: an OTel Collector, Jaeger, Grafana Tempo,
Langfuse, Phoenix, Datadog...) instead of a new one.

One span per tools/call, named and attributed per the OTel MCP semantic
conventions (`tools/call <tool>`, mcp.method.name, gen_ai.tool.name,
jsonrpc.request.id, error.type=tool_error), plus what guard adds on top:

- mcp_doctor.guard.verdict: confirmed (a read found the write), passed (a
  read or other call cleared every check), unconfirmed (a write no read could
  check), flagged (a check found a "success" that isn't one), held
  (--hold-leaks stopped it) or error (the server itself said it failed)
- mcp_doctor.guard.checks: the checks that fired, and one span event per finding

A flagged or held call gets span status ERROR: the server said success, guard
found it wasn't. Arguments and results are never exported (they're opt-in in
the conventions for that reason); a finding's message can quote a short bit of
an argument, such as the path that didn't get written.

If the client sent a W3C traceparent in params._meta, the span joins that
trace, so it shows up under the agent's own tool-call span.

`guard hook` exports through the same class: an agent's built-in tools (Bash,
Write, WebFetch) get `execute_tool <tool>` spans without the MCP attributes,
and every call of one agent session shares a trace.

Stdlib only, OTLP/HTTP with JSON bodies. The target is a URL (spans are POSTed
to <url>/v1/traces) or a file (one ExportTraceServiceRequest per line, the
format the Collector's otlpjsonfile receiver reads). Export runs on its own
thread and never delays or fails a call.
"""
from __future__ import annotations

import json
import os
import queue
import re
import secrets
import threading
import time
import urllib.parse
from typing import Any

_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")
_KIND_CLIENT = 3
_STATUS_ERROR = 2


def _value(v: Any) -> dict:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [_value(x) for x in v]}}
    return {"stringValue": str(v)}


def _attrs(d: dict) -> list[dict]:
    return [{"key": k, "value": _value(v)} for k, v in d.items() if v is not None]


def parent_of(params: Any) -> tuple[str, str | None]:
    """(trace id, parent span id) from params._meta.traceparent, or a new trace."""
    meta = params.get("_meta") if isinstance(params, dict) else None
    tp = meta.get("traceparent") if isinstance(meta, dict) else None
    m = _TRACEPARENT.match(tp.strip().lower()) if isinstance(tp, str) else None
    if m and set(m.group(1)) != {"0"} and set(m.group(2)) != {"0"}:
        return m.group(1), m.group(2)
    return secrets.token_hex(16), None


def env_target() -> str | None:
    """The standard OTEL_EXPORTER_OTLP_* endpoint, already resolved to /v1/traces."""
    traces = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if traces:
        return traces
    base = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    return base.rstrip("/") + "/v1/traces" if base else None


def env_headers() -> dict[str, str]:
    raw = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS") or os.environ.get("OTEL_EXPORTER_OTLP_HEADERS") or ""
    out = {}
    for pair in raw.split(","):
        k, sep, v = pair.partition("=")
        if sep and k.strip():
            out[urllib.parse.unquote(k.strip())] = urllib.parse.unquote(v.strip())
    return out


class SpanExporter:
    def __init__(self, target: str, err, headers: dict[str, str] | None = None, version: str = "",
                 service: str | None = None, transport: str | None = "pipe", timeout: float = 10):
        if target.startswith(("http://", "https://")) and not target.rstrip("/").endswith("/v1/traces"):
            target = target.rstrip("/") + "/v1/traces"
        self.target = target
        self.http = target.startswith(("http://", "https://"))
        self.headers = headers or {}
        self.err = err
        self.version = version
        self.transport = transport
        self.timeout = timeout
        self.resource = _attrs({
            "service.name": service or os.environ.get("OTEL_SERVICE_NAME") or "mcp-doctor-guard",
            "telemetry.sdk.name": "mcp-doctor", "telemetry.sdk.language": "python", "telemetry.sdk.version": version,
        })
        self.q: queue.Queue = queue.Queue()
        self.warned = False
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def span(self, *, tool: str, request_id: Any, params: Any, start_ns: int, end_ns: int, verdict: str,
             findings: list, tool_error: bool = False, rpc_error: dict | None = None,
             session: str | None = None, protocol: str | None = None, server: str | None = None,
             mcp: bool = True, trace_id: str | None = None, extra: dict | None = None) -> None:
        """One span for one call. mcp=False is an agent's built-in tool (no MCP
        attributes); trace_id puts the span in a given trace when params carry none."""
        trace, parent = parent_of(params)
        if trace_id and parent is None:
            trace = trace_id
        attrs: dict[str, Any] = {
            "mcp.method.name": "tools/call" if mcp else None,
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": tool,
            "jsonrpc.request.id": str(request_id) if request_id is not None else None,
            "mcp.session.id": session,
            "mcp.protocol.version": protocol,
            "network.transport": self.transport,
            "server.address": server,
            **(extra or {}),
            "mcp_doctor.guard.verdict": verdict,
            "mcp_doctor.guard.checks": sorted({f.check for f in findings}) or None,
        }
        status: dict = {}
        if rpc_error is not None:
            code = rpc_error.get("code")
            attrs["error.type"] = str(code) if code is not None else "_OTHER"
            attrs["rpc.response.status_code"] = str(code) if code is not None else None
            status = {"code": _STATUS_ERROR, "message": str(rpc_error.get("message", ""))[:500]}
        elif tool_error:
            attrs["error.type"] = "tool_error"
            status = {"code": _STATUS_ERROR}
        if verdict in ("flagged", "held") and findings:
            status = {"code": _STATUS_ERROR, "message": findings[0].message[:500]}
        method = "tools/call" if mcp else "execute_tool"
        span = {
            "traceId": trace,
            "spanId": secrets.token_hex(8),
            "name": f"{method} {tool}" if tool else method,
            "kind": _KIND_CLIENT,
            "startTimeUnixNano": str(start_ns),
            "endTimeUnixNano": str(end_ns),
            "attributes": _attrs(attrs),
            "events": [{"timeUnixNano": str(end_ns), "name": "mcp_doctor.guard.finding",
                        "attributes": _attrs({"mcp_doctor.guard.check": f.check, "mcp_doctor.guard.message": f.message})}
                       for f in findings],
            "status": status,
        }
        if parent:
            span["parentSpanId"] = parent
        self.q.put(span)

    def _body(self, spans: list[dict]) -> bytes:
        return json.dumps({"resourceSpans": [{
            "resource": {"attributes": self.resource},
            "scopeSpans": [{"scope": {"name": "mcp-doctor.guard", "version": self.version}, "spans": spans}],
        }]}, ensure_ascii=False).encode()

    def _send(self, spans: list[dict]) -> None:
        body = self._body(spans)
        try:
            if self.http:
                import urllib.request

                req = urllib.request.Request(self.target, data=body, method="POST",
                                             headers={"Content-Type": "application/json", **self.headers})
                urllib.request.urlopen(req, timeout=self.timeout).close()
            else:
                with open(self.target, "ab") as f:
                    f.write(body + b"\n")
        except Exception as e:
            if not self.warned:  # once per run: a dead collector must not flood stderr
                self.warned = True
                print(f"[mcp-doctor guard] couldn't export spans to {self.target}: {getattr(e, 'code', None) or e!r}",
                      file=self.err, flush=True)

    def _run(self) -> None:
        while True:
            first = self.q.get()
            if first is None:
                return
            batch, stop = [first], False
            deadline = time.monotonic() + 0.5
            while len(batch) < 256:
                try:
                    nxt = self.q.get(timeout=max(0.0, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if nxt is None:
                    stop = True
                    break
                batch.append(nxt)
            self._send(batch)
            if stop:
                return

    def close(self, timeout: float = 5.0) -> None:
        self.q.put(None)
        self.worker.join(timeout=timeout)
