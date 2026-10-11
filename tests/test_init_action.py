"""Tests for `mcp-doctor init-action`: the workflow lands in the git repo's
root, points at the server's subdirectory, and never clobbers an existing
file without --force."""

from mcp_doctor.cli import main

SERVER = '''from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers.

    Args:
        a: First number.
        b: Second number.
    """
    return a + b
'''


def _repo(tmp_path, server_dir="."):
    (tmp_path / ".git").mkdir()
    d = tmp_path / server_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / "server.py").write_text(SERVER)
    return d


def test_writes_workflow_at_repo_root(tmp_path, capsys):
    _repo(tmp_path)
    assert main(["init-action", str(tmp_path)]) == 0
    wf = (tmp_path / ".github/workflows/mcp-doctor.yml").read_text()
    assert "uses: vishalhabib99/mcp-doctor@v1" in wf
    assert "path: .\n" in wf
    assert "fail-under: 0 " in wf
    assert "pull-requests: write" in wf
    assert "1 tool(s)" in capsys.readouterr().out


def test_subdirectory_server_gets_relative_path(tmp_path):
    server = _repo(tmp_path, "servers/demo")
    assert main(["init-action", str(server), "--fail-under", "70"]) == 0
    assert not (server / ".github").exists()
    wf = (tmp_path / ".github/workflows/mcp-doctor.yml").read_text()
    assert "path: servers/demo\n" in wf
    assert "fail-under: 70 " in wf


def test_refuses_to_overwrite_without_force(tmp_path):
    _repo(tmp_path)
    out = tmp_path / ".github/workflows/mcp-doctor.yml"
    out.parent.mkdir(parents=True)
    out.write_text("mine\n")
    assert main(["init-action", str(tmp_path)]) == 1
    assert out.read_text() == "mine\n"
    assert main(["init-action", str(tmp_path), "--force"]) == 0
    assert out.read_text() != "mine\n"


def test_print_writes_nothing(tmp_path, capsys):
    _repo(tmp_path)
    assert main(["init-action", str(tmp_path), "--print"]) == 0
    assert "vishalhabib99/mcp-doctor@v1" in capsys.readouterr().out
    assert not (tmp_path / ".github").exists()


def test_zero_tools_explains_not_graded(tmp_path, capsys):
    (tmp_path / ".git").mkdir()
    assert main(["init-action", str(tmp_path)]) == 0
    assert "Not graded" in capsys.readouterr().out
