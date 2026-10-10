import shlex
import sys
from pathlib import PurePosixPath

from pydantic import BaseModel, Field

from harness.tools.base import ToolError
from harness.tools.cli import CliTool


class Edit(CliTool):
    name = "edit"
    description = (
        "Change an existing file with `sed` or `patch`. Read the file first so your match is exact. For "
        "multi-line changes put a unified diff in `stdin` and run `patch -p0`. Quote sed scripts."
    )
    programs = frozenset({"sed", "patch"})
    paths_are_arguments = True
    usage = (
        "edit: change an existing file. `sed -i 's/old/new/' file` for one-line changes (quote the script). "
        "For anything bigger, send a unified diff in `stdin` and run `patch -p0`; paths in the diff must be "
        "relative to the workspace. Read the file first so the text you match is exact."
    )

    class Args(BaseModel):
        command: str = Field(description="`sed -i 's/old/new/' file`, or `patch -p0` with a diff in stdin")
        stdin: str | None = Field(None, description="Unified diff for patch")

    def run(self, args: "Edit.Args") -> str:
        argv = self.parse(args.command)
        if argv[0] == "patch":
            if args.stdin is None:
                raise ToolError("patch needs a unified diff in `stdin`")
            self._check_diff_paths(args.stdin)
        elif args.stdin is not None:
            raise ToolError("`stdin` is only used with patch")
        return self.execute(args.command, args.stdin)

    def parse(self, command: str) -> list[str]:
        argv = super().parse(command)
        # GNU habit: `sed -i 's/a/b/' f`. BSD sed on macOS reads the script as the backup suffix.
        if sys.platform == "darwin" and argv[0] == "sed" and "-i" in argv[1:-1]:
            i = argv.index("-i")
            if argv[i + 1] != "" and not argv[i + 1].startswith("."):
                argv.insert(i + 1, "")
        return argv

    def execute(self, command: str, stdin: str | None = None) -> str:
        return super().execute(command, stdin)

    def describe(self, args: "Edit.Args") -> str:
        if args.stdin is None:
            return args.command
        lines = args.stdin.splitlines()
        preview = lines[:40] + ([f"… {len(lines) - 40} more lines"] if len(lines) > 40 else [])
        return f"{args.command}\n── diff ({len(lines)} lines) ──\n" + "\n".join(preview)

    def display(self, args: "Edit.Args") -> str | None:
        return args.command

    @staticmethod
    def _check_diff_paths(diff: str) -> None:
        """patch writes wherever the diff headers say, so they must stay relative and inside."""
        for line in diff.splitlines():
            if line.startswith(("--- ", "+++ ")):
                name = line[4:].split("\t")[0].strip()
                if name in ("", "/dev/null"):
                    continue
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or name.startswith("~"):
                    raise ToolError(f"diff path '{name}' must be relative and inside the workspace")
