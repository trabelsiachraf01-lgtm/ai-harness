import json

from pydantic import BaseModel

from harness.tools import Tool, ToolError, ToolRegistry, web_search_spec


class Add(Tool):
    name = "add"
    description = "add two ints"

    class Args(BaseModel):
        a: int
        b: int

    def run(self, args):
        if args.a < 0:
            raise ToolError("negative numbers are not supported")
        if args.b == 99:
            raise RuntimeError("boom")
        return str(args.a + args.b)


def registry(**kw):
    return ToolRegistry([Add()], [web_search_spec()], **kw)


def test_specs_list_function_tools_then_server_tools():
    specs = registry().specs()
    assert specs[0]["type"] == "function" and specs[0]["name"] == "add"
    assert specs[0]["parameters"]["required"] == ["a", "b"]
    assert "title" not in specs[0]["parameters"]
    assert specs[1] == web_search_spec()
    assert specs[1]["parameters"]["engine"] == "exa"


def test_run_ok():
    r = registry().run("add", json.dumps({"a": 2, "b": 3}))
    assert (r.output, r.ok) == ("5", True)


def test_every_failure_becomes_a_result_not_an_exception():
    reg = registry()
    assert "unknown tool 'nope'" in reg.run("nope", "{}").output
    bad_type = reg.run("add", '{"a": "x", "b": 1}')
    assert not bad_type.ok and "a:" in bad_type.output
    assert "b: Field required" in reg.run("add", '{"a": 1}').output
    assert "Invalid JSON" in reg.run("add", "not json").output
    assert reg.run("add", '{"a": -1, "b": 1}').output == "Error: negative numbers are not supported"
    assert reg.run("add", '{"a": 1, "b": 99}').output == "Error: RuntimeError: boom"


def test_output_is_truncated_with_a_notice():
    class Big(Tool):
        name, description = "big", "x"

        class Args(BaseModel):
            pass

        def run(self, args):
            return "x" * 100

    r = ToolRegistry([Big()], max_output_chars=10).run("big", "{}")
    assert r.output.startswith("x" * 10) and "90 more characters" in r.output and r.ok
