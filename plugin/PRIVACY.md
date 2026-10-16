# Privacy

The mcp-doctor plugin and the `mcp-doctor` CLI it runs collect no data. There is no telemetry, no account and no server.

- The CLI reads your MCP server's source files on your machine and prints a report. It makes no network requests.
- The only network access is optional and one-time: if the CLI isn't installed, `uvx` or `pipx` (with your approval) downloads the `mcp-server-lint` package from PyPI.
- The `mcp-doctor-guard` plugin runs locally on each tool call. It keeps line and secret hashes of tool results (never the text) in a journal file in your temp folder, used only within the session, and makes no network requests.
- What Claude does with the report happens in your Claude session, under your Claude account's own terms.

Separate from the plugin: the no-install scan on GitHub (opening a "scan request" issue) is public by design, since GitHub issues are public. That service only reads repositories you name.

Questions: open an issue at https://github.com/vishalhabib99/mcp-doctor/issues
