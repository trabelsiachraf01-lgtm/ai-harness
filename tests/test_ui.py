import io

from rich.console import Console

from harness.runner import Text, ToolEnd, ToolUse
from harness.ui import THEME, ReplyView, _Tail, stats_line, tool_detail


def console(width=60, height=24):
    return Console(file=io.StringIO(), theme=THEME, width=width, height=height, force_terminal=False)


STATS = {
    "usage": {"input_tokens": 183, "output_tokens": 54, "reasoning_tokens": 12, "cost": 0.000046},
    "timing": {"ttft": 1.34, "total": 1.84},
    "searches": [{"query": "q", "sources": []}], "tools": [{"name": "fetch_url"}],
}


def test_stats_line_has_tokens_cost_speed_and_tool_counts():
    line = stats_line(STATS)
    assert "183 in · 54 out (12 reasoning)" in line
    assert "$0.00005" in line and "first token 1.3s" in line and "total 1.8s" in line
    assert "29 tok/s" in line and "1 searches · 1 tool calls" in line


def test_tool_detail_shows_the_argument_not_the_json():
    assert tool_detail(ToolUse("fetch_url", '{"url": "https://a.b", "start": 0}')) == "https://a.b"
    assert tool_detail(ToolUse("web_search", "python 3.14 release")) == "python 3.14 release"
    assert tool_detail(ToolUse("web_search", "2026")) == "2026"  # valid JSON but not an object


def test_reply_view_prints_tools_in_order_and_each_text_block_once():
    c = console()
    view = ReplyView(c)
    view.text("Let me ")
    view.text("check.")
    view.tool_use(ToolUse("fetch_url", '{"url": "https://a.b"}'))
    view.tool_end(ToolEnd("fetch_url", 0.7, 12_122, True))
    view.text("The answer is **42**.")
    view.flush()

    out = c.file.getvalue()
    assert out.count("Let me check.") == 1 and out.count("The answer is 42.") == 1
    assert out.index("Let me check.") < out.index("→ fetch_url") < out.index("← fetch_url") < out.index("The answer")
    assert "https://a.b" in out and "ok" in out and "12,122 chars" in out


def test_failed_tool_is_marked():
    c = console()
    ReplyView(c).tool_end(ToolEnd("fetch_url", 0.1, 5, False))
    assert "FAILED" in c.file.getvalue()


def test_tail_never_exceeds_the_screen_and_keeps_the_end():
    c = console(width=40, height=10)
    c.print(_Tail("\n\n".join(f"paragraph {i}" for i in range(50))))
    lines = c.file.getvalue().strip().splitlines()
    assert len(lines) <= 10
    assert "paragraph 49" in lines[-1]
