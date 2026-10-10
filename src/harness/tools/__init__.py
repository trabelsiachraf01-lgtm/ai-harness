import platform
from pathlib import Path

from harness.config import workspace_root
from harness.permissions import Permissions
from harness.tools.base import Tool, ToolError
from harness.tools.edit import Edit
from harness.tools.fetch_url import FetchUrl
from harness.tools.plan import Plan
from harness.tools.read import Read
from harness.tools.registry import ToolRegistry, ToolResult
from harness.tools.web_search import USAGE as SEARCH_USAGE, web_search_spec
from harness.tools.write import Write


def default_registry(root: Path | None = None, permissions: Permissions | None = None) -> ToolRegistry:
    """Without `permissions`, everything that needs approval is denied."""
    cwd = root or workspace_root()
    tools = [Plan(), Read(cwd), Write(cwd), Edit(cwd), FetchUrl()]
    environment = f"{'macOS' if platform.system() == 'Darwin' else platform.system()}, workspace: {cwd}"
    return ToolRegistry(tools=tools, server_tools=[web_search_spec()], permissions=permissions or Permissions(),
                        server_notes=[SEARCH_USAGE], environment=environment)


__all__ = ["Tool", "ToolError", "ToolRegistry", "ToolResult", "default_registry"]
