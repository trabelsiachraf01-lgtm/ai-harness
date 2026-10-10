import io

from rich.console import Console

from harness.runner import Text, ToolEnd, ToolUse
from harness.ui import THEME, ReplyView, _Tail, render_details, stats_line, tool_detail


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
    assert "29 tok/s" in line and "tool calls" not in line


def test_tool_detail_shows_the_argument_not_the_json():
    assert tool_detail(ToolUse("fetch_url", '{"url": "https://a.b", "start": 0}')) == "https://a.b"
    assert tool_detail(ToolUse("web_search", "python 3.14 release")) == "python 3.14 release"
    assert tool_detail(ToolUse("web_search", "2026")) == "2026"  # valid JSON but not an object


def test_tail_never_exceeds_the_screen_and_keeps_the_end():
    c = console(width=40, height=10)
    c.print(_Tail("\n\n".join(f"paragraph {i}" for i in range(50))))
    lines = c.file.getvalue().strip().splitlines()
    assert len(lines) <= 10
    assert "paragraph 49" in lines[-1]


def test_plan_checklist_prints_once_before_the_reply_text():
    c = console()
    view = ReplyView(c)
    view.tool_use(ToolUse("plan", '{"steps": []}'))
    view.tool_end(ToolEnd("plan", 0.0, 40, True, "[~] write code"))
    view.tool_use(ToolUse("plan", '{"steps": []}'))
    view.tool_end(ToolEnd("plan", 0.0, 40, True, "[x] write code"))
    view.text("Here is the answer.")
    view.finish()
    out = c.file.getvalue()
    assert out.count("plan") == 1 and out.count("[x] write code") == 1 and "[~]" not in out
    assert out.index("[x] write code") < out.index("Here is the answer.")


def test_tool_calls_are_folded_into_one_summary_line_and_kept_in_the_log():
    c = console()
    view = ReplyView(c)
    view.tool_use(ToolUse("fetch_url", '{"url": "https://a.b"}'))
    view.tool_end(ToolEnd("fetch_url", 0.7, 12_122, True))
    view.tool_use(ToolUse("fetch_url", '{"url": "https://c.d"}'))
    view.tool_end(ToolEnd("fetch_url", 0.2, 5, True))
    view.text("done")
    view.finish()
    out = c.file.getvalue()
    assert "→ fetch_url" not in out and "← fetch_url" not in out  # folded, not printed
    assert view.summary() == "▸ 2 tool calls: fetch_url ×2 · /details to expand"
    assert len(view.log) == 4


def test_details_shows_the_full_log_with_status_and_plan(capsys):
    c = console()
    render_details(c, [
        ToolUse("plan", '{"steps": [{"step": "a"}, {"step": "b"}]}'),
        ToolEnd("plan", 0.0, 9, True, "[x] a\n[ ] b"),
        ToolUse("fetch_url", '{"url": "https://a.b"}'),
        ToolEnd("fetch_url", 0.5, 3, False),
    ])
    out = c.file.getvalue()
    assert "2 items" in out and "https://a.b" in out
    assert "[x] a" in out and "FAILED" in out


def test_no_tool_calls_means_no_summary():
    view = ReplyView(console())
    view.text("hi")
    view.finish()
    assert view.summary() is None


def test_list_arguments_are_summarized_in_tool_lines():
    assert tool_detail(ToolUse("plan", '{"steps": [{"step": "a"}, {"step": "b"}]}')) == "2 items"


def test_picker_is_capped_at_a_share_of_the_terminal():
    from harness.ui import picker_max_height
    assert picker_max_height(50) == 20
    assert picker_max_height(10) == 4  # never collapses to nothing


def test_picker_rows_are_one_line_each_and_mark_the_selection(tmp_path):
    from harness.sessions import Session
    from harness.ui import picker_fragments
    s = Session.new(tmp_path, "x" * 200)
    s.append_turn([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}], {})
    rows = picker_fragments([s, s], index=1)
    text = "".join(t for _, t in rows)
    assert text.count("\n") == 1  # one line per row, no trailing blank line
    assert text.splitlines()[0].startswith("  ") and text.splitlines()[1].startswith("▸ ")
    assert all("selected" not in style for style, _ in rows)  # that class means reverse video
    assert "…" in text and "x" * 200 not in text  # long titles are cut to one line


