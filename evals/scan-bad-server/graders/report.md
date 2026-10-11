---
type: llm
weight: 2
---
The workspace is a FastMCP server whose real mcp-doctor scores are Quality 13% (grade F) and Security 100% (grade A), with 2 tools (`do_thing`, `run`) in server.py. The eval sandbox may stop the CLI from running; either outcome below can pass.

Pass only if the final response does ONE of these:
- (scanner ran) gives quality and security as two separate scores matching those numbers, never merged into one;
- (scanner didn't run) says so up front, states no grade or percentage at all, and labels its findings as its own reading of the code.

And in both cases:
- lists at most five findings, each tied to a file and line in server.py, followed by no more than three one-line notes (security, repo-level files, count left out);
- explains at least one finding as what goes wrong for the agent calling the tool, not just the rule name;
- does not claim it changed any file.
