import json

from harness.sessions import Session

U = {"role": "user", "content": "hello there"}
A = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}
STATS = {"model": "m", "usage": {"cost": 0.1}}


def test_file_is_created_lazily_and_titled_from_first_message(tmp_path):
    s = Session.new(tmp_path)
    assert not s.path.exists()
    s.append_turn([U, A], STATS)
    assert Session.load(s.path).title == "hello there"


def test_explicit_name_wins(tmp_path):
    s = Session.new(tmp_path, "my project")
    s.append_turn([U, A], STATS)
    assert Session.load(s.path).title == "my project"


def test_items_roundtrip_stats_are_kept_on_disk_but_not_replayed(tmp_path):
    s = Session.new(tmp_path)
    call = {"type": "function_call", "call_id": "c1", "name": "t", "arguments": "{}"}
    out = {"type": "function_call_output", "call_id": "c1", "output": "x"}
    s.append_turn([U, call, out, A], STATS)

    loaded = Session.load(s.path)
    assert loaded.items == [U, call, out, A]
    assert loaded.turns == 1
    assert json.loads(s.path.read_text().splitlines()[-1]) == {"type": "turn_stats", **STATS}

    loaded.append_turn([U, A], STATS)
    assert len(Session.load(s.path).items) == 6


def test_torn_write_is_dropped_and_file_stays_appendable(tmp_path):
    s = Session.new(tmp_path)
    s.append_turn([U, A], STATS)
    with s.path.open("a") as f:
        f.write('{"role": "user", "content": "cut o')  # crash mid-write
    loaded = Session.load(s.path)
    assert loaded.items == [U, A]
    loaded.append_turn([U, A], STATS)
    assert len(Session.load(s.path).items) == 4


def test_legacy_message_lines_are_normalized(tmp_path):
    s = Session.new(tmp_path)
    s.path.write_text(
        json.dumps({"title": "old", "created_at": "x"}) + "\n"
        + json.dumps({"role": "user", "content": "q"}) + "\n"
        + json.dumps({"role": "assistant", "content": "a", "model": "m", "usage": {}, "timing": {}}) + "\n"
    )
    assert Session.load(s.path).items == [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]


def test_list_is_newest_first_and_skips_garbage(tmp_path):
    a, b = Session.new(tmp_path, "a"), Session.new(tmp_path, "b")
    a.append_turn([U, A], STATS)
    b.append_turn([U, A], STATS)
    (tmp_path / "junk.jsonl").write_text("not json\n")
    assert [s.title for s in Session.list(tmp_path)] == ["b", "a"]


def test_clear_restarts_context_but_keeps_the_transcript(tmp_path):
    s = Session.new(tmp_path)
    s.append_turn([U, A], STATS)
    s.clear()
    assert s.items == [] and s.turns == 0
    assert Session.load(s.path).items == []  # the reset survives a restart

    s.append_turn([{"role": "user", "content": "again"}, A], STATS)
    loaded = Session.load(s.path)
    assert [i.get("content") for i in loaded.items][0] == "again"
    assert loaded.turns == 1
    assert '"hello there"' in s.path.read_text()  # history is still on disk
    assert loaded.title == "hello there"  # title is kept


def test_clear_on_a_new_session_writes_nothing(tmp_path):
    s = Session.new(tmp_path)
    s.clear()
    assert not s.path.exists()
