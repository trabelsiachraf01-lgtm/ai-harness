import json

import pytest
from pydantic import BaseModel

from harness import ui
from harness.permissions import Decision, Permissions, Request, allow_all, deny_all
from harness.runner import LLMRunner, ToolEnd
from harness.sessions import Session
from harness.tools import Tool, ToolRegistry
from harness.config import LLMConfig
from tests.test_runner import call, completed, reply, runner as make_runner, sse


class Greet(Tool):
    name = "greet"
    description = "say hi"

    class Args(BaseModel):
        who: str

    def __init__(self):
        self.ran = []

    def scope(self, args):
        return args.who

    def describe(self, args):
        return f"greet {args.who}"

    def run(self, args):
        self.ran.append(args.who)
        return f"hi {args.who}"


class Quiet(Tool):  # no side effects, so it never asks
    name = "quiet"
    description = "x"
    needs_approval = False

    class Args(BaseModel):
        pass

    def run(self, args):
        return "ok"


def setup(approver, store=None):
    tool = Greet()
    perms = Permissions(approver, store=store)
    return tool, perms, ToolRegistry([tool, Quiet()], permissions=perms)


def ask(reg, who="ann"):
    return reg.run("greet", json.dumps({"who": who}))


class Script:
    """An approver that answers from a list and records what it was asked."""

    def __init__(self, *decisions):
        self.decisions, self.asked = list(decisions), []

    def __call__(self, request):
        self.asked.append(request)
        return self.decisions.pop(0)


def test_nothing_runs_without_an_approver():
    tool, _, reg = setup(deny_all)
    r = ask(reg)
    assert not r.ok and r.permission == "denied" and "denied this call" in r.output
    assert tool.ran == []


def test_allow_once_asks_every_time_and_shows_the_exact_call():
    script = Script(Decision(True), Decision(True))
    tool, _, reg = setup(script)
    assert ask(reg).permission == "once"
    ask(reg)
    assert len(script.asked) == 2 and tool.ran == ["ann", "ann"]
    assert script.asked[0] == Request("greet", "ann", "greet ann")


def test_session_rule_is_keyed_on_the_scope():
    script = Script(Decision(True, "session"), Decision(False, reason="no"))
    tool, perms, reg = setup(script)
    assert ask(reg, "ann").permission == "session"  # asked once, and remembered
    again = ask(reg, "ann")
    assert again.ok and again.permission == "session" and len(script.asked) == 1  # not asked again
    other = ask(reg, "bob")  # a different scope still asks
    assert not other.ok and len(script.asked) == 2
    assert tool.ran == ["ann", "ann"]


def test_whole_tool_rule_covers_every_scope():
    script = Script(Decision(True, "session", whole_tool=True))
    tool, perms, reg = setup(script)
    ask(reg, "ann")
    assert ask(reg, "bob").ok and ask(reg, "cy").ok
    assert len(script.asked) == 1 and "greet" in perms.session


def test_always_rules_persist_and_can_be_revoked(tmp_path):
    store = tmp_path / "permissions.json"
    _, perms, reg = setup(Script(Decision(True, "always")), store=store)
    ask(reg, "ann")
    assert json.loads(store.read_text()) == {"allow": ["greet:ann"]}

    _, fresh, reg2 = setup(deny_all, store=store)  # a new run
    assert ask(reg2, "ann").permission == "always"
    assert fresh.revoke("greet:ann") and not ask(reg2, "ann").ok
    assert json.loads(store.read_text()) == {"allow": []}


def test_session_rules_reset_between_sessions():
    script = Script(Decision(True, "session"), Decision(False))
    _, perms, reg = setup(script)
    ask(reg)
    perms.reset_session()
    assert not ask(reg).ok and len(script.asked) == 2  # asked again after the reset


def test_denial_reason_goes_back_to_the_model():
    _, _, reg = setup(Script(Decision(False, reason="not that folder")))
    r = ask(reg)
    assert "Reason: not that folder" in r.output and "Do not retry" in r.output


