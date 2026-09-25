"""Tools an agent can't tell apart: two differently named tools whose descriptions say the same
thing once case, punctuation and filler words are set aside.

This is deliberately the narrow version of "look-alike tools". Scoring description similarity
(TF-IDF cosine) across 756 real tools from 18 servers mostly surfaced well-designed CRUD
families: `update_person` / `update_company` score 0.86 but differ in exactly the word an
agent needs, which is also in the name. Only identical descriptions are something text alone
can prove, and on that corpus the rule fired once, on a real copy-paste bug (rhinomcp's
`modify_objects` described as "Create multiple objects at once in the Rhino document.",
word for word the same as `create_objects`).

Whether near-identical tools actually confuse an agent is a question for a real agent test,
not for string matching.
"""

from __future__ import annotations

import re

# Articles and prepositions only. Verbs stay: "Get ..." vs "Set ..." is exactly what separates
# two tools, and an earlier draft that dropped them flagged get/set pairs as identical.
_FILLER = {"a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "by", "from", "at", "into", "via"}


def _normalize(description: str) -> tuple[str, ...]:
    # \w, not [a-z0-9]: an ASCII-only tokenizer threw away every CJK character, so two different
    # Chinese descriptions (xiaohongshu-mcp's user_profile / get_my_profile) compared equal on
    # their few English leftovers.
    return tuple(w for w in re.findall(r"\w+", description.lower()) if w not in _FILLER)


def find_indistinguishable_tools(tools: list[tuple[str, str | None]]) -> list[tuple[str, str]]:
    """(name, description) pairs in, pairs of tool names with indistinguishable descriptions out.
    Tools with no description are skipped (the missing-description check already covers them),
    and so are repeats of the same name (the duplicate-name check covers those)."""
    by_text: dict[tuple[str, ...], list[str]] = {}
    for name, description in tools:
        key = _normalize(description or "")
        if key:
            names = by_text.setdefault(key, [])
            if name not in names:
                names.append(name)
    pairs = []
    for names in by_text.values():
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                pairs.append((names[i], names[j]))
    return pairs


def message(other: str) -> str:
    return (
        f"Description is identical to '{other}' — an agent reading tools/list has nothing to tell "
        "these two apart, and at least one of them likely describes the wrong tool (a copy-paste)."
    )
