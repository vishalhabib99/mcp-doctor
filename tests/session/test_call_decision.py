"""Per-call ACT / ESCALATE / BLOCK decision: pure-function unit tests, plus the same fixture
server test_session.py uses, so the wiring is checked against real calls too."""

import asyncio

from mcp_doctor.session import ACT, BLOCK, ESCALATE, GuardedCallResult, decide_call
from mcp_doctor.session.session import HIGH, LOW, MEDIUM

from .test_session import _guarded_session


def _ok(**kw):
    return GuardedCallResult("t", "ok", **kw)


def test_clean_call_with_history_is_a_confident_act():
    assert decide_call(_ok(), prior_calls=5, known_tool=True, registration_flagged=False)[:2] == (ACT, HIGH)


def test_crash_timeout_and_disguised_refusal_block():
    for r in (GuardedCallResult("t", "crash", detail="x"), GuardedCallResult("t", "timeout", detail="x"),
              _ok(refusal_in_disguise="I can't help with that")):
        d, c, reasons = decide_call(r, prior_calls=0, known_tool=True, registration_flagged=False)
        assert (d, c) == (BLOCK, HIGH) and reasons


def test_empty_schema_slow_and_large_escalate():
    for r in (_ok(empty_content=True), _ok(schema_violation="missing field"),
              _ok(slow_reasons=["5000ms"]), _ok(bloated_reasons=["30000 chars"])):
        assert decide_call(r, prior_calls=5, known_tool=True, registration_flagged=False)[0] == ESCALATE


def test_honest_error_is_act():
    d, c, reasons = decide_call(_ok(is_error=True), prior_calls=5, known_tool=True, registration_flagged=False)
    assert (d, c) == (ACT, HIGH) and any("isError" in r for r in reasons)


def test_no_baseline_yet_is_medium():
    d, c, reasons = decide_call(_ok(), prior_calls=0, known_tool=True, registration_flagged=False)
    assert (d, c) == (ACT, MEDIUM) and any("baseline" in r for r in reasons)


def test_unknown_tool_is_low_and_registration_flag_is_medium():
    assert decide_call(_ok(), prior_calls=5, known_tool=False, registration_flagged=False)[1] == LOW
    assert decide_call(_ok(), prior_calls=5, known_tool=True, registration_flagged=True)[1] == MEDIUM


def test_should_act_only_on_confident_act():
    r = _ok()
    r.decision, r.confidence = ACT, HIGH
    assert r.should_act
    r.confidence = MEDIUM
    assert r.should_act  # no speed baseline yet shouldn't escalate every early call
    r.confidence = LOW
    assert not r.should_act
    r.decision, r.confidence = ESCALATE, HIGH
    assert not r.should_act


def test_live_session_confidence_grows_with_history():
    async def run():
        gs, stack = await _guarded_session()
        async with stack:
            await gs.list_tools()
            results = [await gs.call_tool("well_behaved", {"city": "Paris"}) for _ in range(4)]
            return results
    results = asyncio.run(run())
    assert all(r.decision == ACT for r in results)
    assert [r.confidence for r in results] == [MEDIUM, MEDIUM, MEDIUM, HIGH]
    assert all(r.should_act for r in results)


def test_live_session_blocks_and_escalates_real_failures():
    async def run():
        gs, stack = await _guarded_session()
        async with stack:
            await gs.list_tools()
            refused = await gs.call_tool("secretly_refuses", {"city": "Paris"})
            empty = await gs.call_tool("returns_empty", {"city": "Paris"})
            crashed = await gs.call_tool("kills_process", {"x": "anything"})
            return refused, empty, crashed
    refused, empty, crashed = asyncio.run(run())
    assert refused.decision == BLOCK
    assert empty.decision == ESCALATE
    assert (crashed.decision, crashed.confidence) == (BLOCK, HIGH)


def test_calling_before_list_tools_is_low_confidence():
    async def run():
        gs, stack = await _guarded_session()
        async with stack:
            return await gs.call_tool("well_behaved", {"city": "Paris"})
    r = asyncio.run(run())
    assert (r.decision, r.confidence) == (ACT, LOW) and not r.should_act


def test_act_threshold_is_configurable_and_validated():
    import pytest
    from mcp_doctor.session import GuardedSession
    r = _ok()
    r.decision = ACT
    for confidence, threshold, expected in [
        (MEDIUM, MEDIUM, True), (MEDIUM, HIGH, False), (HIGH, HIGH, True),
        (LOW, MEDIUM, False), (LOW, LOW, True),
    ]:
        r.confidence, r.act_threshold = confidence, threshold
        assert r.should_act is expected, (confidence, threshold)
    with pytest.raises(ValueError):
        GuardedSession(session=None, act_threshold="medium-ish")


def test_strict_threshold_escalates_early_calls_live():
    async def run():
        gs, stack = await _guarded_session()
        gs.act_threshold = HIGH
        async with stack:
            await gs.list_tools()
            return [await gs.call_tool("well_behaved", {"city": "Paris"}) for _ in range(4)]
    results = asyncio.run(run())
    assert [r.should_act for r in results] == [False, False, False, True]


def test_look_alike_tools_are_flagged_and_capped_at_medium():
    async def run():
        gs, stack = await _guarded_session()
        async with stack:
            await gs.list_tools()
            results = [await gs.call_tool("lookup_order", {"order_id": "7"}) for _ in range(4)]
            clean = [await gs.call_tool("well_behaved", {"city": "Paris"}) for _ in range(4)]
            return gs, results, clean
    gs, results, clean = asyncio.run(run())
    assert gs.registration_results["lookup_order"].flagged and gs.registration_results["cancel_order"].flagged
    assert results[-1].confidence == MEDIUM and any("look-alike" in r for r in results[-1].reasons)
    assert clean[-1].confidence == HIGH
