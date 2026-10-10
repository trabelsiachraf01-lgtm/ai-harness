import json
import os

import pytest
from pydantic import BaseModel

from harness.tools import ToolRegistry
from harness.tools.cli import CliTool
from harness.tools.read import Read


@pytest.fixture
def work(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    (root / "a.py").write_text("import os\nprint('hello')\n# TODO fix\n")
    (root / "b.py").write_text("x = 1\n")
    (root / ".env").write_text("OPENROUTER_API_KEY=sk-PLANTED\n")
    return root


def run(root, command, tool=None):
    return ToolRegistry([tool or Read(root)]).run("read", json.dumps({"command": command}))


def test_runs_a_read_only_command(work):
    r = run(work, "cat -n a.py")
    assert r.ok and "     2\tprint('hello')" in r.output
    assert run(work, "grep -n TODO a.py").output.strip() == "3:# TODO fix"


def test_shell_operators_are_plain_arguments_and_never_run(work):
    r = run(work, "cat a.py; touch pwned")
    assert not (work / "pwned").exists()
    assert "No such file" in r.output  # `;` and `touch` were just filenames given to cat
    run(work, "cat a.py > out.txt")
    assert not (work / "out.txt").exists()


def test_programs_outside_the_allowlist_are_refused(work):
    for command in ["rm a.py", "python3 -c 'print(1)'", "bash -c ls", "/bin/cat a.py"]:
        r = run(work, command)
        assert not r.ok and "not allowed in read" in r.output
    assert (work / "a.py").exists()


def test_find_cannot_execute_or_delete(work):
    for flag in ["-exec rm {} ;", "-delete", "-fprint out.txt"]:
        r = run(work, f"find . {flag}")
        assert not r.ok and "not allowed" in r.output


def test_credential_files_are_blocked_even_through_globs_and_recursion(work):
    assert "credential" in run(work, "cat .env").output
    assert "credential" in run(work, "cat ./.env").output
    out = run(work, "grep -r PLANTED .").output
    assert "sk-PLANTED" not in out  # recursive grep skips .env
    assert "sk-PLANTED" not in run(work, "rg PLANTED").output


def test_paths_outside_the_workspace_are_blocked_but_patterns_are_not(work, tmp_path):
    (tmp_path / "outside.txt").write_text("private")
    for command in [f"cat {tmp_path}/outside.txt", "cat ../outside.txt", "cat ~/.ssh/id_rsa"]:
        assert not run(work, command).ok
    (work / "link").symlink_to(tmp_path / "outside.txt")
    assert "outside the workspace" in run(work, "cat link").output
    assert run(work, "grep -rn '/api/v1' .").ok  # a pattern that looks like a path is fine


def test_globs_expand_like_a_shell_and_stay_literal_when_unmatched(work):
    out = run(work, "wc -l *.py").output
    assert "a.py" in out and "b.py" in out
    assert "No such file" in run(work, "cat *.nothing").output


def test_exit_codes_stderr_and_empty_output_are_reported(work):
    assert "[exit code 1]" in run(work, "grep -q nomatch a.py").output
    assert "No such file" in run(work, "cat nope.txt").output
    assert run(work, "grep -c hello a.py").output.strip() == "1"
    assert run(work, "ls -d nothing_here_*").ok


class EnvTool(CliTool):
    name = "read"
    description = "x"
    programs = frozenset({"env", "sleep"})

    class Args(BaseModel):
        command: str

    def run(self, args):
        return self.execute(args.command)


def test_the_model_never_sees_our_environment(work, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-SECRET-IN-ENV")
    out = run(work, "env", tool=EnvTool(work)).output
    assert "sk-SECRET-IN-ENV" not in out and "OPENROUTER" not in out
    assert "PATH=" in out


def test_timeouts_kill_the_command(work):
    r = run(work, "sleep 5", tool=EnvTool(work, timeout=0.2))
    assert not r.ok and "timed out" in r.output
