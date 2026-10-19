"""The in-browser check (leaderboard/check/) downloads only the files
fetch_rules.json names and creates every other file empty. These tests hold
that view of a repo to the same report the CLI gives on the full checkout.
Checked by hand on 44 real MCP repos (2026-10-15, 44/44 identical in Pyodide)."""
import json
import shutil
from pathlib import Path

import pytest

from mcp_doctor.analyzer import analyze_repo
from mcp_doctor.report import render_json

ROOT = Path(__file__).resolve().parents[1]
RULES = json.loads((ROOT / "leaderboard" / "check" / "fetch_rules.json").read_text())
JS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"}


def wanted(rel: Path, size: int) -> bool | None:
    """Python twin of wanted() in leaderboard/check/worker.js."""
    if set(rel.parts[:-1]) & set(RULES["skip_dirs"]):
        return None
    if size > RULES["max_file_bytes"]:
        return False
    if rel.suffix in JS and any(p in RULES["skip_js_build_dirs"] or p.startswith(".") for p in rel.parts[:-1]):
        return False
    name = rel.name
    return (rel.suffix in RULES["content_suffixes"] or name in RULES["content_names"]
            or any(name.startswith(p) for p in RULES["content_name_prefixes"]))


def browser_view(src: Path, dst: Path) -> None:
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        w = wanted(rel, p.stat().st_size)
        if w is None:
            continue
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        if w:
            shutil.copyfile(p, dst / rel)
        else:
            (dst / rel).write_bytes(b"")


def summary(root: Path) -> tuple:
    d = json.loads(render_json(analyze_repo(root)))
    return d["grade"], d["percent"], d["security_grade"], d["security_percent"], len(d["tools"])


@pytest.mark.parametrize("example", ["good_server", "bad_server", "good_server_ts", "bad_server_ts", "good_server_rs", "bad_server_rs"])
def test_browser_view_grades_like_full_checkout(example, tmp_path):
    # Each example is a subfolder of this repo, so the page checks it the way
    # worker.js lays out a subfolder: under a .git marker, with the LICENSE
    # files of its parent folders created empty.
    src = ROOT / "examples" / example
    rel = src.relative_to(ROOT)
    (tmp_path / ".git").mkdir()
    for parent in (ROOT, *[ROOT / Path(*rel.parts[:i]) for i in range(1, len(rel.parts))]):
        for lic in parent.glob("LICENSE*"):
            (tmp_path / lic.relative_to(ROOT)).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / lic.relative_to(ROOT)).write_bytes(b"")
    browser_view(src, tmp_path / rel)
    assert summary(tmp_path / rel) == summary(src)


def test_unread_files_still_exist_for_name_checks(tmp_path):
    src = tmp_path / "src"
    (src / "tests").mkdir(parents=True)
    (src / "LICENSE").write_text("MIT")
    (src / "tests" / "test_x.py").write_text("def test_x(): pass\n")
    (src / "node_modules" / "x").mkdir(parents=True)
    (src / "node_modules" / "x" / "index.js").write_text("eval(x)")
    dst = tmp_path / "dst"
    browser_view(src, dst)
    assert (dst / "LICENSE").exists() and (dst / "LICENSE").read_text() == ""
    assert (dst / "tests" / "test_x.py").read_text() == "def test_x(): pass\n"
    assert not (dst / "node_modules").exists()


def test_rules_match_worker_js():
    """worker.js reads the same JSON; make sure it still does."""
    worker = (ROOT / "leaderboard" / "check" / "worker.js").read_text()
    assert 'fetch("fetch_rules.json")' in worker
    for key in ("skip_dirs", "skip_js_build_dirs", "content_suffixes", "content_names", "content_name_prefixes", "max_file_bytes"):
        assert f"rules.{key}" in worker, key
