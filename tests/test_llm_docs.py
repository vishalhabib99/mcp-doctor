import ast
from pathlib import Path
from textwrap import dedent

import pytest

from mcp_doctor import llm_docs
from mcp_doctor.analyzer import analyze_repo
from mcp_doctor.cli import main
from mcp_doctor.fix import plan_fixes
from mcp_doctor.llm_docs import Drafter, LLMUnavailable, _clean, check_ollama, code_facts


def write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(dedent(content))
    return p


class FakeDrafter(Drafter):
    """Answers like the model would, without a server."""

    def __init__(self, answers):
        super().__init__(model="fake")
        self.answers = answers
        self.asked = []

    def _ask(self, source, arg_name, annotation):
        self.asked.append(arg_name)
        return self.answers.get(arg_name, {})


def _fn(src: str, name: str):
    tree = ast.parse(dedent(src))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    return fn, tree


def test_code_facts_literal_enum_and_default():
    fn, tree = _fn("""
        from enum import Enum
        from typing import Annotated, Literal, Optional

        class Mode(str, Enum):
            FAST = "fast"
            SLOW = "slow"

        def t(a: Literal["x", "y"] = "x", b: Optional[Mode] = None, c: Annotated[int, "n"] = 8, d: str = ""):
            pass
        """, "t")
    assert code_facts(fn, "a", tree) == "One of 'x', 'y'. Defaults to 'x'."
    assert code_facts(fn, "b", tree) == "One of 'fast', 'slow'."
    assert code_facts(fn, "c", tree) == "Defaults to 8."
    assert code_facts(fn, "d", tree) == ""


def test_enum_with_auto_values_states_nothing():
    fn, tree = _fn("""
        from enum import Enum, auto
        class Mode(Enum):
            A = auto()
        def t(m: Mode):
            pass
        """, "t")
    assert code_facts(fn, "m", tree) == ""


def test_clean_rejects_what_would_break_the_docstring():
    assert _clean('job_id: "the job to cancel"', "job_id") == "The job to cancel."
    assert _clean("Ends here \"\"\" oops", "x") is None
    assert _clean("Regex like \\d+", "x") is None
    assert _clean("TODO: fill in", "x") is None
    assert _clean("", "x") is None


SERVER = """
    from typing import Literal
    from mcp.server.fastmcp import FastMCP, Context
    mcp = FastMCP("x")

    @mcp.tool()
    def cancel_job(job_id: str, mode: Literal["soft", "hard"] = "soft", ctx: Context = None) -> str:
        \"\"\"Cancel a job.\"\"\"
        return "ok"
    """


def test_drafts_replace_the_todo_placeholders(tmp_path):
    write(tmp_path, "server.py", SERVER)
    drafter = FakeDrafter({
        "job_id": {"description": "ID of the job to cancel", "format_needed": True},
        "mode": {"description": "How to stop the job", "format_needed": False},
    })
    planned = plan_fixes(tmp_path, analyze_repo(tmp_path), only=frozenset({"args-stub"}), drafter=drafter)
    fixed = planned["server.py"][1]
    assert "job_id: ID of the job to cancel. TODO: state the expected format." in fixed
    assert "mode: How to stop the job. One of 'soft', 'hard'. Defaults to 'soft'." in fixed
    assert "ctx" not in drafter.asked  # injected Context: no model call
    assert drafter.drafted == 2 and drafter.format_todos == ["cancel_job.job_id"]
    ast.parse(fixed)


def test_unusable_draft_keeps_the_placeholder(tmp_path):
    write(tmp_path, "server.py", SERVER)
    drafter = FakeDrafter({"job_id": {"description": 'bad """ text'}})
    fixed = plan_fixes(tmp_path, analyze_repo(tmp_path), drafter=drafter)["server.py"][1]
    assert "job_id: TODO: describe this parameter." in fixed
    assert drafter.failed and drafter.failed[0].startswith("cancel_job.job_id")


def test_appends_drafts_to_a_partial_args_section(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def search(query: str, limit: int = 10) -> str:
            \"\"\"Search notes.

            Args:
                query: Words to look for.
            \"\"\"
            return ""
        """)
    drafter = FakeDrafter({"limit": {"description": "Most results to return"}})
    fixed = plan_fixes(tmp_path, analyze_repo(tmp_path), drafter=drafter)["server.py"][1]
    assert "        limit: Most results to return. Defaults to 10.\n" in fixed
    assert drafter.asked == ["limit"]


def test_no_model_call_when_the_docstring_cant_take_entries(tmp_path):
    write(tmp_path, "server.py", """
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("x")

        @mcp.tool()
        def search(query: str) -> str:
            return ""
        """)
    drafter = FakeDrafter({})
    plan_fixes(tmp_path, analyze_repo(tmp_path), drafter=drafter)
    assert drafter.asked == []


def test_check_ollama_explains_when_unreachable(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9")
    with pytest.raises(LLMUnavailable, match="ollama serve"):
        check_ollama("qwen3:8b")


def test_check_ollama_names_a_missing_model(monkeypatch):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"models": [{"name": "llama3:latest"}]}'

    monkeypatch.setattr(llm_docs.urllib.request, "urlopen", lambda *a, **k: Resp())
    with pytest.raises(LLMUnavailable, match="ollama pull qwen3:8b"):
        check_ollama("qwen3:8b")
    check_ollama("llama3")  # ":latest" is implied


def test_cli_llm_needs_fix_or_diff(tmp_path, capsys):
    assert main([str(tmp_path), "--llm"]) == 2
    assert "--llm needs --fix or --diff" in capsys.readouterr().err


def test_cli_llm_needs_args_stub_kind(tmp_path, capsys):
    assert main([str(tmp_path), "--diff", "--llm", "--fix-only", "none-default"]) == 2
    assert "add args-stub" in capsys.readouterr().err


def test_cli_llm_reports_unreachable_ollama(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9")
    assert main([str(tmp_path), "--diff", "--llm"]) == 2
    assert "ollama serve" in capsys.readouterr().err
