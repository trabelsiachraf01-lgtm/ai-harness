import json
import shutil
import sys

import pytest

from harness.prompts import tools_prompt
from harness.tools import ToolRegistry, default_registry
from harness.tools.edit import Edit
from harness.tools.write import Write


@pytest.fixture
def work(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    return root


def call(root, tool, name, **args):
    return ToolRegistry([tool(root)]).run(name, json.dumps(args))


def write(root, command, stdin=None):
    return call(root, Write, "write", command=command, stdin=stdin)


def edit(root, command, stdin=None):
    return call(root, Edit, "edit", command=command, stdin=stdin)


# ---- write ---------------------------------------------------------------------------------

def test_tee_writes_stdin_to_a_file(work):
    assert write(work, "mkdir -p notes").ok
    r = write(work, "tee notes/todo.md", stdin="- one\n- two\n")
    assert r.ok and (work / "notes/todo.md").read_text() == "- one\n- two\n"


def test_write_argument_rules(work):
    assert "needs the file content" in write(work, "tee a.txt").output
    assert "only used with tee" in write(work, "touch a.txt", stdin="x").output
    assert "needs a file path" in write(work, "tee -a", stdin="x").output
    assert "not allowed in write" in write(work, "rm a.txt").output


def test_write_cannot_leave_the_workspace_or_touch_credentials(work, tmp_path):
    for command in ["tee ../x.txt", f"touch {tmp_path}/y.txt", "mkdir ../d", "mv a.txt ../a.txt"]:
        r = write(work, command, stdin="x" if command.startswith("tee") else None)
        assert not r.ok and "outside the workspace" in r.output
    assert not (tmp_path / "x.txt").exists()
    assert "credential" in write(work, "tee .env", stdin="KEY=1").output
    assert not (work / ".env").exists()


def test_write_cannot_escape_through_a_symlink(work, tmp_path):
    (tmp_path / "target.txt").write_text("keep")
    (work / "link.txt").symlink_to(tmp_path / "target.txt")
    r = write(work, "tee link.txt", stdin="overwritten")
    assert not r.ok and "outside the workspace" in r.output
    assert (tmp_path / "target.txt").read_text() == "keep"


def test_describe_warns_about_overwrites_and_previews_stdin(work):
    (work / "a.txt").write_text("old")
    tool = Write(work)
    shown = tool.describe(Write.Args(command="tee a.txt", stdin="\n".join(f"l{i}" for i in range(40))))
    assert "overwrites existing file: a.txt" in shown
    assert "stdin (40 lines)" in shown and "… 25 more lines" in shown
    assert "overwrites" not in tool.describe(Write.Args(command="tee fresh.txt", stdin="x"))
    assert "overwrites existing file: a.txt" in tool.describe(Write.Args(command="cp b.txt a.txt"))


# ---- edit ----------------------------------------------------------------------------------

def test_sed_edits_in_place_with_gnu_style_flags(work):
    (work / "m.py").write_text("x = 1\ny = 2\n")
    r = edit(work, "sed -i 's/y = 2/y = 3/' m.py")  # no backup suffix: works on macOS too
    assert r.ok and (work / "m.py").read_text() == "x = 1\ny = 3\n"


def test_sed_cannot_write_or_execute(work):
    (work / "m.py").write_text("a\n")
    for command in ["sed -n 'w out.txt' m.py", "sed -i 's/a/b/w leaked.txt' m.py", "sed 's/a/id/e' m.py",
                    "sed -f script.sed m.py", "sed -e '1e id' m.py"]:
        r = edit(work, command)
        assert not r.ok and "not allowed" in r.output, command
    assert not (work / "out.txt").exists() and not (work / "leaked.txt").exists()


def test_sed_script_text_and_filenames_are_not_false_positives(work):
    (work / "w.txt").write_text("a word here\n")
    assert edit(work, "sed -i 's/word/term/' w.txt").ok
    assert (work / "w.txt").read_text() == "a term here\n"


@pytest.mark.skipif(shutil.which("patch") is None, reason="patch not installed")
def test_patch_applies_a_unified_diff_from_stdin(work):
    (work / "m.py").write_text("one\ntwo\nthree\n")
    diff = "--- m.py\n+++ m.py\n@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n"
    r = edit(work, "patch -p0", stdin=diff)
    assert r.ok, r.output
    assert (work / "m.py").read_text() == "one\nTWO\nthree\n"


def test_patch_diff_paths_must_stay_inside(work):
    for name in ["/etc/passwd", "../outside.txt", "~/x"]:
        r = edit(work, "patch -p0", stdin=f"--- {name}\n+++ {name}\n@@ -0,0 +1 @@\n+x\n")
        assert not r.ok and "must be relative" in r.output
    assert "needs a unified diff" in edit(work, "patch -p0").output
    assert "only used with patch" in edit(work, "sed -i 's/a/b/' x", stdin="d").output


def test_edit_refuses_credential_files_and_unlisted_programs(work):
    (work / ".env").write_text("KEY=1\n")
    assert "credential" in edit(work, "sed -i 's/1/2/' .env").output
    assert (work / ".env").read_text() == "KEY=1\n"
    assert "not allowed in edit" in edit(work, "awk '{print}' x").output


@pytest.mark.skipif(sys.platform != "darwin", reason="BSD sed normalisation is macOS-only")
def test_the_macos_empty_suffix_is_added_only_when_missing(work):
    tool = Edit(work)
    assert tool.parse("sed -i 's/a/b/' f") == ["sed", "-i", "", "s/a/b/", "f"]
    assert tool.parse("sed -i '' 's/a/b/' f") == ["sed", "-i", "", "s/a/b/", "f"]
    assert tool.parse("sed -i .bak 's/a/b/' f") == ["sed", "-i", ".bak", "s/a/b/", "f"]


# ---- tools prompt --------------------------------------------------------------------------

def test_tools_prompt_is_assembled_from_the_registry(work):
    prompt = tools_prompt(default_registry(work))
    for name in ["plan:", "read:", "write:", "edit:", "fetch_url:", "web_search:"]:
        assert f"- {name}" in prompt
    assert str(work.resolve()) in prompt and "approval" in prompt and "untrusted" in prompt
    assert tools_prompt(ToolRegistry()) == ""  # no tools, no section

    only_read = ToolRegistry([Edit(work)])
    assert "- edit:" in tools_prompt(only_read) and "- write:" not in tools_prompt(only_read)
