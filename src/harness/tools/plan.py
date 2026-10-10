from typing import Literal

from pydantic import BaseModel, Field, field_validator

from harness.tools.base import Tool, ToolError

MARKS = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
# Models often write these instead of the schema values; accept them rather than failing the call.
STATUS_ALIASES = {"completed": "done", "complete": "done", "in progress": "in_progress", "todo": "pending"}


def render(steps: list["Plan.Step"]) -> str:
    return "\n".join(f"{MARKS[s.status]} {s.step}" for s in steps)


class Plan(Tool):
    name = "plan"
    needs_approval = False
    usage = (
        "plan: for any task with three or more steps, write the plan first and keep it current: one step "
        "in_progress, mark each done as you finish it. Send the whole list every time."
    )
    description = (
        "Create or update the plan for the current task. Send the complete list every time: each call "
        "replaces the previous plan. Use it for tasks with three or more steps. Keep exactly one step "
        "in_progress, and mark each step done as soon as it is finished."
    )

    class Step(BaseModel):
        step: str = Field(min_length=1, description="One concrete action")
        status: Literal["pending", "in_progress", "done"] = "pending"

        @field_validator("status", mode="before")
        @classmethod
        def _alias(cls, value):
            return STATUS_ALIASES.get(str(value).strip().lower(), value)

    class Args(BaseModel):
        steps: list["Plan.Step"] = Field(min_length=1, max_length=20)

    def run(self, args: "Plan.Args") -> str:
        self._check(args)
        done = sum(s.status == "done" for s in args.steps)
        return f"Plan updated: {done}/{len(args.steps)} done.\n{render(args.steps)}"

    def display(self, args: "Plan.Args") -> str:
        return render(args.steps)

    @staticmethod
    def _check(args: "Plan.Args") -> None:
        if sum(s.status == "in_progress" for s in args.steps) > 1:
            raise ToolError("only one step can be in_progress at a time")
