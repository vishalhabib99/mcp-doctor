"""The plugin folders load in Claude Code, Codex and Cursor: each manifest parses,
names what's really there, and the hook files speak each agent's format."""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = {"mcp-doctor": ROOT / "plugin", "mcp-doctor-guard": ROOT / "plugin-guard"}


def load(path):
    return json.loads(path.read_text())


@pytest.mark.parametrize("agent", [".claude-plugin", ".codex-plugin", ".cursor-plugin"])
@pytest.mark.parametrize("name", PLUGINS)
def test_every_agent_has_a_manifest_with_the_same_name_and_version(agent, name):
    manifest = load(PLUGINS[name] / agent / "plugin.json")
    assert manifest["name"] == name
    claude = load(PLUGINS[name] / ".claude-plugin" / "plugin.json")
    assert manifest["version"] == claude["version"]
    for key in ("hooks", "skills", "logo"):
        if isinstance(manifest.get(key), str):
            assert (PLUGINS[name] / manifest[key]).exists(), f"{agent} {key} -> {manifest[key]}"
    for key in ("logo", "composerIcon"):
        if key in manifest.get("interface", {}):
            assert (PLUGINS[name] / manifest["interface"][key]).exists()


def test_skill_names_match_their_folders():
    """Cursor requires it; Codex and Claude Code key on the name too."""
    for skill in (ROOT / "plugin" / "skills").iterdir():
        front = (skill / "SKILL.md").read_text().split("---")[1]
        assert re.search(rf"^name: {re.escape(skill.name)}$", front, re.M)
        assert re.search(r"^description: \S", front, re.M)


def test_marketplaces_list_both_plugins_at_real_paths():
    claude = load(ROOT / ".claude-plugin" / "marketplace.json")
    codex = load(ROOT / ".agents" / "plugins" / "marketplace.json")
    cursor = load(ROOT / ".cursor-plugin" / "marketplace.json")
    for market, path_of in ((claude, lambda p: p["source"]), (codex, lambda p: p["source"]["path"]), (cursor, lambda p: p["source"])):
        assert {p["name"] for p in market["plugins"]} == set(PLUGINS)
        for p in market["plugins"]:
            assert (ROOT / path_of(p)).resolve() == PLUGINS[p["name"]].resolve()
    for p in codex["plugins"]:
        assert p["policy"]["installation"] == "AVAILABLE" and p["category"]
    assert cursor["owner"]["name"]


def _commands(hooks):
    for groups in hooks["hooks"].values():
        for g in groups:
            for h in g.get("hooks", [g]):
                yield h["command"]


def test_codex_hooks_use_codex_events_and_client():
    hooks = load(ROOT / "plugin-guard" / "hooks" / "codex.json")
    assert set(hooks["hooks"]) == {"PreToolUse", "PostToolUse"}
    assert all("--client codex" in c and c.rstrip().endswith("|| true") for c in _commands(hooks))


def test_cursor_hooks_use_cursor_events_and_always_answer():
    hooks = load(ROOT / "plugin-guard" / "hooks" / "cursor.json")
    assert hooks["version"] == 1
    assert set(hooks["hooks"]) == {"preToolUse", "postToolUse", "beforeShellExecution", "afterShellExecution",
                                   "beforeMCPExecution", "afterMCPExecution"}
    # A Cursor permission hook with no valid answer blocks the call, so the last fallback allows.
    assert all("--client cursor" in c and c.endswith("""|| echo '{"permission":"allow"}'""") for c in _commands(hooks))


def test_claude_hooks_unchanged_format():
    hooks = load(ROOT / "plugin-guard" / "hooks" / "hooks.json")
    assert set(hooks["hooks"]) == {"PreToolUse", "PostToolUse"}
