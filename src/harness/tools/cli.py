"""One CLI command, no shell. Shared by the read / write / edit tools.

The command is split with shlex and executed with shell=False, so `;`, `&&`, `|`, `$(...)`
and `>` are plain arguments: nothing can be chained or redirected. Each tool allows only its
own programs. These checks prevent accidents. The real control is the user approving every
command (see permissions.py), because a determined injection can still abuse an allowed program.
"""

import glob
import os
import re
import shlex
import subprocess
from pathlib import Path

from harness.tools.base import Tool, ToolError

# The model's shell must not be able to print our API key, so it never receives it.
ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "USER", "TMPDIR")

SECRET_NAMES = {"id_rsa", "id_ed25519", "id_ecdsa", "credentials", ".netrc", ".npmrc", ".pypirc"}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
FIND_BLOCKED = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
# recursive searches must not walk into credential files
GREP_EXCLUDES = ["--exclude=.env", "--exclude=.env.*", "--exclude=*.pem", "--exclude=*.key", "--exclude-dir=.git"]
RG_EXCLUDES = ["--glob=!.env", "--glob=!.env.*", "--glob=!*.pem", "--glob=!*.key", "--glob=!.git"]
# sed commands that write files or run programs: `w`, `W`, `e`, and the s///w, s///e flags
SED_COMMANDS = re.compile(r"(?:^|[;{}\n])\s*[0-9,$]*\s*[wWeE]\b")
SED_FLAGS = re.compile(r"s(.)(?:(?!\1).)*\1(?:(?!\1).)*\1[a-zA-Z0-9]*[we]")


class CliTool(Tool):
    """Subclasses set `name`, `description`, `programs`, `usage` and `Args`, and call `execute`."""

    programs: frozenset[str]
    paths_are_arguments = False  # True: every non-flag argument is a path and must stay in the workspace

    def __init__(self, cwd: Path, timeout: float = 30.0):
        self.cwd = cwd.resolve()
        self.timeout = timeout

    # ---- permission hooks (see permissions.py) ----------------------------------------------
    def scope(self, args) -> str | None:
        try:
            return shlex.split(args.command)[0]
        except (ValueError, IndexError):
            return None

    def describe(self, args) -> str:
        return args.command

    # ---- execution --------------------------------------------------------------------------
    def execute(self, command: str, stdin: str | None = None, *, echo: bool = True) -> str:
        argv = self.parse(command)
        env = {k: os.environ[k] for k in ENV_KEYS if k in os.environ}
        try:
            done = subprocess.run(
                argv, cwd=self.cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace",
                timeout=self.timeout, input=stdin, start_new_session=True, stdin=None if stdin is not None else subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise ToolError(f"timed out after {self.timeout:g}s") from None
        except FileNotFoundError:
            raise ToolError(f"{argv[0]} is not installed on this machine") from None
        out = done.stdout if echo else ""
        if done.stderr:
            out += ("\n" if out else "") + done.stderr
        if done.returncode != 0:
            out += f"\n[exit code {done.returncode}]"
        return out if out.strip() else "[ok, no output]"

    def parse(self, command: str) -> list[str]:
        try:
            argv = shlex.split(command)
        except ValueError as e:
            raise ToolError(f"cannot parse command: {e}") from None
        if not argv:
            raise ToolError("empty command")
        program, rest = argv[0], argv[1:]
        if program not in self.programs:
            raise ToolError(f"'{program}' is not allowed in {self.name}. Allowed: {', '.join(sorted(self.programs))}")

        script = self._check_program(program, rest)
        rest = self._expand_globs(rest)
        for i, token in enumerate(rest):
            if token.startswith("-") or i == script or token == "":
                continue
            self._check_path(token, strict=self.paths_are_arguments)
        if program in ("grep", "egrep", "fgrep") and any(t in ("-r", "-R", "--recursive") or re.fullmatch(r"-[a-zA-Z]*[rR][a-zA-Z]*", t) for t in rest):
            rest = GREP_EXCLUDES + rest
        if program == "rg":
            rest = RG_EXCLUDES + rest
        return [program, *rest]

    def _check_program(self, program: str, rest: list[str]) -> int | None:
        """Per-program rules. Returns the index of sed's script argument, which is not a path."""
        if program == "find" and FIND_BLOCKED & set(rest):
            raise ToolError(f"find options {sorted(FIND_BLOCKED & set(rest))} are not allowed")
        if program != "sed":
            return None
        if any(t in ("-f", "--file") or t.startswith("--file=") for t in rest):
            raise ToolError("sed -f is not allowed; pass the script inline")
        for i, token in enumerate(rest):
            if token in ("-e", "--expression") and i + 1 < len(rest):
                script = i + 1
                break
            if token.startswith("--expression="):
                script = i
                break
        else:
            script = next((i for i, t in enumerate(rest) if t and not t.startswith("-")), None)
        text = rest[script].split("=", 1)[-1] if script is not None else ""
        if SED_COMMANDS.search(text) or SED_FLAGS.search(text):
            raise ToolError("sed commands w, W, e and the s///w, s///e flags are not allowed")
        return script

    def _expand_globs(self, tokens: list[str]) -> list[str]:
        out: list[str] = []
        for token in tokens:
            if not token.startswith("-") and any(c in token for c in "*?["):
                matches = sorted(glob.glob(token, root_dir=self.cwd))
                out += matches or [token]  # like a shell: an unmatched pattern stays literal
            else:
                out.append(token)
        return out

    def _check_path(self, token: str, *, strict: bool) -> None:
        value = token.split("=", 1)[-1] if token.startswith("--") else token
        path = Path(value).expanduser()
        name = path.name
        if name == ".env" or name.startswith(".env.") or name in SECRET_NAMES or path.suffix in SECRET_SUFFIXES:
            raise ToolError("access to credential files is blocked")
        resolved = (path if path.is_absolute() else self.cwd / path).resolve()  # follows symlinks
        # a search pattern like "/api/v1" is not a path: outside the strict tools, only existing files count
        if (strict or resolved.exists()) and not resolved.is_relative_to(self.cwd):
            raise ToolError(f"path is outside the workspace ({self.cwd})")
