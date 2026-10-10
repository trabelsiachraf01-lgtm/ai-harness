import json

from harness.tools import ToolRegistry, default_registry
from harness.tools.plan import Plan


def plan(*steps):
    return json.dumps({"steps": [{"step": s, "status": st} for s, st in steps]})


def test_run_reports_progress_and_renders_checklist():
    r = ToolRegistry([Plan()]).run("plan", plan(("read spec", "done"), ("write code", "in_progress"), ("test", "pending")))
    assert r.ok
    assert r.output == "Plan updated: 1/3 done.\n[x] read spec\n[~] write code\n[ ] test"


def test_display_is_the_checklist_and_never_part_of_the_output():
    r = ToolRegistry([Plan()]).run("plan", plan(("a", "done"), ("b", "pending")))
    assert r.display == "[x] a\n[ ] b"
    assert "[x] a" in r.output  # the model sees the same checklist as text
    assert ToolRegistry([Plan()]).run("plan", plan(("a", "pending"))).display == "[ ] a"


def test_two_steps_in_progress_is_rejected_with_a_reason_for_the_model():
    r = ToolRegistry([Plan()]).run("plan", plan(("a", "in_progress"), ("b", "in_progress")))
    assert not r.ok and r.display is None
    assert r.output == "Error: only one step can be in_progress at a time"


def test_schema_rejects_empty_plan_and_unknown_status():
    reg = ToolRegistry([Plan()])
    assert not reg.run("plan", '{"steps": []}').ok
    bad = reg.run("plan", '{"steps": [{"step": "a", "status": "doing"}]}')
    assert not bad.ok and "status" in bad.output


def test_spec_is_a_function_tool_with_nested_step_schema():
    spec = Plan().spec()
    assert spec["name"] == "plan" and spec["type"] == "function"
    steps = spec["parameters"]["properties"]["steps"]
    assert steps["minItems"] == 1 and steps["maxItems"] == 20


def test_default_registry_exposes_plan_and_search():
    names = [s["name"] for s in default_registry().specs() if s["type"] == "function"]
    assert "plan" in names and "fetch_url" in names


def test_common_status_spellings_are_accepted():
    r = ToolRegistry([Plan()]).run("plan", plan(("a", "completed"), ("b", "In Progress"), ("c", "todo")))
    assert r.ok and r.display == "[x] a\n[~] b\n[ ] c"
