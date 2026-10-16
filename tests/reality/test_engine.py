"""End-to-end tests: these actually launch the real fixture MCP server as a
subprocess and talk to it over real stdio — not mocked. Same discipline as
mcp-doctor/mcp-fuzz: verifies real runtime classification, not just the
input-generation/checks logic in isolation (see test_checks.py, test_
generator.py for that)."""

import sys
from pathlib import Path

import pytest

from mcp_doctor.reality.engine import run_reality_check
from mcp_doctor.reality.report import build_report

FIXTURE_SERVER = str(Path(__file__).parent / "fixtures" / "fixture_server.py")
TIMEOUT = 3.0


@pytest.fixture(scope="module")
def raw_report():
    import asyncio

    return asyncio.run(run_reality_check(sys.executable, [FIXTURE_SERVER], timeout=TIMEOUT))


def _result(raw_report, name):
    return next(r for r in raw_report.results if r.tool_name == name)


def test_connects_and_lists_all_eight_tools(raw_report):
    assert raw_report.connect_error is None
    names = {r.tool_name for r in raw_report.results}
    assert names == {
        "well_behaved", "secretly_refuses", "returns_empty",
        "genuinely_fails", "no_string_args", "delete_everything",
        "kills_process", "hangs_forever",
    }


def test_non_read_only_tool_is_skipped_by_default(raw_report):
    r = _result(raw_report, "delete_everything")
    assert r.tested is False
    assert "readOnlyHint" in r.skip_reason


def test_well_behaved_tool_is_not_flagged(raw_report):
    r = _result(raw_report, "well_behaved")
    assert r.tested is True
    assert r.refusal_in_disguise is None
    assert r.empty_content is False
    assert r.schema_violation is None


def test_well_behaved_tool_echoes_its_input(raw_report):
    r = _result(raw_report, "well_behaved")
    # generate_realistic_arguments should have supplied a real-looking city
    # (e.g. "Paris"), and the fixture's response includes it verbatim.
    assert r.echo_mismatch_inputs == []


def test_disguised_refusal_is_caught(raw_report):
    r = _result(raw_report, "secretly_refuses")
    assert r.tested is True
    assert r.refusal_in_disguise is not None
    assert "don't have access" in r.refusal_in_disguise or "i'm sorry, but" in r.refusal_in_disguise.lower()


def test_empty_content_on_success_is_caught(raw_report):
    r = _result(raw_report, "returns_empty")
    assert r.tested is True
    assert r.empty_content is True


def test_honest_failure_is_not_flagged_as_a_disguised_refusal(raw_report):
    r = _result(raw_report, "genuinely_fails")
    assert r.tested is True
    # The SDK catches the raised ValueError and returns isError=true —
    # already honest about failing, so none of the content checks should
    # even run (skip_reason explains why, not a refusal-in-disguise flag).
    assert r.refusal_in_disguise is None
    assert r.empty_content is False
    assert r.skip_reason is not None and "isError=true" in r.skip_reason


def test_tool_with_no_string_args_has_no_echo_check(raw_report):
    r = _result(raw_report, "no_string_args")
    assert r.tested is True
    assert r.echo_mismatch_inputs is None


def test_report_scores_only_the_checkable_tools(raw_report):
    report = build_report(raw_report)
    # 8 tools total: 1 skipped (not read-only), 3 tested-but-not-checkable
    # (genuinely_fails' honest isError=true, plus kills_process/hangs_forever
    # failing before a response was ever available — gate.py's job, not this
    # batch-audit engine's, to classify those two further), leaving 4
    # checkable — well_behaved, secretly_refuses, returns_empty, no_string_args.
    assert report.tested_count == 7
    assert report.skipped_count == 1
    assert report.checkable_count == 4
    assert report.flagged_count == 2  # secretly_refuses + returns_empty
    assert report.sanity_percent == 50.0
    assert report.grade == "D"


def test_stdio_recovers_from_kills_process_without_terminating_early(raw_report):
    # A stdio target's crashed subprocess is almost always relaunchable —
    # contrast with test_http_target_reports_terminated_early_after_
    # unrecoverable_crash below, where the same tool's crash *is*
    # unrecoverable over HTTP (mirrors mcp-fuzz's identical pair of tests).
    assert raw_report.terminated_early is None


