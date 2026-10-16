"""A tamper-evident audit log of every call GuardedSession sees, one JSON object per line.

The trilogy used to decline "logging completeness" because there's no spec-mandated log format
to grade a server against. This sidesteps that: instead of grading the server's logs, the
wrapper writes its own, since it already sees every call, its decision and the reasons.

Each record carries the SHA-256 of the previous record ("prev") and of itself ("hash"), so
editing, deleting or reordering any line breaks the chain from that point on, and
verify_audit_log says exactly where. That's tamper-evident, not tamper-proof: someone who can
rewrite the whole file can rebuild the chain. Anchor the last hash somewhere else (a CI
artifact, a ticket) if that matters.

Argument values are NOT logged by default, because they can hold the very PII this project
checks responses for. Records keep the argument names plus a SHA-256 of the canonical
arguments, which is enough to prove two calls had identical input. Pass log_arguments=True
to log values too.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _record_hash(record: dict) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(_canonical(body).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path, log_arguments: bool = False):
        self.path = Path(path)
        self.log_arguments = log_arguments
        self._prev = self._last_hash()

    def _last_hash(self) -> str:
        if not self.path.exists():
            return GENESIS
        last = None
        with self.path.open() as f:
            for line in f:
                if line.strip():
                    last = line
        return json.loads(last)["hash"] if last else GENESIS

    def write(self, *, tool: str, arguments: dict, called: bool, outcome: str, decision: str,
              confidence: str, reasons: list[str], duration_ms: float = 0.0) -> dict:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "tool": tool,
            "argument_names": sorted(arguments),
            "arguments_sha256": hashlib.sha256(_canonical(arguments).encode()).hexdigest(),
            "called": called,
            "outcome": outcome,
            "decision": decision,
            "confidence": confidence,
            "reasons": reasons,
            "duration_ms": round(duration_ms, 1),
            "prev": self._prev,
        }
        if self.log_arguments:
            record["arguments"] = arguments
        record["hash"] = _record_hash(record)
        with self.path.open("a") as f:
            f.write(_canonical(record) + "\n")
        self._prev = record["hash"]
        return record


def verify_audit_log(path: str | Path) -> tuple[bool, str]:
    """(True, summary) if every record's hash and chain link check out, else (False, where it breaks)."""
    prev = GENESIS
    n = 0
    with Path(path).open() as f:
        for lineno, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                return False, f"line {lineno}: not valid JSON"
            if record.get("prev") != prev:
                return False, f"line {lineno}: chain broken (a record before it was edited, removed or reordered)"
            if record.get("hash") != _record_hash(record):
                return False, f"line {lineno}: contents don't match its hash (this record was edited)"
            prev = record["hash"]
            n += 1
    return True, f"{n} records, chain intact, last hash {prev[:16]}…"


def summarize_audit_log(path: str | Path) -> dict:
    """Counts over the whole log: the light version of monitoring, a report and not a service."""
    decisions: Counter = Counter()
    by_tool: dict[str, Counter] = {}
    reasons: Counter = Counter()
    total = 0
    with Path(path).open() as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            total += 1
            decisions[r["decision"]] += 1
            by_tool.setdefault(r["tool"], Counter())[r["decision"]] += 1
            for reason in r["reasons"]:
                reasons[reason.split(":")[0]] += 1
    return {
        "calls": total,
        "decisions": dict(decisions),
        "by_tool": {t: dict(c) for t, c in sorted(by_tool.items())},
        "top_reasons": reasons.most_common(10),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="mcp-trust-check-audit", description="Verify or summarize a GuardedSession audit log.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("verify", help="check the hash chain").add_argument("path")
    sub.add_parser("summary", help="decision counts per tool").add_argument("path")
    args = parser.parse_args(argv)
    if args.cmd == "verify":
        ok, msg = verify_audit_log(args.path)
        print(("OK: " if ok else "TAMPERED: ") + msg)
        return 0 if ok else 1
    print(json.dumps(summarize_audit_log(args.path), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