def test_picker_rows_fit_the_terminal_width(tmp_path):
    from harness.sessions import Session
    from harness.ui import picker_fragments
    s = Session.new(tmp_path, "y" * 300)
    s.append_turn([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}], {})
    for width in (60, 80, 120):
        for _, row in picker_fragments([s], index=0, width=width):
            assert len(row.rstrip("\n")) <= width
    assert "1 turn " in "".join(t for _, t in picker_fragments([s], 0))  # singular


def _session(tmp_path):
    from harness.sessions import Session
    s = Session.new(tmp_path)
    s.append_turn([{"role": "user", "content": "FIRST QUESTION"},
                   {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "FIRST ANSWER\n\n```python\nprint('kept')\n```"}]}], {})
    s.append_turn([{"role": "user", "content": "SECOND QUESTION"},
                   {"type": "function_call", "call_id": "c1", "name": "plan", "arguments": "{}"},
                   {"type": "function_call_output", "call_id": "c1", "output": "Plan updated: 1/2 done.\n[x] read\n[~] write"},
                   {"type": "function_call", "call_id": "c2", "name": "fetch_url", "arguments": "{}"},
                   {"type": "function_call_output", "call_id": "c2", "output": "page text"},
                   {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "SECOND ANSWER"}]}], {})
    return s


def test_replay_shows_the_whole_conversation_in_order_with_code_intact(tmp_path):
    from harness.ui import replay
    c = console(width=80, height=40)
    replay(c, _session(tmp_path))
    out = c.file.getvalue()
    order = ["you ❯ FIRST QUESTION", "FIRST ANSWER", "print('kept')", "you ❯ SECOND QUESTION", "SECOND ANSWER", "resumed · 2 turns"]
    positions = [out.index(x) for x in order]
    assert positions == sorted(positions)


def test_replay_prints_the_plan_before_the_answer_and_folds_tool_calls(tmp_path):
    from harness.ui import replay
    c = console(width=80, height=40)
    replay(c, _session(tmp_path))
    out = c.file.getvalue()
    assert out.count("[x] read") == 1 and out.count("[~] write") == 1
    assert out.index("[x] read") < out.index("SECOND ANSWER")
    assert "▸ 2 tool calls: plan, fetch_url" in out
    assert "page text" not in out  # tool output is not replayed


def test_clear_screen_wipes_screen_and_scrollback_only_on_a_terminal():
    from harness.ui import clear_screen
    tty = Console(file=io.StringIO(), force_terminal=True)
    clear_screen(tty)
    assert tty.file.getvalue() == "\x1b[2J\x1b[3J\x1b[H"
    plain = console()
    clear_screen(plain)
    assert plain.file.getvalue() == ""  # never write escape codes into a pipe or file


def test_file_changes_are_never_folded_away():
    c = console()
    view = ReplyView(c)
    view.tool_use(ToolUse("edit", '{"command": "sed -i \'s/a/b/\' src/app.py"}'))
    view.tool_end(ToolEnd("edit", 0.0, 30, True, "sed -i"))
    view.tool_use(ToolUse("write", '{"command": "tee notes.md"}'))
    view.tool_end(ToolEnd("write", 0.0, 30, False))  # failed writes are not announced as done
    view.finish()
    out = c.file.getvalue()
    assert "✎ edited sed -i" in out and "notes.md" not in out


def test_replay_announces_successful_file_changes(tmp_path):
    from harness.sessions import Session
    from harness.ui import replay
    s = Session.new(tmp_path)
    s.append_turn([{"role": "user", "content": "fix it"},
                   {"type": "function_call", "call_id": "c1", "name": "edit",
                    "arguments": '{"command": "sed -i \'s/x/y/\' a.py"}'},
                   {"type": "function_call_output", "call_id": "c1", "output": "Edited a.py: 1 replacement"},
                   {"type": "function_call", "call_id": "c2", "name": "write",
                    "arguments": '{"command": "tee b.py"}'},
                   {"type": "function_call_output", "call_id": "c2", "output": "Error: b.py already exists."},
                   {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}], {})
    c = console(width=80, height=40)
    replay(c, s)
    out = c.file.getvalue()
    assert "✎ edited sed -i" in out and "b.py" not in out
