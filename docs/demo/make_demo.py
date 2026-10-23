"""Build docs/demo.svg: an animated terminal walking one server through build,
release and production. Every line is real output, captured 2026-10-17 from
mcp-doctor 0.28.0 (examples/bad_server, examples/good_server, and guard in front
of tests/fixtures/guard_server.py fs_broken); long lines are cut with an ellipsis.
The bad_server score lines were re-captured 2026-10-19 (0.32.1, bug #125), and every
other line was re-checked against 0.32.1 the same day: unchanged.
Every line re-checked against 0.33.0 on 2026-10-20: unchanged.
Re-checked against 0.35.0 on 2026-10-23. Same day, examples/bad_server gained a null-default
bug (run's `timeout: int = None`) so the lint report opens with a real breakage; scene 1 and
the BLOCK line were re-captured then (quality 24% -> 22%). Scene 1 leaves out some findings
to fit (the repo-level ones among them); the README sample has the full report.

    python docs/demo/make_demo.py   (writes docs/demo.svg)
"""
from html import escape
from pathlib import Path

W, H = 940, 400
FONT, CH, LH = 13.5, 8.13, 21  # font size, monospace char width, line height
X0, Y0 = 24, 74
CYCLE = 27.0  # seconds
C = {"fg": "#e6edf3", "dim": "#8b949e", "green": "#3fb950", "red": "#f85149", "yellow": "#d29922",
     "blue": "#58a6ff", "purple": "#bc8cff", "bg": "#0d1117", "bar": "#161b22"}

# (stage label, caption, start, end, lines); a line is a list of (text, color) or ("$", command)
SCENES = [
    ("1/3  Build", "Lint the server before an agent ever calls it", 0.0, 8.5, [
        ("$", "mcp-doctor examples/bad_server"),
        [("mcp-doctor report", "dim")],
        [("Will break an agent: 1", "red")],
        [("  run(timeout) (server.py:18): ", "fg"), ("a client that sends the advertised null default gets a validation error, …", "fg")],
        [("      Fix: ", "green"), ("type it `T | None` (`mcp-doctor --fix` does this). ", "fg"), ("(fixed by maintainers in 20 repos)", "dim")],
        [("Quality:  22%  Grade: ", "fg"), ("F", "red"), ("  (2 tool(s) found)", "dim")],
        [("Security: 100%  Grade: ", "fg"), ("A", "green")],
        [("  [FAIL] ", "red"), ("do_thing (server.py:12)", "fg")],
        [("      ERROR  ", "red"), ("Tool has no description. An agent cannot decide when to call this.", "fg")],
        [("      WARNING  ", "yellow"), ("2/2 parameters have no type annotation.", "fg")],
        [("  [FAIL] ", "red"), ("run (server.py:18)", "fg")],
        [("      ERROR  ", "red"), ("Tool has no description. An agent cannot decide when to call this.", "fg")],
        [("      WARNING  ", "yellow"), ("timeout defaults to None but is typed as a non-optional type, so the schema …", "fg")],
        [("      ERROR  ", "red"), ("Bare 'except:' swallows all errors including cancellation — catch …", "fg")],
    ]),
    ("2/3  Release", "Launch it, fuzz it, and get one decision", 8.5, 18.5, [
        ("$", 'mcp-doctor verdict examples/bad_server --run "python examples/bad_server/server.py"'),
        [("mcp-doctor verdict: ", "fg"), ("BLOCK", "red"), (" (HIGH confidence) · static 22% (F)", "dim")],
        [("Blockers", "red"), (" (any one of these blocks release):", "dim")],
        [("- fuzz: server never connected (MCPError: Connection closed)", "fg")],
        [("- fuzz --truth: server never connected (MCPError: Connection closed)", "fg")],
        [("Fix first", "yellow"), (" (shippable once addressed, or knowingly accepted):", "dim")],
        [("- static scan: 4 spec/documentation errors", "fg")],
        [("", "fg")],
        ("$", 'mcp-doctor verdict examples/good_server --run "python examples/good_server/server.py" \\'),
        [("    --include-destructive", "fg")],
        [("mcp-doctor verdict: ", "fg"), ("SHIP", "green"), (" (HIGH confidence) · static 100% (A) · fuzz 100% (A) · truth 100% (A)", "dim")],
        [("No rule fired. ", "fg"), ("fuzz called 1 of 1 tools (100%).", "dim")],
    ]),
    ("3/3  Production", "Catch a \"success\" that didn't happen, live", 18.5, CYCLE, [
        ("$", "mcp-doctor guard -- python tests/fixtures/guard_server.py fs_broken"),
        [("agent  → ", "blue"), ('tools/call write_file {"path": "/tmp/report.md", "content": "# Q3 report"}', "fg")],
        [("server → ", "purple"), ('"Successfully wrote to /tmp/report.md"', "fg")],
        [("guard  → ", "yellow"), ("⚠ mcp-doctor guard: write_file reported success, but reading it back with", "yellow")],
        [("         ", "fg"), ("get_file_info(path='/tmp/report.md') failed: \"Error: ENOENT: no such file", "yellow")],
        [("         ", "fg"), ("or directory, …\". The write may not have happened.", "yellow")],
        [("", "fg")],
        [("The result still says success (isError: false); guard adds the warning the agent reads,", "dim")],
        [("so it can retry or tell you instead of saying \"done\".", "dim")],
    ]),
]

