import json
from datetime import date

import httpx
import pytest
from pydantic import BaseModel

from harness.config import LLMConfig
from harness.runner import LLMError, LLMRunner, Text, ToolEnd, ToolUse
from harness.sessions import Session
from harness.tools import Tool, ToolRegistry, web_search_spec


# ---- helpers: build the SSE stream the Responses API sends ---------------------------------

def sse(*events: dict) -> str:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"


def delta(text):
    return {"type": "response.output_text.delta", "delta": text}


def message(text):
    return {
        "type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": text, "logprobs": [],
                     "annotations": [{"type": "url_citation", "url": "https://x", "content": "BULKY EXCERPT"}]}],
    }


def call(name="echo", args=None, call_id="c1"):
    return {"type": "function_call", "id": "fc_1", "call_id": call_id, "name": name,
            "arguments": json.dumps(args if args is not None else {"text": "hi"})}


REASONING = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "abc"}
SEARCH = {"type": "openrouter:web_search", "id": "st_1", "status": "completed",
          "action": {"type": "search", "query": "python latest", "sources": [{"type": "url", "url": "https://py.org"}]}}


def completed(output, i=7, o=2, reasoning=0, cost=0.001):
    usage = {"input_tokens": i, "output_tokens": o, "cost": cost, "output_tokens_details": {"reasoning_tokens": reasoning}}
    return {"type": "response.completed", "response": {"model": "m/x", "output": output, "usage": usage}}


def reply(text, **kw):
    return sse(delta(text), completed([message(text)], **kw))


class Echo(Tool):
    name = "echo"
    description = "echo text"

    class Args(BaseModel):
        text: str

    def run(self, args):
        return f"echo:{args.text}"


def runner(tmp_path, responses, tools=None, **kw):
    """`responses` are SSE bodies served in order; every request body is recorded on .sent."""
    queue, sent = list(responses), []

    def handler(req):
        sent.append(json.loads(req.content))
        return queue.pop(0) if isinstance(queue[0], httpx.Response) else httpx.Response(200, text=queue.pop(0))

    http = httpx.Client(base_url="https://x/api/v1", transport=httpx.MockTransport(handler))
    cfg = LLMConfig(api_key="k", **{k: kw.pop(k) for k in list(kw) if k in LLMConfig.__dataclass_fields__})
    r = LLMRunner(cfg, Session.new(tmp_path), tools=tools if tools is not None else ToolRegistry(), http=http, **kw)
    r.sent = sent
    return r


# ---- plain chat -----------------------------------------------------------------------------

def test_streams_text_then_saves_the_turn(tmp_path):
    r = runner(tmp_path, [sse(delta("Hel"), delta("lo"), completed([message("Hello")]))])
    assert list(r.chat("hi")) == [Text("Hel"), Text("lo")]
    assert r.sent[0]["input"] == [{"role": "user", "content": "hi"}]

    saved = Session.load(r.session.path).items
    assert saved[0] == {"role": "user", "content": "hi"}
    assert saved[1]["content"][0]["text"] == "Hello"
    assert saved[1]["content"][0]["annotations"] == []  # bulky citations are not replayed
    stats = r.last_stats
    assert stats["usage"] == {"input_tokens": 7, "cached_tokens": 0, "output_tokens": 2, "reasoning_tokens": 0, "cost": 0.001}
    assert 0 <= stats["timing"]["ttft"] <= stats["timing"]["total"]


def test_second_turn_replays_history(tmp_path):
    r = runner(tmp_path, [reply("one"), reply("two")])
    list(r.chat("a"))
    list(r.chat("b"))
    assert [i.get("role") for i in r.sent[1]["input"]] == ["user", "assistant", "user"]
    assert r.sent[1]["instructions"].startswith(r.system_prompt) and r.sent[1]["stream"] is True
    assert f"Today's date: {date.today().isoformat()}" in r.sent[1]["instructions"]


