from pydantic import BaseModel, Field

from harness.tools.cli import CliTool


class Read(CliTool):
    name = "read"
    description = (
        "Read files and search the workspace with one read-only CLI command (no pipes, redirects or "
        "substitutions). Programs: cat, head, tail, grep, rg, ls, find, wc, file, stat, tree, diff. "
        "Examples: `cat -n src/app.py`, `head -n 40 README.md`, `rg -n 'def main' src`, `ls -la src`. "
        "For a large file use head, tail or grep."
    )
    programs = frozenset({"cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "ls", "find", "wc",
                          "file", "stat", "tree", "diff"})
    usage = (
        "read: one read-only command per call, no pipes. `cat -n file` numbers lines, `head -n N` / "
        "`tail -n N` read ends, `rg -n 'pattern' dir` searches, `ls -la dir` and `find . -name '*.py'` list. "
        "Credential files (.env, keys) are blocked."
    )

    class Args(BaseModel):
        command: str = Field(description="One read-only CLI command, e.g. `rg -n 'TODO' src`")

    def run(self, args: "Read.Args") -> str:
        return self.execute(args.command)
