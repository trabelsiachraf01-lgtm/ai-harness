from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import ValidationError

from harness.tools.base import Tool, ToolError


@dataclass(frozen=True)
class ToolResult:
    output: str
    ok: bool = True
    display: str | None = None


class ToolRegistry:
    """Holds client tools (we run them) and server tools (the provider runs them, spec only)."""

    def __init__(self, tools: Iterable[Tool] = (), server_tools: Iterable[dict] = (), max_output_chars: int = 20_000):
        self._tools = {t.name: t for t in tools}
        self._server_tools = list(server_tools)
        self.max_output_chars = max_output_chars

    def specs(self) -> list[dict]:
        return [t.spec() for t in self._tools.values()] + self._server_tools

    def run(self, name: str, arguments: str) -> ToolResult:
        """Never raises: every failure becomes a result the model can read and react to."""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(f"Error: unknown tool '{name}'", ok=False)
        try:
            args = tool.Args.model_validate_json(arguments or "{}")
        except ValidationError as e:
            problems = "; ".join(f"{'.'.join(map(str, err['loc'])) or 'arguments'}: {err['msg']}" for err in e.errors())
            return ToolResult(f"Error: invalid arguments: {problems}", ok=False)
        try:
            output = tool.run(args)
            display = tool.display(args)
        except ToolError as e:
            return ToolResult(f"Error: {e}", ok=False)
        except Exception as e:  # tool code is untrusted from the loop's point of view
            return ToolResult(f"Error: {type(e).__name__}: {e}", ok=False)
        extra = len(output) - self.max_output_chars
        if extra > 0:  # a tool result must never be able to flood the context
            output = f"{output[: self.max_output_chars]}\n[output truncated: {extra} more characters]"
        return ToolResult(output, display=display)
