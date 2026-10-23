// "Check any MCP server": resolve what the visitor typed to a GitHub repo, run
// mcp-doctor on it in worker.js, and show grades. Same disclosure rule as the
// scan-request bot: security findings are counts only.

const MAX_ROWS = 15;
const form = document.getElementById("check-form");
const input = document.getElementById("target");
const button = document.getElementById("check-button");
const statusEl = document.getElementById("status");
const resultEl = document.getElementById("result");
let worker = null;

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function setStatus(text, isError = false) {
  statusEl.hidden = !text;
  statusEl.textContent = text || "";
  statusEl.classList.toggle("error", isError);
}

// github.com/o/r, https://github.com/o/r/tree/ref/sub/dir, o/r, git+https://github.com/o/r.git
function parseGitHub(text) {
  const m = text.trim().match(/^(?:git\+)?(?:https?:\/\/)?(?:www\.)?github\.com[/:]([\w.-]+)\/([\w.-]+?)(?:\.git)?(?:\/tree\/([^/]+)(?:\/(.+?))?)?\/?(?:[#?].*)?$/i);
  if (m) return { owner: m[1], repo: m[2], ref: m[3] || null, subdir: m[4] || null };
  // owner/repo or owner/repo/sub/dir (the form ?repo= share links use)
  const short = text.trim().match(/^([\w.-]+)\/([\w.-]+)(?:\/(.+?))?\/?$/);
  if (short && !text.trim().startsWith("@")) return { owner: short[1], repo: short[2], ref: null, subdir: short[3] || null };
  return null;
}

async function fromNpm(name) {
  const res = await fetch(`https://registry.npmjs.org/${name.startsWith("@") ? "@" + encodeURIComponent(name.slice(1)) : encodeURIComponent(name)}`);
  if (!res.ok) return null;
  const pkg = await res.json();
  const repo = pkg.repository || (pkg.versions && pkg.versions[pkg["dist-tags"]?.latest]?.repository);
  const url = typeof repo === "string" ? repo : repo?.url;
  const target = url && parseGitHub(url.replace(/^github:/, "github.com/"));
  if (target && repo?.directory) target.subdir = repo.directory;
  return target;
}

async function fromPyPI(name) {
  const res = await fetch(`https://pypi.org/pypi/${encodeURIComponent(name)}/json`);
  if (!res.ok) return null;
  const info = (await res.json()).info || {};
  const urls = Object.values(info.project_urls || {}).concat(info.home_page || []);
  for (const u of urls) {
    const t = u && parseGitHub(u.replace(/\/(issues|blob|releases|wiki)(\/.*)?$/, ""));
    if (t) return t;
  }
  // No project URLs (mcp-atlassian on PyPI): take the repo its README links most.
  const counts = {};
  for (const m of (info.description || "").matchAll(/github\.com\/([\w.-]+)\/([\w.-]+)/g)) {
    if (m[1] === "user-attachments") continue;
    const key = `${m[1]}/${m[2].replace(/\.git$/, "")}`;
    counts[key] = (counts[key] || 0) + 1;
  }
  const best = Object.entries(counts).sort((a, b) => b[1] - a[1])[0];
  return best ? parseGitHub(best[0]) : null;
}

class Ambiguous extends Error {
  constructor(name, npm, pypi) {
    super(`"${name}" is two different packages.`);
    this.choices = [["npm", npm], ["pypi", pypi]];
  }
}

async function resolveTarget(text) {
  const raw = text.trim().replace(/^(npm|pypi):/i, "");
  const forced = (text.trim().match(/^(npm|pypi):/i) || [])[1]?.toLowerCase();
  if (!forced) {
    const gh = parseGitHub(raw);
    if (gh) return { ...gh, via: null };
  }
  setStatus(`Looking up the package "${raw}"…`);
  const [npm, pypi] = await Promise.all([
    forced === "pypi" ? null : fromNpm(raw).catch(() => null),
    forced === "npm" ? null : fromPyPI(raw).catch(() => null),
  ]);
  // The same name on both registries can be two unrelated servers
  // ("mcp-atlassian" on npm is not the popular PyPI one): ask, never guess.
  if (npm && pypi && label(npm).toLowerCase() !== label(pypi).toLowerCase()) {
    throw new Ambiguous(raw, npm, pypi);
  }
  const t = npm || pypi;
  if (t) return { ...t, via: `${npm ? "npm" : "PyPI"} package ${raw}` };
  throw new Error(`Couldn't find a GitHub repo for "${raw}". Paste the GitHub link instead.`);
}

function label(t) {
  return `${t.owner}/${t.repo}` + (t.subdir ? `/${t.subdir}` : "");
}

function shareUrl(t) {
  const u = new URL(location.href);
  u.search = "";
  u.searchParams.set("repo", label(t));
  if (t.ref) u.searchParams.set("ref", t.ref);
  return u.toString();
}

function gradeCard(name, grade, pct, hint) {
  const g = grade && grade !== "N/A" ? grade : "–";
  return `<div class="grade-card"><div class="label">${esc(name)}</div>
    <div class="value g-${esc(g)}">${esc(g)}<small>${grade && grade !== "N/A" ? esc(pct) + "%" : "not graded"}</small></div>
    <div class="label">${esc(hint)}</div></div>`;
}

function render(target, report, meta) {
  const issues = [...(report.repo_issues || [])];
  for (const t of report.tools || []) for (const i of t.issues || []) issues.push({ ...i, tool: t.name });
  const quality = issues.filter(i => (i.category || "quality") === "quality");
  const security = issues.filter(i => i.category === "security");
  const rank = { error: 0, warning: 1, info: 2 };
  quality.sort((a, b) => (rank[a.severity] ?? 3) - (rank[b.severity] ?? 3));
  const secCounts = {};
  for (const i of security) secCounts[i.severity || "warning"] = (secCounts[i.severity || "warning"] || 0) + 1;
  const tools = (report.tools || []).length;
  const tsGap = meta.tsFiles && !meta.tsSupported;

  let html = `<h2>${esc(label(target))}</h2>
    <p class="sub">${target.via ? `From ${esc(target.via)} · ` : ""}branch <code>${esc(meta.ref)}</code>
    · ${meta.stars != null ? `${meta.stars.toLocaleString()} stars · ` : ""}mcp-doctor ${esc(meta.version)}</p>`;

  if (tools === 0 && tsGap) {
    html += `<div class="note"><strong>Not graded yet in the browser.</strong> This looks like a TypeScript server, and
      the in-browser check can't read TypeScript tools yet. Get the full report with the command below, or
      <a href="https://github.com/vishalhabib99/mcp-doctor/issues/new?template=scan-request.yml&title=${encodeURIComponent("Scan " + label(target))}">ask the scan bot</a>.</div>`;
  } else if (tools === 0) {
    html += `<div class="note"><strong>Not graded: no tools found.</strong> The server may live in a subfolder
      (paste the GitHub link to that folder), or it registers tools in a way mcp-doctor can't read yet.
      If it does have tools, <a href="https://github.com/vishalhabib99/mcp-doctor/issues/new">tell us</a>: that's a bug worth fixing.</div>`;
  } else {
    const breakages = report.breakages || [];
    if (breakages.length) {
      html += `<div class="note breakages"><strong>Will break an agent: ${breakages.length}</strong><ul>`;
      for (const b of breakages.slice(0, MAX_ROWS)) {
        const where = b.line ? `${b.file}:${b.line}` : b.file;
        const who = b.tool ? `<code>${esc(b.tool)}</code> (${esc(where)})` : `<code>${esc(where)}</code>`;
        html += `<li>${who}: ${esc(b.breaks)}. <strong>Fix:</strong> ${esc(b.fix)}. <span class="sub">(${esc(b.evidence)})</span></li>`;
      }
      html += `</ul></div>`;
    } else if (report.breakages) {
      html += `<div class="note ok"><strong>No known agent-breaking issues</strong> (checked: null defaults, stdout on stdio, mcp 2.x start-up).</div>`;
    }
    html += `<div class="grades">
      ${gradeCard("Quality", report.grade, report.percent, "Can an agent tell what each tool does?")}
      ${gradeCard("Security", report.security_grade, report.security_percent, "Risky patterns in code an agent can reach")}
      <div class="grade-card"><div class="label">Tools found</div><div class="value">${tools}</div><div class="label">Python · TypeScript · Go · Rust</div></div>
    </div>`;
    if (tsGap) html += `<div class="note">This repo also has TypeScript files. The in-browser check doesn't read TypeScript tools yet, so those aren't counted here; the command line version does.</div>`;
  }

  if (tools > 0) {
    html += `<h3>Quality findings (${quality.length})</h3>`;
    if (quality.length) {
      html += `<div class="table-scroll"><table class="findings"><thead><tr><th>Severity</th><th>Where</th><th>What an agent would hit</th></tr></thead><tbody>`;
      for (const i of quality.slice(0, MAX_ROWS)) {
        html += `<tr><td>${esc(i.severity)}</td><td><code>${esc(i.tool || i.file || "repo")}</code></td><td>${esc(i.message)}</td></tr>`;
      }
      html += `</tbody></table></div>`;
      if (quality.length > MAX_ROWS) html += `<p class="sub">…and ${quality.length - MAX_ROWS} more in the full report.</p>`;
    } else {
      html += `<p>None. Every tool is documented well enough for an agent to use.</p>`;
    }
    const secTotal = security.length;
    const breakdown = Object.entries(secCounts).sort().map(([s, n]) => `${n} ${s}${n === 1 ? "" : "s"}`).join(", ");
    html += `<h3>Security findings (${secTotal})</h3>
      <p>${secTotal ? `${esc(breakdown)}. Details aren't shown on a public page; see them on your own machine:` : "None found."}</p>`;
  }

  const dir = target.subdir ? `${target.repo}/${target.subdir}` : target.repo;
  html += `<h3>Full report on your machine</h3>
    <pre>pip install mcp-server-lint
git clone --depth 1 https://github.com/${esc(target.owner)}/${esc(target.repo)}.git
mcp-doctor ${esc(dir)}</pre>
    <div class="actions">
      <button type="button" id="copy-link">Copy link to this check</button>
      <a class="btn" href="https://github.com/vishalhabib99/mcp-doctor#github-action">Maintainer? Check every PR</a>
      <a class="btn" href="../">Leaderboard</a>
    </div>`;

  resultEl.innerHTML = html;
  resultEl.hidden = false;
  document.getElementById("copy-link").addEventListener("click", async e => {
    try { await navigator.clipboard.writeText(shareUrl(target)); e.target.textContent = "Link copied"; }
    catch { e.target.textContent = shareUrl(target); }
  });
}

// Each check carries an id the worker echoes back. Without it, a check started
// while another was still scanning got the earlier repo's grades under its own
// name (seen in testing on 2026-10-15).
let nextId = 0;

function runWorker(target) {
  if (!worker) worker = new Worker("worker.js", { type: "module" });
  const id = ++nextId;
  return new Promise((resolve, reject) => {
    worker.onmessage = ({ data }) => {
      if (data.id !== id) return;
      if (data.type === "progress") setStatus(data.text);
      else if (data.type === "done") resolve(data);
      else if (data.type === "error") reject(new Error(data.message));
    };
    worker.onerror = e => reject(new Error(e.message || "The checker crashed."));
    worker.postMessage({ id, target: { owner: target.owner, repo: target.repo, ref: target.ref, subdir: target.subdir } });
  });
}

async function check(text, ref) {
  button.disabled = true;
  resultEl.hidden = true;
  const started = performance.now();
  try {
    const target = await resolveTarget(text);
    if (ref) target.ref = ref;
    history.replaceState(null, "", shareUrl(target));
    document.title = `${label(target)} — Check an MCP Server`;
    const { report, meta } = await runWorker(target);
    setStatus(`Checked in ${((performance.now() - started) / 1000).toFixed(1)}s, in your browser.`);
    render(target, report, meta);
  } catch (err) {
    if (err instanceof Ambiguous) {
      setStatus("");
      resultEl.innerHTML = `<div class="note"><strong>${esc(err.message)}</strong> Which one?
        ${err.choices.map(([reg, t]) => `<a href="?repo=${encodeURIComponent(label(t))}">${reg === "npm" ? "npm" : "PyPI"}: ${esc(label(t))}</a>`).join(" · ")}</div>`;
      resultEl.hidden = false;
    } else {
      setStatus(err.message, true);
    }
  } finally {
    button.disabled = false;
  }
}

form.addEventListener("submit", e => { e.preventDefault(); if (!button.disabled) check(input.value); });

const params = new URLSearchParams(location.search);
if (params.get("repo")) {
  input.value = params.get("repo");
  check(params.get("repo"), params.get("ref"));
}
