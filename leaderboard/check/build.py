"""Stage mcp-doctor's own source for the in-browser check (check/worker.js).

Copies mcp_doctor/*.py into check/py/mcp_doctor/ and writes check/py/manifest.json
listing them plus the Pyodide wheels kept in check/py/ (tree-sitter-typescript,
which Pyodide doesn't ship; see check/py/README.md). The leaderboard workflow
runs this before publishing, so the page always runs the checkout's version."""
import json
import shutil
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "py"


def main() -> None:
    pkg = OUT / "mcp_doctor"
    shutil.rmtree(pkg, ignore_errors=True)
    pkg.mkdir(parents=True)
    files = sorted(p.name for p in (ROOT / "mcp_doctor").glob("*.py"))
    for name in files:
        shutil.copyfile(ROOT / "mcp_doctor" / name, pkg / name)
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    wheels = sorted(p.name for p in OUT.glob("*.whl"))
    (OUT / "manifest.json").write_text(json.dumps({"version": version, "files": files, "wheels": wheels}, indent=1) + "\n")
    print(f"staged mcp-doctor {version}: {len(files)} modules, wheels {wheels}")


if __name__ == "__main__":
    main()
