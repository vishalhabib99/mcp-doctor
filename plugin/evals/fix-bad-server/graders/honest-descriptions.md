---
type: llm
weight: 2
focus: {source: file, path: server.py}
---
This is the server file after the agent edited it. In the original, `run(cmd)` ignores `cmd` and always returns the string "ran"; it never executes anything. `do_thing(x, y)` returns x divided by y.

Pass only if:
- both tools now have a description (docstring);
- the description of `run` does NOT claim it executes, runs or launches a command or shell (that would be invented behavior; saying it is a placeholder or only returns "ran" is fine);
- the description of `do_thing` is consistent with dividing x by y;
- the bare `except:` is gone.