def test_config_maps_to_request(tmp_path):
    r = runner(tmp_path, [reply("ok")], model="a/b", max_tokens=50, temperature=0.2, reasoning_effort="low")
    list(r.chat("x"))
    p = r.sent[0]
    assert (p["model"], p["max_output_tokens"], p["temperature"], p["reasoning"]) == ("a/b", 50, 0.2, {"effort": "low"})
    assert "tools" not in p  # empty registry sends no tools

    r = runner(tmp_path, [reply("ok")], reasoning_effort=None)
    list(r.chat("x"))
    assert "reasoning" not in r.sent[0] and "max_output_tokens" not in r.sent[0]


# ---- tool loop ------------------------------------------------------------------------------

def test_tool_loop_runs_the_tool_feeds_it_back_and_saves_everything(tmp_path):
    r = runner(
        tmp_path,
        [sse(completed([REASONING, call()], i=10, o=5, reasoning=3, cost=0.002)), reply("done", i=20, o=4, cost=0.003)],
        tools=ToolRegistry([Echo()], [web_search_spec()]),
    )
    events = list(r.chat("go"))

    assert [type(e).__name__ for e in events] == ["ToolUse", "ToolEnd", "Text"]
    assert events[0] == ToolUse("echo", '{"text": "hi"}')
    assert events[1].ok and events[1].chars == len("echo:hi")

    second = r.sent[1]["input"]
    assert [i.get("type", i.get("role")) for i in second] == ["user", "reasoning", "function_call", "function_call_output"]
    assert second[3] == {"type": "function_call_output", "call_id": "c1", "output": "echo:hi"}
    assert [t["type"] for t in r.sent[0]["tools"]] == ["function", "openrouter:web_search"]

    saved = Session.load(r.session.path).items
    assert [i.get("type", i.get("role")) for i in saved] == ["user", "reasoning", "function_call", "function_call_output", "message"]
    assert r.last_stats["usage"] == {"input_tokens": 30, "cached_tokens": 0, "output_tokens": 9, "reasoning_tokens": 3, "cost": 0.005}
    assert r.last_stats["steps"] == 2 and r.last_stats["tools"][0]["name"] == "echo"


def test_tool_errors_go_back_to_the_model_not_up_the_stack(tmp_path):
    r = runner(tmp_path, [sse(completed([call("nope")])), reply("ok")], tools=ToolRegistry([Echo()]))
    events = list(r.chat("go"))
    assert events[1].ok is False
    assert r.sent[1]["input"][-1]["output"].startswith("Error: unknown tool")
    assert r.last_stats["tools"][0]["error"].startswith("Error: unknown tool")  # kept for diagnosis


def test_server_search_is_shown_and_recorded_but_not_replayed(tmp_path):
    r = runner(tmp_path, [sse({"type": "response.output_item.done", "item": SEARCH}, delta("answer"),
                              completed([SEARCH, message("answer")]))])
    events = list(r.chat("latest python?"))
    assert events == [ToolUse("web_search", "python latest"), Text("answer")]
    assert r.last_stats["searches"] == [{"query": "python latest", "sources": ["https://py.org"]}]
    assert all(i.get("type") != "openrouter:web_search" for i in Session.load(r.session.path).items)


def _balanced(items):
    """Every function_call has its output: the history is valid to send back to the model."""
    calls = {i["call_id"] for i in items if i.get("type") == "function_call"}
    outputs = {i["call_id"] for i in items if i.get("type") == "function_call_output"}
    return calls == outputs


def test_runaway_tool_loop_stops_but_keeps_what_the_tools_did(tmp_path):
    r = runner(tmp_path, [sse(completed([call()]))] * 3, tools=ToolRegistry([Echo()]), max_steps=3)
    events = list(r.chat("loop"))
    assert isinstance(events[-1], Text) and "limit of 3 steps" in events[-1].text
    assert r.last_stats["stopped"] == "max_steps" and r.last_stats["steps"] == 3
    saved = Session.load(r.session.path).items
    assert saved[0]["content"] == "loop" and _balanced(saved) and len(saved) == 7  # user + 3 x (call, output)


