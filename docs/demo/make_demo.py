"""Build docs/demo.svg: an animated terminal walking the four steps (find, fix,
ship, guard) on the bundled examples, then an end card. Every terminal line is
real output, captured 2026-10-29 from mcp-doctor 0.35.11 with mcp 2.3.0
(examples/bad_server, a copy of it after `mcp-doctor --fix`, examples/good_server,
and guard in front of tests/fixtures/guard_server.py fs_broken). Long lines are
wrapped, never reworded. Scene 1 leaves out some findings to fit (the README sample
has the full report), scene 2 shows only the changed lines of `git diff`, and
scene 3 shows verdict's Markdown rendered (no ###, ** or emoji) without the
server's own stderr logging. The end card's numbers come from docs/coverage.md and
the merged PRs linked from the profile README.

Earlier versions opened the release scene with `verdict` on examples/bad_server,
which BLOCKs only because that example never calls mcp.run() ("server never
connected"), so it no longer appears here.

    python docs/demo/make_demo.py   (writes docs/demo.svg)
"""
from html import escape
from pathlib import Path

W, H = 940, 420
FONT, CH, LH = 13.5, 8.13, 21  # font size, monospace char width, line height
X0, Y0 = 24, 82
CYCLE = 46.0  # seconds
SANS = "-apple-system,Segoe UI,Helvetica,Arial,sans-serif"
C = {"fg": "#e6edf3", "dim": "#8b949e", "green": "#3fb950", "red": "#f85149", "yellow": "#d29922",
     "blue": "#58a6ff", "purple": "#bc8cff", "bg": "#0d1117", "bar": "#161b22", "add": "#12261e", "del": "#2d1517"}
STEPS = ["Find", "Fix", "Ship", "Guard"]

# (step index, caption, start, end, lines); a line is a list of (text, color) or ("$", command)
SCENES = [
    (0, "Find what will break an agent, before anyone calls the tool", 0.0, 10.5, [
        ("$", "mcp-doctor examples/bad_server"),
        [("mcp-doctor report", "dim")],
        [("Will break an agent: 1", "red")],
        [("  run(timeout) (server.py:18): ", "fg"), ("a client that sends the advertised null default gets", "fg")],
        [("  a validation error, and the tool never runs.", "fg")],
        [("      Fix: ", "green"), ("type it `T | None` (`mcp-doctor --fix` does this). ", "fg"), ("(fixed by maintainers in 22 repos)", "dim")],
        [("", "fg")],
        [("Quality:  22%  Grade: ", "fg"), ("F", "red"), ("  (2 tool(s) found)", "dim")],
        [("Security: 100%  Grade: ", "fg"), ("A", "green")],
        [("  [FAIL] ", "red"), ("do_thing (server.py:12)", "fg")],
        [("      ERROR  ", "red"), ("Tool has no description. An agent cannot decide when to call this.", "fg")],
        [("  [FAIL] ", "red"), ("run (server.py:18)", "fg")],
        [("      ERROR  ", "red"), ("Bare 'except:' swallows all errors including cancellation — catch specific exceptions.", "fg")],
    ]),
    (1, "Fix it in place, and the re-check comes back clean", 10.5, 19.5, [
        ("$", "mcp-doctor --fix examples/bad_server"),
        [("Fixed 1 file(s): server.py", "green")],
        [("mcp-doctor report", "dim")],
        [("No known agent-breaking issues ", "green"), ("(checked: null defaults, stdout on stdio, mcp 2.x start-up)", "dim")],
        [("", "fg")],
        ("$", "git diff examples/bad_server"),
        [("-def run(cmd, timeout: int = None):", "red", "del")],
        [("+def run(cmd, timeout: int | None = None):", "green", "add")],
        [("-    except:", "red", "del")],
        [("+    except Exception:", "green", "add")],
    ]),
    (2, "Launch a server, fuzz it, and get one release decision", 19.5, 28.5, [
        ("$", 'mcp-doctor verdict examples/good_server --run "python examples/good_server/server.py" \\'),
        [("    --include-destructive", "fg")],
        [("mcp-doctor verdict: ", "fg"), ("SHIP", "green"), (" (HIGH confidence) · static 100% (A) · fuzz 100% (A) · truth 100% (A)", "dim")],
        [("", "fg")],
        [("Release decision: ", "fg"), ("SHIP", "green")],
        [("No rule fired.", "fg")],
        [("Confidence: HIGH", "fg"), (" · needs human review: no", "dim")],
        [("- fuzz called 1 of 1 tools (100%).", "dim")],
    ]),
    (3, "Catch a \"success\" that didn't happen, live", 28.5, 39.5, [
        ("$", "mcp-doctor guard -- python tests/fixtures/guard_server.py fs_broken"),
        [("agent  → ", "blue"), ('tools/call write_file {"path": "/tmp/report.md", "content": "# Q3 report"}', "fg")],
        [("server → ", "purple"), ('"Successfully wrote to /tmp/report.md"', "fg")],
        [("guard  → ", "yellow"), ("⚠ mcp-doctor guard: write_file reported success, but reading it back with", "yellow")],
        [("         ", "fg"), ("get_file_info(path='/tmp/report.md') failed: \"Error: ENOENT: no such file", "yellow")],
        [("         ", "fg"), ("or directory, stat '/tmp/report.md'\". The write may not have happened.", "yellow")],
        [("", "fg")],
        [("The result still says success (isError: false); guard adds the warning the agent reads,", "dim")],
        [("so it can retry or tell you instead of saying \"done\".", "dim")],
    ]),
]
END = (39.5, CYCLE)

