import subprocess
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
        if argv[0] != "patch":
            if args.stdin is not None:
                raise ToolError("`stdin` is only used with patch")
            return self.execute(args.command)
        if args.stdin is None:
            raise ToolError("patch needs a unified diff in `stdin`")
        self._check_diff_paths(args.stdin)
        return self._apply_patch(argv[1:], args.stdin)

    def _apply_patch(self, rest: list[str], diff: str) -> str:
        """All or nothing. A dry run first, so a diff that only partly matches changes no file, and no
        .orig/.rej files are left behind (patch writes them on offsets and rejected hunks)."""
        flags = self._backup_flags()
        code, out = self._run(["patch", "--dry-run", *flags, *rest], diff)
        if code != 0:
            raise ToolError(
                "the diff does not apply cleanly, so nothing was changed. Read the file again and "
                f"regenerate the diff from its current contents.\n{out.strip()}")
        code, out = self._run(["patch", *flags, *rest], diff)
        return out.strip() or "[ok, no output]" if code == 0 else f"{out.strip()}\n[exit code {code}]"

    def _backup_flags(self) -> list[str]:
        """No backup files. GNU patch and BSD patch (macOS) spell this differently."""
        if self._flags is None:
            try:
                version = subprocess.run(["patch", "--version"], capture_output=True, text=True, timeout=5).stdout
            except (OSError, subprocess.SubprocessError):
                version = ""
            self._flags = ["--no-backup-if-mismatch", "-r", "-"] if "GNU patch" in version else ["-V", "none"]
        return self._flags

    _flags: list[str] | None = None

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
