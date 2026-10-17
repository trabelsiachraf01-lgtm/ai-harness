"""One JSONL file per conversation.

Line 1 is a header {"title", "created_at"}. Every later line is either:
  * an *item*: exactly what gets sent back to the model as `input` next turn (user and
    assistant messages, function calls and their outputs, reasoning), or
  * a {"type": "turn_stats", ...} record (usage, cost, timing, searches) for later analysis.

A whole turn (all its items plus one stats record) is written in a single append, so a
crash never leaves a half-finished tool exchange in the history.
"""

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

STATS = "turn_stats"
CLEAR = "context_clear"  # marks where the model's context restarts; earlier lines stay on disk


@dataclass
class Session:
    path: Path
    title: str | None = None
    items: list[dict] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.path.stem

    @property
    def turns(self) -> int:
        return sum(1 for i in self.items if i.get("role") == "user")

    @classmethod
    def new(cls, root: Path, title: str | None = None) -> "Session":
        # The file is created lazily on the first turn, so quitting early leaves nothing behind.
        return cls(root / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}.jsonl", title)

    @classmethod
    def load(cls, path: Path) -> "Session":
        text = path.read_text()
        if text and not text.endswith("\n"):  # torn final write: drop the partial line
            text = text[: text.rfind("\n") + 1]
            path.write_text(text)
        header, *lines = text.splitlines()
        items: list[dict] = []
        for record in (json.loads(line) for line in lines):
            if record.get("type") == CLEAR:
                items = []  # everything before the marker is history, not context
            elif record.get("type") != STATS:
                items.append(_normalize(record))
        return cls(path, json.loads(header)["title"], items)

    @classmethod
    def list(cls, root: Path) -> list["Session"]:
        """Newest first. Unreadable files are skipped."""
        sessions = []
        for path in sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                sessions.append(cls.load(path))
            except (ValueError, KeyError):
                continue
        return sessions

    def clear(self) -> None:
        """Restart the model's context. The transcript stays on disk; usage stats are kept."""
        if self.path.exists():
            with self.path.open("a") as f:
                f.write(json.dumps({"type": CLEAR, "at": datetime.now().isoformat(timespec="seconds")}) + "\n")
                f.flush()
                os.fsync(f.fileno())
        self.items = []

    def append_turn(self, items: list[dict], stats: dict) -> None:
        lines: list[dict] = []
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            first = items[0]["content"]
            self.title = self.title or first.strip().replace("\n", " ")[:60]
            lines.append({"title": self.title, "created_at": datetime.now().isoformat(timespec="seconds")})
        lines += [*items, {"type": STATS, **stats}]
        with self.path.open("a") as f:
            f.write("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines))
            f.flush()
            os.fsync(f.fileno())
        self.items += items


def _normalize(record: dict) -> dict:
    # Sessions from before tools stored {"role", "content", "model", "usage", ...} per message.
    if "type" not in record:
        return {k: record[k] for k in ("role", "content", "images") if k in record}
    return record
