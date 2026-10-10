from pathlib import Path

from harness.config import workspace_root
from harness.tools.base import Tool, ToolError
from harness.tools.fetch_url import FetchUrl
from harness.tools.plan import Plan
from harness.tools.read import Read
from harness.tools.registry import ToolRegistry, ToolResult
from harness.tools.web_search import web_search_spec


def default_registry(root: Path | None = None) -> ToolRegistry:
    cwd = root or workspace_root()
    tools = [Plan(), Read(cwd), FetchUrl()]
    return ToolRegistry(tools=tools, server_tools=[web_search_spec()])


__all__ = ["Tool", "ToolError", "ToolRegistry", "ToolResult", "default_registry"]