css, body, n = [], [], 0


def window(start, end, appear):
    """Keyframes: hidden, then shown from `appear` until the scene ends."""
    global n
    n += 1
    a, e = appear / CYCLE * 100, end / CYCLE * 100
    css.append(f"@keyframes k{n}{{0%,{max(a - 0.01, 0):.3f}%{{opacity:0}}{a:.3f}%,{max(e - 0.4, a):.3f}%{{opacity:1}}"
               f"{e:.3f}%,100%{{opacity:0}}}}.k{n}{{animation:k{n} {CYCLE}s linear infinite}}")
    return f"k{n}"


def steps(active):
    """Step tracker in the title bar: done steps green, the current one bright, the rest dim."""
    out, x = [], W - 24
    for i in reversed(range(len(STEPS))):
        label = STEPS[i]
        if i == active:
            fill, weight = C["fg"], "600"
        elif active is None or i < active:
            fill, weight = C["green"], "400"
        else:
            fill, weight = C["dim"], "400"
        mark = "✓ " if (active is None or i < active) else ""
        text = mark + label
        out.append(f'<text x="{x}" y="34" text-anchor="end" fill="{fill}" '
                   f'style="font:{weight} 13px {SANS}">{escape(text)}</text>')
        x -= len(text) * 7.0 + 20
        if i:
            out.append(f'<text x="{x + 10}" y="34" text-anchor="middle" fill="{C["dim"]}" style="font:13px {SANS}">›</text>')
    return "".join(out)


for s, (step, caption, start, end, lines) in enumerate(SCENES):
    first = s == 0
    t = start + 0.4
    body.append(f'<g class="{window(start, end, start)}{" s0" if first else ""}">{steps(step)}'
                f'<text x="{W // 2}" y="{H - 17}" text-anchor="middle" fill="{C["fg"]}" '
                f'style="font:14px {SANS}">{escape(caption)}</text></g>')
    y = Y0
    for line in lines:
        if line[0] == "$":
            cmd = line[1]
            k = window(start, end, t)
            width = len(cmd) * CH
            typing = min(1.6, 0.03 * len(cmd))
            n_cover = window(start, end, t)  # unique name for the cover's slide
            a, b = t / CYCLE * 100, (t + typing) / CYCLE * 100
            css.append(f"@keyframes c{n_cover}{{0%,{a:.3f}%{{transform:translateX(0)}}{b:.3f}%,100%{{transform:translateX({width + 20:.0f}px)}}}}"
                       f".c{n_cover}{{animation:c{n_cover} {CYCLE}s steps({len(cmd)}) infinite}}")
            body.append(f'<g class="{k}{" s0" if first else ""}"><text x="{X0}" y="{y}"><tspan fill="{C["green"]}">$ </tspan>'
                        f'<tspan fill="{C["fg"]}">{escape(cmd)}</tspan></text>'
                        f'<rect class="c{n_cover}{" s0c" if first else ""}" x="{X0 + 2 * CH}" y="{y - 15}" width="{width + 20:.0f}" height="{LH}" fill="{C["bg"]}"/></g>')
            t += typing + 0.45
        else:
            k = window(start, end, t)
            hl = ""
            if len(line[0]) == 3:  # diff line: tint the row
                txt, col, bg = line[0]
                line = [(txt, col)]
                hl = f'<rect x="{X0 - 6}" y="{y - 15}" width="{len(txt) * CH + 12:.0f}" height="{LH - 1}" rx="3" fill="{C[bg]}"/>'
            spans = "".join(f'<tspan fill="{C[col]}">{escape(txt)}</tspan>' for txt, col in line)
            body.append(f'<g class="{k}{" s0" if first else ""}">{hl}<text x="{X0}" y="{y}" xml:space="preserve">{spans}</text></g>')
            t += 0.16
        y += LH
    assert y <= H - 52, f"scene {STEPS[step]} runs into the caption bar"
    assert t < end - 4.0, f"scene {STEPS[step]} leaves under 4s to read ({t:.1f}s of {end}s)"

