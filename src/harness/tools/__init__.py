from harness.tools.base import Tool, ToolError
from harness.tools.fetch_url import FetchUrl
from harness.tools.plan import Plan
from harness.tools.registry import ToolRegistry, ToolResult
from harness.tools.web_search import web_search_spec


def default_registry() -> ToolRegistry:
    return ToolRegistry(tools=[Plan(), FetchUrl()], server_tools=[web_search_spec()])


__all__ = ["Tool", "ToolError", "ToolRegistry", "ToolResult", "default_registry"]
