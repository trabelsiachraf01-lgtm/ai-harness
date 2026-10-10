import shlex

from pydantic import BaseModel, Field

from harness.tools.cli import CliTool
from harness.tools.base import ToolError


class Write(CliTool):
    name = "write"
    description = (
        "Create files and folders with one CLI command. File content goes in `stdin`. Programs: tee, "
        "touch, mkdir, cp, mv. Example: command `tee notes/todo.md` with the content in stdin. `tee` "
        "overwrites, so read first. To change part of an existing file, use edit."
    )
    programs = frozenset({"tee", "touch", "mkdir", "cp", "mv"})
    paths_are_arguments = True
    usage = (
        "write: create files with one command; file content goes in `stdin`. `tee path` writes stdin to a "
        "file (and overwrites it), `mkdir -p dir` makes folders, `touch`, `cp` and `mv` work as usual. "
        "tee does not create folders: run mkdir first. Paths must stay inside the workspace."
    )

    class Args(BaseModel):
        command: str = Field(description="One CLI command, e.g. `tee notes/todo.md` or `mkdir -p build`")
        stdin: str | None = Field(None, description="File content for tee")

    def run(self, args: "Write.Args") -> str:
        program = shlex.split(args.command)[0] if args.command.strip() else ""
        if program == "tee" and args.stdin is None:
            raise ToolError("tee needs the file content in `stdin`")
        if program != "tee" and args.stdin is not None:
            raise ToolError("`stdin` is only used with tee")
        argv = self.parse(args.command)
        if program == "tee" and not [t for t in argv[1:] if not t.startswith("-")]:
            raise ToolError("tee needs a file path")
        out = self.execute(args.command, args.stdin, echo=program != "tee")  # tee would echo the file back
        if program == "tee" and out == "[ok, no output]":
            paths = [t for t in argv[1:] if not t.startswith("-")]
            return f"Wrote {len((args.stdin or '').splitlines())} lines to {', '.join(paths)}"
        return out

    def describe(self, args: "Write.Args") -> str:
        parts = [args.command]
        try:
            argv = shlex.split(args.command)
        except ValueError:
            argv = []
        paths = [t for t in argv[1:] if not t.startswith("-")]
        targets = paths if argv[:1] == ["tee"] else paths[-1:] if argv[:1] in (["cp"], ["mv"]) else []
        existing = [t for t in targets if (self.cwd / t).is_file()]
        if existing:
            parts.append(f"⚠ overwrites existing file: {', '.join(existing)}")
        if args.stdin is not None:
            lines = args.stdin.splitlines()
            preview = lines[:15] + ([f"… {len(lines) - 15} more lines"] if len(lines) > 15 else [])
            parts.append(f"── stdin ({len(lines)} lines) ──\n" + "\n".join(preview))
        return "\n".join(parts)

    def display(self, args: "Write.Args") -> str | None:
        return args.command
