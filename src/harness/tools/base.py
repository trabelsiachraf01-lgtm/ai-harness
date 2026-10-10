from pydantic import BaseModel


class ToolError(Exception):
    """Expected failure. The message goes back to the model as-is so it can adjust."""


class Tool:
    """One file per tool: subclass, set `name`, `description`, `Args`, and implement `run`.

    The same pydantic model produces the JSON schema the model sees and validates the
    arguments it sends, so the two can't drift apart.
    """

    name: str
    description: str
    Args: type[BaseModel]
    needs_approval = True  # every tool asks first unless it opts out (e.g. plan has no side effects)
    usage: str = ""  # guidance for the tools prompt: longer than `description`, with examples and limits

    def spec(self) -> dict:
        schema = self.Args.model_json_schema()
        schema.pop("title", None)
        return {"type": "function", "name": self.name, "description": self.description, "parameters": schema}

    def run(self, args) -> str:
        raise NotImplementedError

    def scope(self, args) -> str | None:
        """What an "allow for this session" rule is keyed on, e.g. the program name. None = whole tool."""
        return None

    def describe(self, args) -> str:
        """What the user is shown when asked to approve this call."""
        return ", ".join(f"{k}={v!r}" for k, v in args.model_dump().items())

    def display(self, args) -> str | None:
        """Optional text for the terminal only. The model never sees it."""
        return None