def test_tools_without_side_effects_and_bad_arguments_never_ask():
    script = Script()
    _, _, reg = setup(script)
    assert reg.run("quiet", "{}").ok
    assert not reg.run("greet", "{}").ok  # invalid arguments fail before any prompt
    assert script.asked == []


def test_a_corrupt_rules_file_means_no_rules(tmp_path):
    store = tmp_path / "permissions.json"
    store.write_text("not json")
    assert Permissions(deny_all, store=store).always == set()


# ---- through the runner ---------------------------------------------------------------------

def test_runner_records_the_decision_and_the_model_sees_the_denial(tmp_path):
    tool = Greet()
    reg = ToolRegistry([tool], permissions=Permissions(Script(Decision(False, reason="nope"))))
    r = make_runner(tmp_path, [sse(completed([call("greet", {"who": "ann"})])), reply("ok")], tools=reg)
    events = list(r.chat("go"))
    end = next(e for e in events if isinstance(e, ToolEnd))
    assert end.ok is False and end.permission == "denied"
    assert "denied this call" in r.sent[1]["input"][-1]["output"]
    assert r.last_stats["tools"][0]["permission"] == "denied" and tool.ran == []


def test_the_default_registry_denies_everything_that_needs_approval(tmp_path):
    r = LLMRunner(LLMConfig(api_key="k"), Session.new(tmp_path))
    result = r.tools.run("read", json.dumps({"command": "ls"}))
    assert result.permission == "denied"


# ---- terminal approver ----------------------------------------------------------------------

@pytest.mark.parametrize("key,expected", [
    ("o", Decision(True)),
    ("s", Decision(True, "session")),
    ("S", Decision(True, "session", whole_tool=True)),
    ("a", Decision(True, "always")),
    ("A", Decision(True, "always", whole_tool=True)),
    ("d", Decision(False, reason="because")),
    ("", Decision(False, reason="because")),  # Enter is a denial, never an approval
    ("x", Decision(False, reason="because")),
])
def test_keys_map_to_decisions(key, expected):
    assert ui._decision_for(key, lambda: "because") == expected


def test_terminal_approver_shows_the_full_command_and_pauses_the_spinner(monkeypatch):
    import io
    from rich.console import Console
    c = Console(file=io.StringIO(), theme=ui.THEME, width=70, force_terminal=False)
    approver = ui.TerminalApprover(c)
    view = ui.ReplyView(c)
    view.waiting()
    approver.view = view
    monkeypatch.setattr(ui, "_ask_key", lambda: "s")
    decision = approver(Request("read", "rg", "rg -n 'a very long pattern' " + "src/ " * 20))
    assert decision == Decision(True, "session")
    out = c.file.getvalue()
    assert "read" in out and "rg -n" in out and out.count("src/") >= 20  # nothing truncated
    assert "(read:rg)" in out
    view.close()


def test_typeahead_is_discarded_before_the_prompt(monkeypatch):
    calls = []
    monkeypatch.setattr(ui.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(ui.sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(ui.termios, "tcflush", lambda fd, queue: calls.append((fd, queue)))
    monkeypatch.setattr(ui.time, "sleep", lambda s: calls.append(("sleep", s)))
    ui._drain_input()
    assert [c[0] for c in calls] == [0, "sleep", 0]  # flush, wait for stragglers, flush again


def test_permissions_listing_and_empty_state():
    import io
    from rich.console import Console
    c = Console(file=io.StringIO(), theme=ui.THEME, width=80)
    ui.permissions_text(c, Permissions(deny_all))
    assert "every call asks" in c.file.getvalue()
    perms = Permissions(deny_all)
    perms.session.add("read:grep")
    perms.always.add("fetch_url")
    c = Console(file=io.StringIO(), theme=ui.THEME, width=80)
    ui.permissions_text(c, perms)
    out = c.file.getvalue()
    assert "read:grep" in out and "this session" in out and "fetch_url" in out and "always" in out
