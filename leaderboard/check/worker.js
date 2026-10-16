// Runs the real mcp-doctor (Python, via Pyodide) on a GitHub repo, entirely in
// the visitor's browser: nothing is uploaded and no server of ours is involved.
// Only the files mcp-doctor reads are downloaded (fetch_rules.json); every
// other file in the tree is created empty so name checks (LICENSE, tests,
// packaging) still see it. tests/test_check_parity.py holds these rules to
// the CLI's grades.

// A module worker (check.js starts it with type: "module"): Chrome refused to
// importScripts() the classic pyodide.js in a worker; the .mjs build loads.
import { loadPyodide } from "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs";

const ROOT = "/repo";
let pyodideReady = null;

let currentId = 0;
function post(type, data) { self.postMessage({ type, id: currentId, ...data }); }

async function loadEngine() {
  post("progress", { step: "engine", text: "Loading mcp-doctor in your browser…" });
  const pyodide = await loadPyodide();
  await pyodide.loadPackage(["tree-sitter", "tree-sitter-go", "micropip"]);
  const manifest = await (await fetch("py/manifest.json")).json();
  if (manifest.wheels.length) {
    const micropip = pyodide.pyimport("micropip");
    const base = new URL("py/", self.location.href).href;
    await micropip.install(manifest.wheels.map(w => base + w), { deps: false });
  }
  pyodide.FS.mkdirTree("/pkg/mcp_doctor");
  await Promise.all(manifest.files.map(async name => {
    const text = await (await fetch(`py/mcp_doctor/${name}`)).text();
    pyodide.FS.writeFile(`/pkg/mcp_doctor/${name}`, text);
  }));
  pyodide.runPython(`
import sys, json
sys.path.insert(0, "/pkg")
from pathlib import Path
from mcp_doctor.analyzer import analyze_repo
from mcp_doctor.report import render_json
import mcp_doctor.ts_analyzer as _ts
def _scan(root):
    return render_json(analyze_repo(Path(root)))
`);
  return { pyodide, version: manifest.version, ts: pyodide.runPython("_ts.TS_AVAILABLE") };
}

function wanted(path, size, rules) {
  const parts = path.split("/");
  const name = parts[parts.length - 1];
  const dirs = parts.slice(0, -1);
  if (dirs.some(d => rules.skip_dirs.includes(d))) return null; // not created at all
  if (size > rules.max_file_bytes) return false;
  const dot = name.lastIndexOf(".");
  const suffix = dot > 0 ? name.slice(dot) : "";
  const js = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"].includes(suffix);
  if (js && dirs.some(d => rules.skip_js_build_dirs.includes(d) || d.startsWith("."))) return false;
  return rules.content_suffixes.includes(suffix)
    || rules.content_names.includes(name)
    || rules.content_name_prefixes.some(p => name.startsWith(p));
}

async function gh(url) {
  const res = await fetch(url, { headers: { Accept: "application/vnd.github+json" } });
  if (res.status === 403 || res.status === 429) {
    const reset = res.headers.get("x-ratelimit-reset");
    const when = reset ? new Date(Number(reset) * 1000).toLocaleTimeString() : "in a few minutes";
    throw new Error(`GitHub's limit for anonymous lookups from your network was reached (60 an hour). Try again after ${when}.`);
  }
  if (res.status === 404) throw new Error("Repo not found. Is it public, and is the name right?");
  if (!res.ok) throw new Error(`GitHub answered ${res.status}.`);
  return res.json();
}

