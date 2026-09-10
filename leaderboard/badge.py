"""Generate static, shields.io-style SVG badges for scanned repos.

Each badge shows the repo's real mcp-doctor quality grade/percent (e.g.
"mcp-doctor A 95%") so a maintainer can embed it in their own README as
verifiable, dated proof — not a generic "scanned by" badge. Colors reuse
the same A-F scheme as the CLI's own terminal/JSON output (see
mcp_doctor/report.py's _color_for_grade) for consistency across surfaces.
"""

from __future__ import annotations

# Matches mcp_doctor.report._color_for_grade's A/B/C/D/F -> green/green/yellow/yellow/red
# scheme, just as hex values suited to an SVG fill instead of an ANSI code.
_GRADE_COLOR = {
    "A": "#4c1",
    "B": "#97ca00",
    "C": "#dfb317",
    "D": "#fe7d37",
    "F": "#e05d44",
}

_FONT_FAMILY = "Verdana,Geneva,DejaVu Sans,sans-serif"

# Approximate per-character advance width (px) for 11px Verdana, the same
# rough estimate shields.io's own flat template has historically used.
# Not pixel-perfect kerning, but close enough for a legible flat badge.
_CHAR_WIDTH = 6.5
_PAD = 10


def _text_width(text: str) -> int:
    return round(len(text) * _CHAR_WIDTH + _PAD)


def render_badge_svg(label: str, message: str, grade: str) -> str:
    """Render a two-segment flat badge: dark `label` | colored `message`."""
    color = _GRADE_COLOR.get(grade, "#9f9f9f")
    label_w = _text_width(label)
    msg_w = _text_width(message)
    total_w = label_w + msg_w
    height = 20

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="{height}" role="img" aria-label="{label}: {message}">
  <title>{label}: {message}</title>
  <linearGradient id="s" x2="0" y2="100%">
    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>
  <clipPath id="r">
    <rect width="{total_w}" height="{height}" rx="3" fill="#fff"/>
  </clipPath>
  <g clip-path="url(#r)">
    <rect width="{label_w}" height="{height}" fill="#555"/>
    <rect x="{label_w}" width="{msg_w}" height="{height}" fill="{color}"/>
    <rect width="{total_w}" height="{height}" fill="url(#s)"/>
  </g>
  <g fill="#fff" text-anchor="middle" font-family="{_FONT_FAMILY}" font-size="11">
    <text x="{label_w / 2}" y="14">{label}</text>
    <text x="{label_w + msg_w / 2}" y="14">{message}</text>
  </g>
</svg>
"""


def badge_filename(owner: str, repo: str) -> str:
    return f"{owner}-{repo}.svg"
