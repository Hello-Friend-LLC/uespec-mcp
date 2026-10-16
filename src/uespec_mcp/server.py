from __future__ import annotations

import os
from typing import Any, Callable

try:
    from fastmcp import FastMCP as _FastMCP
except ImportError:
    class _FastMCP:  # pragma: no cover - exercised indirectly in tests
        def __init__(self, name: str, **_options: Any) -> None:
            self.name = name
            self.tools: dict[str, Callable[..., Any]] = {}

        def tool(self, name: str | None = None, description: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            del description

            def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
                self.tools[name or func.__name__] = func
                return func

            return decorator

        def run(self, transport: str = "stdio") -> None:
            raise RuntimeError(
                "fastmcp is not installed. Install project dependencies before running the stdio server."
            )


from .tools import compile as compile_tools
from .tools import generate, orchestrator, preview, surface, test as test_tools, validate

FastMCP = _FastMCP


def llm_tools_enabled() -> bool:
    """The LLM-adapter tools (autofix, patch proposal, image-to-spec) need an API provider; host-Claude setups have none.

    ``UESPEC_LLM_TOOLS=on|off`` forces it; the default ``auto`` registers them only when ``UESPEC_LLM_PROVIDER`` or an
    ``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` is present, which keeps ``tools/list`` small for the routed host-Claude child.
    """
    mode = os.getenv("UESPEC_LLM_TOOLS", "auto").strip().lower()
    if mode in {"on", "1", "true", "yes"}:
        return True
    if mode in {"off", "0", "false", "no"}:
        return False
    return bool(os.getenv("UESPEC_LLM_PROVIDER") or os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY"))


def build_server() -> FastMCP:
    mcp = FastMCP("uespec", include_fastmcp_meta=False)
    modules = [surface, validate, compile_tools, test_tools, preview]
    if llm_tools_enabled():
        modules += [orchestrator, generate]
    for module in modules:
        module.register(mcp)
    _ensure_tools_attribute(mcp)
    return mcp


def list_registered_tools(mcp: FastMCP) -> dict[str, Any]:
    tools = getattr(mcp, "tools", None)
    if isinstance(tools, dict):
        return dict(tools)

    manager = getattr(mcp, "_tool_manager", None)
    raw_tools = getattr(manager, "_tools", {}) if manager is not None else {}
    if not isinstance(raw_tools, dict):
        return {}
    return {name: _tool_callable(tool) for name, tool in raw_tools.items()}


def _ensure_tools_attribute(mcp: FastMCP) -> None:
    if isinstance(getattr(mcp, "tools", None), dict):
        return
    tools = list_registered_tools(mcp)
    if not tools:
        return
    try:
        setattr(mcp, "tools", tools)
    except Exception:
        pass


def _tool_callable(tool: Any) -> Any:
    return getattr(tool, "fn", None) or getattr(tool, "function", None) or tool


mcp = build_server()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