async function download(target, rules) {
  const { owner, repo } = target;
  post("progress", { step: "tree", text: `Reading the file list of ${owner}/${repo}…` });
  const meta = await gh(`https://api.github.com/repos/${owner}/${repo}`);
  const ref = target.ref || meta.default_branch;
  // Two API calls per check (repo + tree): anonymous visitors get 60 an hour.
  const tree = await gh(`https://api.github.com/repos/${owner}/${repo}/git/trees/${encodeURIComponent(ref)}?recursive=1`);
  const sha = ref;
  if (tree.truncated) throw new Error("This repo is too big for the in-browser check. Use the command line version below.");

  const prefix = target.subdir ? target.subdir.replace(/\/+$/, "") + "/" : "";
  const blobs = tree.tree.filter(e => e.type === "blob" && (!prefix || e.path.startsWith(prefix)));
  if (prefix && !blobs.length) throw new Error(`No folder named ${target.subdir} in ${owner}/${repo}.`);
  const fetchList = [], empty = [];
  for (const e of blobs) {
    const rel = e.path.slice(prefix.length);
    const w = wanted(rel, e.size || 0, rules);
    if (w === null) continue;
    (w ? fetchList : empty).push({ path: e.path, rel, size: e.size || 0 });
  }
  const bytes = fetchList.reduce((n, f) => n + f.size, 0);
  if (fetchList.length > rules.max_content_files || bytes > rules.max_content_bytes) {
    throw new Error(`This repo has ${fetchList.length} source files (${Math.round(bytes / 1e6)} MB), more than the in-browser check reads. Use the command line version below.`);
  }

  const files = [];
  let done = 0;
  const queue = fetchList.slice();
  async function workerLoop() {
    while (queue.length) {
      const f = queue.shift();
      const url = `https://raw.githubusercontent.com/${owner}/${repo}/${sha}/${f.path.split("/").map(encodeURIComponent).join("/")}`;
      const res = await fetch(url);
      if (!res.ok) throw new Error(`Couldn't download ${f.path} (${res.status}).`);
      files.push({ rel: f.rel, data: new Uint8Array(await res.arrayBuffer()) });
      done++;
      if (done % 10 === 0 || done === fetchList.length) {
        post("progress", { step: "files", text: `Downloading source files: ${done} of ${fetchList.length}` });
      }
    }
  }
  await Promise.all(Array.from({ length: 12 }, workerLoop));
  return { files, empty: empty.map(e => e.rel), sha, ref, stars: meta.stargazers_count, language: meta.language };
}

function writeTree(pyodide, files, empty) {
  const FS = pyodide.FS;
  if (FS.analyzePath(ROOT).exists) pyodide.runPython(`import shutil; shutil.rmtree("${ROOT}")`);
  FS.mkdirTree(ROOT);
  const mk = rel => { const i = rel.lastIndexOf("/"); if (i > 0) FS.mkdirTree(`${ROOT}/${rel.slice(0, i)}`); };
  for (const f of files) { mk(f.rel); FS.writeFile(`${ROOT}/${f.rel}`, f.data); }
  for (const rel of empty) { mk(rel); FS.writeFile(`${ROOT}/${rel}`, new Uint8Array()); }
}

// One check at a time, so every message carries the id of the check it belongs to.
let queue = Promise.resolve();
self.onmessage = ({ data }) => { queue = queue.then(() => run(data)); };

async function run(data) {
  currentId = data.id;
  try {
    // A failed engine load (network blip) is retried on the next check.
    pyodideReady = pyodideReady || loadEngine().catch(e => { pyodideReady = null; throw e; });
    const rules = await (await fetch("fetch_rules.json")).json();
    const [engine, repo] = await Promise.all([pyodideReady, download(data.target, rules)]);
    // ha-mcp (1,768 source files) takes ~50 s in Pyodide vs 22 s in the CLI;
    // say so, or a long scan looks frozen.
    const n = repo.files.length;
    post("progress", { step: "scan", text: n > 500
      ? `Scanning ${n} source files. A repo this size takes a minute or two in the browser…`
      : `Scanning ${n} source files…` });
    writeTree(engine.pyodide, repo.files, repo.empty);
    const report = JSON.parse(engine.pyodide.globals.get("_scan")(ROOT));
    const tsFiles = repo.files.some(f => /\.(ts|tsx|mts|cts)$/.test(f.rel) && !/\.d\.[mc]?ts$/.test(f.rel));
    post("done", {
      report,
      meta: { sha: repo.sha, ref: repo.ref, stars: repo.stars, language: repo.language, version: engine.version, tsSupported: engine.ts, tsFiles },
    });
  } catch (err) {
    post("error", { message: err.message || String(err) });
  }
}
