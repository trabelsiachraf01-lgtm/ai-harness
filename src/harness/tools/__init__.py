from harness.tools.base import Tool, ToolError
from harness.tools.registry import ToolRegistry, ToolResult
from harness.tools.web_search import web_search_spec


def default_registry() -> ToolRegistry:
    return ToolRegistry(server_tools=[web_search_spec()])


__all__ = ["Tool", "ToolError", "ToolRegistry", "ToolResult", "default_registry"]
