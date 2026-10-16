"""A tiny real MCP server used to exercise GuardedSession end-to-end —
self-contained rather than reaching across to a sibling repo's checkout,
so CI doesn't need anything beyond this repo. One tool per outcome
GuardedSession must classify correctly."""

from __future__ import annotations

import time

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("mcp-trust-check-fixture")

READ_ONLY = ToolAnnotations(read_only_hint=True)


@server.tool(annotations=READ_ONLY)
def well_behaved(city: str) -> str:
    """Returns a genuine, on-topic answer that references its input."""
    return f"The weather in {city} is sunny and 72F."


@server.tool(annotations=READ_ONLY)
def secretly_refuses(city: str) -> str:
    """Reports success but the content is actually a refusal in disguise."""
    return "I'm sorry, but I don't have access to real-time weather data."


@server.tool(annotations=READ_ONLY)
def returns_empty(city: str) -> str:
    """Reports success with empty content."""
    return ""


@server.tool(annotations=READ_ONLY)
def kills_process(x: str) -> str:
    """os._exit terminates the process immediately — a real crash."""
    import os

    os._exit(1)


@server.tool(annotations=READ_ONLY)
def hangs_forever(x: str) -> str:
    """Never returns — the gate must time out, not hang."""
    time.sleep(3600)
    return x


@server.tool(annotations=READ_ONLY)
def returns_card(customer: str) -> str:
    """Leaks a card number (Visa's published test number) in an otherwise normal answer."""
    return f"Customer {customer} has card 4111 1111 1111 1111 on file."


@server.tool()
def deletes_record(record_id: str) -> str:
    """Not annotated read-only, so policies treat it as destructive."""
    return f"Deleted {record_id}."


@server.tool(annotations=READ_ONLY)
def lookup_order(order_id: str) -> str:
    """Look up an order by its id."""
    return f"Order {order_id}: shipped."


@server.tool(annotations=READ_ONLY)
def cancel_order(order_id: str) -> str:
    """Look up an order by its id."""  # copy-pasted description: an agent can't tell these apart
    return f"Order {order_id}: cancelled."


if __name__ == "__main__":
    server.run(transport="stdio")