css, body, n = [], [], 0


def window(start, end, appear):
    """Keyframes: hidden, then shown from `appear` until the scene ends."""
    global n
    n += 1
    a, e = appear / CYCLE * 100, end / CYCLE * 100
    css.append(f"@keyframes k{n}{{0%,{max(a - 0.01, 0):.3f}%{{opacity:0}}{a:.3f}%,{max(e - 0.4, a):.3f}%{{opacity:1}}"
               f"{e:.3f}%,100%{{opacity:0}}}}.k{n}{{animation:k{n} {CYCLE}s linear infinite}}")
    return f"k{n}"


for s, (stage, caption, start, end, lines) in enumerate(SCENES):
    first = s == 0
    t = start + 0.4
    body.append(f'<g class="{window(start, end, start)}{" s0" if first else ""}">'
                f'<text x="{W - 24}" y="46" text-anchor="end" fill="{C["dim"]}" font-size="12.5">{escape(stage)}</text>'
                f'<text x="{W // 2}" y="{H - 17}" text-anchor="middle" fill="{C["fg"]}" font-size="13.5" '
                f'font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">{escape(caption)}</text></g>')
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
            t += typing + 0.35
        else:
            k = window(start, end, t)
            spans = "".join(f'<tspan fill="{C[col]}">{escape(txt)}</tspan>' for txt, col in line)
            body.append(f'<text class="{k}{" s0" if first else ""}" x="{X0}" y="{y}" xml:space="preserve">{spans}</text>')
            t += 0.12
        y += LH
    assert t < end - 1.5, f"scene {stage} too long for its window ({t:.1f}s of {end}s)"

svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img"
 aria-label="mcp-doctor demo: lint a server (1 finding that will break an agent, quality 22%, grade F), get a release verdict (BLOCK, then SHIP), and guard a live call that reported success without writing the file">
<style>
text{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;font-size:{FONT}px;white-space:pre}}
{"".join(css)}
@media (prefers-reduced-motion: reduce){{*{{animation:none!important}}[class^="k"]{{opacity:0}}.s0{{opacity:1}}.s0c{{display:none}}}}
</style>
<rect width="{W}" height="{H}" rx="12" fill="{C["bg"]}"/>
<rect width="{W}" height="58" rx="12" fill="{C["bar"]}"/><rect y="46" width="{W}" height="12" fill="{C["bar"]}"/>
<circle cx="26" cy="29" r="6.5" fill="#ff5f57"/><circle cx="48" cy="29" r="6.5" fill="#febc2e"/><circle cx="70" cy="29" r="6.5" fill="#28c840"/>
<text x="{W // 2}" y="34" text-anchor="middle" fill="{C["dim"]}" font-size="13">mcp-doctor · evals and guardrails for AI agents and the MCP tools they call</text>
<line x1="0" y1="{H - 40}" x2="{W}" y2="{H - 40}" stroke="{C["bar"]}" stroke-width="2"/>
{"".join(body)}
</svg>
"""
out = Path(__file__).resolve().parents[1] / "demo.svg"
out.write_text(svg)
print(f"wrote {out} ({len(svg):,} bytes)")
