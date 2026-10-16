"""Policy gate and audit log: pure tests, plus the fixture server to prove a denied call never
reaches the server and a leaked card is escalated."""

import asyncio
import json

import pytest

from mcp_doctor.session import AuditLog, Policy, PolicyError, verify_audit_log
from mcp_doctor.session.session import ACT, BLOCK, ESCALATE

from .test_session import _guarded_session


# --- policy validation: typos must fail loudly ---------------------------------

@pytest.mark.parametrize("bad", [
    {"deny_tool": ["x"]},                                   # misspelled key
    {"deny_tools": "x"},                                    # not a list
    {"require_approval_for_destructive": "yes"},            # not a bool
    {"arguments": {"t": {"a": {"regex": ".*"}}}},           # unknown rule key
    {"arguments": {"t": {"a": {"pattern": "("}}}},          # invalid regex
    {"pii": ["cards"]},                                     # unknown detector
])
def test_invalid_policies_are_rejected(bad):
    with pytest.raises(PolicyError):
        Policy.from_dict(bad)


def test_policy_decisions():
    p = Policy.from_dict({
        "allow_tools": ["a", "b", "d"],
        "deny_tools": ["d"],
        "require_approval": ["b"],
        "arguments": {"a": {"mode": {"enum": ["safe"]}, "q": {"pattern": "[a-z]{1,5}"}}},
    })
    assert p.check("a", {"mode": "safe", "q": "abc"}, read_only=True, approved=False) == (None, [])
    assert p.check("c", {}, read_only=True, approved=False)[0] == BLOCK          # not allowed
    assert p.check("d", {}, read_only=True, approved=False)[0] == BLOCK          # deny beats allow
    assert p.check("a", {"mode": "unsafe"}, read_only=True, approved=False)[0] == BLOCK
    assert p.check("a", {"q": "abc; rm -rf"}, read_only=True, approved=False)[0] == BLOCK  # fullmatch, not search
    assert p.check("b", {}, read_only=True, approved=False)[0] == ESCALATE
    assert p.check("b", {}, read_only=True, approved=True) == (None, [])


def test_destructive_needs_approval_only_when_enabled():
    assert Policy.from_dict({}).check("x", {}, read_only=False, approved=False) == (None, [])
    p = Policy.from_dict({"require_approval_for_destructive": True})
    assert p.check("x", {}, read_only=False, approved=False)[0] == ESCALATE
    assert p.check("x", {}, read_only=True, approved=False) == (None, [])


# --- audit log -----------------------------------------------------------------

def _write(log, n):
    for i in range(n):
        log.write(tool=f"t{i}", arguments={"card": "4111111111111111"}, called=True, outcome="ok",
                  decision=ACT, confidence="HIGH", reasons=[])


def test_audit_chain_verifies_and_survives_reopening(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write(AuditLog(path), 2)
    _write(AuditLog(path), 2)  # a new AuditLog continues the chain from the file
    ok, msg = verify_audit_log(path)
    assert ok and msg.startswith("4 records")


def test_audit_does_not_log_argument_values_by_default(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write(AuditLog(path), 1)
    record = json.loads(path.read_text())
    assert "4111111111111111" not in path.read_text()
    assert record["argument_names"] == ["card"] and len(record["arguments_sha256"]) == 64


@pytest.mark.parametrize("tamper", ["edit", "delete", "reorder"])
def test_audit_detects_tampering(tmp_path, tamper):
    path = tmp_path / "audit.jsonl"
    _write(AuditLog(path), 3)
    lines = path.read_text().splitlines()
    if tamper == "edit":
        lines[1] = lines[1].replace('"decision":"ACT"', '"decision":"BLOCK"')
    elif tamper == "delete":
        del lines[1]
    else:
        lines[0], lines[1] = lines[1], lines[0]
    path.write_text("\n".join(lines) + "\n")
    ok, msg = verify_audit_log(path)
    assert not ok and "line" in msg


# --- end to end on the fixture server ------------------------------------------

def test_denied_call_never_reaches_the_server(tmp_path):
    async def run():
        gs, stack = await _guarded_session()
        gs.policy = Policy.from_dict({"deny_tools": ["deletes_record"]})
        gs.audit_log = AuditLog(tmp_path / "audit.jsonl")
        real_calls = []
        original = gs._session.call_tool

        async def counting(name, args, *a, **kw):
            real_calls.append(name)
            return await original(name, args, *a, **kw)

        gs._session.call_tool = counting
        async with stack:
            await gs.list_tools()
            r = await gs.call_tool("deletes_record", {"record_id": "42"})
            return r, real_calls
    r, real_calls = asyncio.run(run())
    assert (r.decision, r.called, real_calls) == (BLOCK, False, [])
    assert verify_audit_log(tmp_path / "audit.jsonl")[0]


def test_destructive_tool_escalates_until_approved():
    async def run():
        gs, stack = await _guarded_session()
        gs.policy = Policy.from_dict({"require_approval_for_destructive": True})
        async with stack:
            await gs.list_tools()
            first = await gs.call_tool("deletes_record", {"record_id": "42"})
            again = await gs.call_tool("deletes_record", {"record_id": "42"}, approved=True)
            readonly = await gs.call_tool("well_behaved", {"city": "Paris"})
            return first, again, readonly
    first, again, readonly = asyncio.run(run())
    assert (first.decision, first.called) == (ESCALATE, False)
    assert again.called and again.decision == ACT
    assert readonly.called and readonly.decision == ACT


def test_leaked_card_is_escalated_and_masked(tmp_path):
    async def run():
        gs, stack = await _guarded_session()
        gs.audit_log = AuditLog(tmp_path / "audit.jsonl")
        async with stack:
            await gs.list_tools()
            return await gs.call_tool("returns_card", {"customer": "Jane"})
    r = asyncio.run(run())
    assert r.decision == ESCALATE
    assert r.pii_findings == ["card ************1111"]
    log = (tmp_path / "audit.jsonl").read_text()
    assert "4111 1111" not in log and "************1111" in log
