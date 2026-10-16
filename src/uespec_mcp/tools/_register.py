from __future__ import annotations

from typing import Any, Callable

from ._envelope import compacting


def add_tool(mcp: Any, name: str, description: str, func: Callable[..., Any], *, compact: bool = False) -> None:
    """Register a tool with a one-paragraph description and no outputSchema (keeps tools/list small).

    ``compact=True`` wraps the MCP-facing callable so replies stay under ~2 KB (full reply written under Saved/UESpec/artifacts).
    """
    target = compacting(name)(func) if compact else func
    try:
        mcp.tool(name=name, description=description, output_schema=None)(target)
    except TypeError:  # the minimal fallback FastMCP used when fastmcp is not installed
        mcp.tool(name=name, description=description)(target)