# --- HttpTarget: the same fixture server, over Streamable HTTP instead of
# stdio — see mcp-fuzz's identical tests for the full rationale. Launches
# the fixture as its own subprocess and owns that subprocess's lifecycle.
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def http_raw_report():
    import asyncio

    from mcp_doctor.reality.engine import run_reality_check as _run_reality_check

    port = _free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    env = {**os.environ, "FIXTURE_TRANSPORT": "streamable-http", "FIXTURE_PORT": str(port)}
    proc = subprocess.Popen([sys.executable, FIXTURE_SERVER], env=env)
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(url, timeout=0.5)
            except urllib.error.HTTPError:
                break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.1)
                continue
            else:
                break
        report = asyncio.run(_run_reality_check(url=url, timeout=TIMEOUT))
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    return report


def test_http_target_connects_and_lists_tools(http_raw_report):
    assert http_raw_report.connect_error is None
    names = {r.tool_name for r in http_raw_report.results}
    assert "well_behaved" in names
    assert "secretly_refuses" in names


def test_http_target_classifies_real_behavior_same_as_stdio(http_raw_report):
    well_behaved = _result(http_raw_report, "well_behaved")
    assert well_behaved.tested is True
    assert well_behaved.refusal_in_disguise is None

    refuses = _result(http_raw_report, "secretly_refuses")
    assert refuses.refusal_in_disguise is not None


def test_http_target_reports_terminated_early_after_unrecoverable_crash(http_raw_report):
    # kills_process calls os._exit() — fatal over HTTP specifically, since
    # this tool doesn't own the remote server process and has no way to
    # relaunch it.
    assert http_raw_report.terminated_early is not None
    assert "kills_process" in http_raw_report.terminated_early
    skipped_after = [
        r for r in http_raw_report.results
        if not r.tested and r.skip_reason and "unreachable" in r.skip_reason
    ]
    assert len(skipped_after) > 0


def test_url_and_command_both_given_raises():
    import asyncio

    from mcp_doctor.reality.engine import run_reality_check as _run_reality_check

    with pytest.raises(ValueError):
        asyncio.run(_run_reality_check(command=sys.executable, url="http://127.0.0.1:1/mcp"))


def test_neither_url_nor_command_given_raises():
    import asyncio

    from mcp_doctor.reality.engine import run_reality_check as _run_reality_check

    with pytest.raises(ValueError):
        asyncio.run(_run_reality_check())


ENV_REQUIRED_SERVER = str(Path(__file__).parent / "fixtures" / "env_required_server.py")


def test_env_kwarg_is_passed_through_to_the_target_server():
    # Found via real dogfooding, not speculatively: sooperset/mcp-atlassian
    # (5.9k stars) registers zero tools without JIRA_*/CONFLUENCE_* config,
    # and this CLI had no way to supply it at all until now — mcp-fuzz
    # already solved the identical problem for brave-search-mcp-server.
    import asyncio

    report = asyncio.run(run_reality_check(
        sys.executable, [ENV_REQUIRED_SERVER],
        env={"REQUIRED_TEST_KEY": "expected-value"}, timeout=TIMEOUT,
    ))
    assert report.connect_error is None


def test_no_env_kwarg_does_not_leak_or_guess_the_required_value():
    # Without an explicit env, the server must NOT start — confirms this
    # isn't accidentally inheriting the operator's full shell environment
    # (a real secret-leaking regression), only the SDK's own minimal safe
    # default (PATH, HOME, ...).
    import asyncio

    report = asyncio.run(run_reality_check(sys.executable, [ENV_REQUIRED_SERVER], timeout=TIMEOUT))
    assert report.connect_error is not None


def test_env_kwarg_merges_onto_safe_defaults_rather_than_replacing_them():
    # A caller passing one custom var must not lose PATH/HOME in the
    # process — verified against a real failure mode: passing only an API
    # key with no PATH would break the interpreter/npx itself before the
    # target server ever runs.
    from mcp.client.stdio import get_default_environment

    from mcp_doctor.reality.engine import _merged_env

    merged = _merged_env({"SOME_API_KEY": "x"})
    assert merged["SOME_API_KEY"] == "x"
    for key in get_default_environment():
        assert key in merged


def test_no_env_is_passed_through_unchanged():
    from mcp_doctor.reality.engine import _merged_env

    assert _merged_env(None) is None
    assert _merged_env({}) == {}