# End card: what to run, and why to trust it.
start, end = END
cx = W // 2
card = [
    (150, f'<text x="{cx}" y="150" text-anchor="middle" fill="{C["fg"]}" style="font:700 30px {SANS}">mcp-doctor</text>'),
    (190, f'<text x="{cx}" y="190" text-anchor="middle" fill="{C["dim"]}" style="font:15px {SANS}">Find → Fix → Ship → Guard, in one open-source CLI</text>'),
    (240, f'<rect x="{cx - 170}" y="216" width="340" height="38" rx="8" fill="{C["bar"]}" stroke="#30363d"/>'
          f'<text x="{cx}" y="241" text-anchor="middle"><tspan fill="{C["green"]}">$ </tspan><tspan fill="{C["fg"]}">pip install mcp-server-lint</tspan></text>'),
    (292, f'<text x="{cx}" y="292" text-anchor="middle" fill="{C["fg"]}" style="font:14.5px {SANS}">'
          f'Finds 15,755 of 15,977 tools across 805 real MCP servers, every miss published</text>'),
    (318, f'<text x="{cx}" y="318" text-anchor="middle" fill="{C["fg"]}" style="font:14.5px {SANS}">'
          f'Fixes merged by maintainers at Microsoft · AWS · NVIDIA · Intel · IBM · Cisco</text>'),
]
body.append(f'<g class="{window(start, end, start)}">{steps(None)}</g>')
for i, (_, el) in enumerate(card):
    body.append(f'<g class="{window(start, end, start + 0.3 + 0.35 * i)}">{el}</g>')

svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img"
 aria-label="mcp-doctor demo: find a bug that will break an agent (quality 22%, grade F), fix it with mcp-doctor --fix and re-check clean, get a SHIP release verdict on a clean server, and guard a live call that reported success without writing the file">
<style>
text{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;font-size:{FONT}px;white-space:pre}}
{"".join(css)}
@media (prefers-reduced-motion: reduce){{*{{animation:none!important}}[class^="k"]{{opacity:0}}.s0{{opacity:1}}.s0c{{display:none}}}}
</style>
<rect width="{W}" height="{H}" rx="12" fill="{C["bg"]}"/>
<rect width="{W}" height="58" rx="12" fill="{C["bar"]}"/><rect y="46" width="{W}" height="12" fill="{C["bar"]}"/>
<circle cx="26" cy="29" r="6.5" fill="#ff5f57"/><circle cx="48" cy="29" r="6.5" fill="#febc2e"/><circle cx="70" cy="29" r="6.5" fill="#28c840"/>
<text x="94" y="34" fill="{C["dim"]}" style="font-size:13px">mcp-doctor · evals and guardrails for AI agents</text>
<line x1="0" y1="{H - 40}" x2="{W}" y2="{H - 40}" stroke="{C["bar"]}" stroke-width="2"/>
{"".join(body)}
</svg>
"""
out = Path(__file__).resolve().parents[1] / "demo.svg"
out.write_text(svg)
print(f"wrote {out} ({len(svg):,} bytes)")
