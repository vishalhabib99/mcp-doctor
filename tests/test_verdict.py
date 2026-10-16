"""Unit tests for the release decision: pure function over the three tools' --json report shapes."""

import json
import sys
from pathlib import Path

from mcp_doctor.verdict import BLOCK, FIX_FIRST, HIGH, LOW, MEDIUM, SHIP, decide


def _doctor(*issues, repo_issues=()):
    return {
        "percent": 95.0,
        "grade": "A",
        "tools": [{"name": "t", "issues": list(issues)}],
        "repo_issues": list(repo_issues),
    }


def _issue(check, severity, category="quality"):
    return {"tool": "t", "file": "s.py", "line": 1, "check": check, "message": "", "severity": severity, "category": category}


def _fuzz(**overrides):
    base = {
        "connect_error": None,
        "terminated_early": None,
        "tested_count": 10,
        "skipped_count": 0,
        "crash_count": 0,
        "timeout_count": 0,
        "crash_resilience_percent": 100.0,
        "grade": "A",
        "concurrency": {"flagged_tools": []},
        "sequence": {"stale_after_delete_count": 0},
        "cross_resource": {"orphaned_child_count": 0},
    }
    base.update(overrides)
    return base


def _reality(*tools, **overrides):
    base = {"connect_error": None, "checkable_count": 1, "sanity_percent": 100.0, "grade": "A", "tools": list(tools)}
    base.update(overrides)
    return base


def _tool(name, **flags):
    t = {"name": name, "refusal_in_disguise": False, "empty_content": False, "schema_violation": False, "echo_mismatch_inputs": []}
    t.update(flags)
    return t


def test_clean_full_run_ships():
    d = decide(_doctor(), _fuzz(), _reality(_tool("a")))
    assert d.verdict == SHIP
    assert d.blockers == [] and d.fixes == [] and d.notes == []


def test_one_crash_blocks_even_when_the_average_is_high():
    # The whole point: 95% doctor, 99% fuzz, 100% reality averages to an A, but a crash still blocks.
    d = decide(_doctor(), _fuzz(crash_count=1, crash_resilience_percent=99.0), _reality(_tool("a")))
    assert d.verdict == BLOCK
    assert any("1 crash " in b for b in d.blockers)


def test_disguised_refusal_blocks_and_names_the_tool():
    d = decide(_doctor(), _fuzz(), _reality(_tool("search", refusal_in_disguise=True)))
    assert d.verdict == BLOCK
    assert "search" in d.blockers[0]


def test_security_error_blocks_but_security_warning_only_needs_a_fix():
    assert decide(_doctor(_issue("shell_injection", "error", "security")), None, None).verdict == BLOCK
    assert decide(_doctor(_issue("broad_fs", "warning", "security")), None, None).verdict == FIX_FIRST


def test_quality_warning_alone_still_ships():
    assert decide(_doctor(_issue("vague_description", "warning")), _fuzz(), _reality()).verdict == SHIP


def test_repo_level_issues_count_too():
    d = decide(_doctor(repo_issues=[_issue("secret_in_repo", "error", "security")]), None, None)
    assert d.verdict == BLOCK


def test_fix_level_runtime_findings():
    cases = [
        _fuzz(timeout_count=2),
        _fuzz(concurrency={"flagged_tools": [{"name": "x"}]}),
        _fuzz(sequence={"stale_after_delete_count": 1}),
        _fuzz(cross_resource={"orphaned_child_count": 1}),
    ]
    for fuzz in cases:
        assert decide(_doctor(), fuzz, _reality()).verdict == FIX_FIRST, fuzz
    for flag in ({"empty_content": True}, {"schema_violation": True}):
        assert decide(_doctor(), _fuzz(), _reality(_tool("a", **flag))).verdict == FIX_FIRST, flag


def test_blockers_outrank_fixes_and_all_reasons_are_kept():
    d = decide(_doctor(), _fuzz(crash_count=1, timeout_count=1), _reality())
    assert d.verdict == BLOCK
    assert len(d.blockers) == 1 and len(d.fixes) == 1


def test_connect_error_blocks():
    assert decide(_doctor(), _fuzz(connect_error="boom"), _reality(connect_error="boom")).verdict == BLOCK


def test_static_only_run_says_so():
    d = decide(_doctor(), None, None)
    assert d.verdict == SHIP
    assert d.notes and "static analysis only" in d.notes[0]


def test_verdict_cli_static_only_on_example_servers(capsys):
    from mcp_doctor.cli import main

    root = Path(__file__).resolve().parent.parent / "examples"
    assert main(["verdict", str(root / "good_server")]) == 0
    out = capsys.readouterr().out
    assert out.startswith("mcp-doctor verdict: ")
    assert "Static scan only." in out
    assert main(["verdict", str(root / "bad_server"), "--json", "--fail-on", "FIX-FIRST"]) in (0, 1)
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] in (SHIP, FIX_FIRST, BLOCK)
    assert payload["scores"]["static"] is not None
    assert "fuzz" not in payload["scores"]


def test_fuzz_without_mcp_sdk_prints_install_hint(monkeypatch, capsys):
    from mcp_doctor.cli import main

    monkeypatch.setitem(sys.modules, "mcp", None)  # makes `import mcp` raise ImportError
    assert main(["fuzz", "--", "python", "server.py"]) == 2
    assert "mcp-server-lint[runtime]" in capsys.readouterr().err