# ---- a turn that already ran tools is saved even when it stops early -----------------------

def test_an_llm_error_after_a_tool_ran_keeps_the_completed_steps(tmp_path):
    r = runner(tmp_path, [sse(completed([call()])), httpx.Response(401, json={"error": "bad"})],
               tools=ToolRegistry([Echo()]))
    with pytest.raises(LLMError):
        list(r.chat("do it"))
    saved = Session.load(r.session.path).items
    assert [i.get("type", i.get("role")) for i in saved] == ["user", "function_call", "function_call_output"]
    assert r.last_stats["stopped"] == "LLMError"


def test_interrupting_after_a_tool_ran_keeps_the_completed_steps(tmp_path):
    r = runner(tmp_path, [sse(completed([call()])), reply("partial answer")], tools=ToolRegistry([Echo()]))
    gen = r.chat("go")
    for event in gen:
        if isinstance(event, Text):
            break  # the user hits Ctrl-C while the second answer streams
    gen.close()
    saved = Session.load(r.session.path).items
    assert _balanced(saved) and [i.get("type", i.get("role")) for i in saved][-1] == "function_call_output"
    assert r.last_stats["stopped"] == "GeneratorExit"


def test_a_step_interrupted_between_call_and_output_is_not_saved(tmp_path):
    class Boom(Tool):
        name = "echo"
        description = "x"

        class Args(BaseModel):
            text: str

        def run(self, args):
            raise KeyboardInterrupt  # Ctrl-C while the tool runs

    r = runner(tmp_path, [sse(completed([call()]))], tools=ToolRegistry([Boom()]))
    with pytest.raises(KeyboardInterrupt):
        list(r.chat("go"))
    assert r.session.items == [] and not r.session.path.exists()  # no half step, nothing completed


# ---- failures -------------------------------------------------------------------------------

def test_http_error_and_interrupt_do_not_save(tmp_path):
    r = runner(tmp_path, [httpx.Response(401, json={"error": "bad key"})])
    with pytest.raises(LLMError, match="401"):
        list(r.chat("hi"))
    assert r.session.items == [] and not r.session.path.exists()

    r = runner(tmp_path, [reply("partial")])
    gen = r.chat("hi")
    next(gen)
    gen.close()  # user hit Ctrl-C mid-stream
    assert r.session.items == []


@pytest.mark.parametrize("event", [
    {"type": "response.failed", "response": {"error": {"message": "upstream"}}},
    {"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}},
    {"type": "error", "error": {"message": "boom"}},
])
def test_mid_stream_failures_do_not_save(tmp_path, event):
    r = runner(tmp_path, [sse(delta("a"), event)])
    with pytest.raises(LLMError):
        list(r.chat("hi"))
    assert r.session.items == []


def test_truncated_stream_and_empty_reply_are_errors(tmp_path):
    r = runner(tmp_path, [sse(delta("a"))])
    with pytest.raises(LLMError, match="before the response completed"):
        list(r.chat("hi"))
    r = runner(tmp_path, [sse(completed([REASONING]))])
    with pytest.raises(LLMError, match="empty"):
        list(r.chat("hi"))


def test_plan_display_reaches_the_ui_event(tmp_path):
    args = {"steps": [{"step": "read", "status": "in_progress"}, {"step": "write", "status": "pending"}]}
    r = runner(tmp_path, [sse(completed([call("plan", args)])), reply("ok")], tools=default_plan_registry())
    events = list(r.chat("make a plan"))
    end = next(e for e in events if isinstance(e, ToolEnd))
    assert end.display == "[~] read\n[ ] write"
    assert "[~] read" in r.sent[1]["input"][-1]["output"]  # model gets the same text


def default_plan_registry():
    from harness.tools.plan import Plan
    return ToolRegistry([Plan()])
