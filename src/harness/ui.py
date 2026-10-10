"""Terminal UI: palette, streamed Markdown replies, tool activity, prompt, session picker.

Inline rather than full-screen, so the conversation stays in the terminal's own scrollback.
The runner yields events; everything visual lives here.
"""

import json
from datetime import datetime
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style as PromptStyle
from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.markdown import Markdown
from rich.segment import Segment
from rich.table import Table
from rich.text import Text as RichText
from rich.theme import Theme

from harness.runner import LLMError, LLMRunner, Text, ToolEnd, ToolUse
from harness.sessions import Session

# One place to retune the look. Hex colors; Rich downgrades them on 256-color terminals.
PALETTE = {
    "accent": "#7dcfff",  # prompt, headings, links
    "tool": "#e0af68",  # tool calls
    "ok": "#9ece6a",
    "error": "#f7768e",
    "muted": "#7982a9",  # stats, hints, previews
    "toolbar_bg": "#24283b",
}
CODE_THEME = "monokai"  # any Pygments style name

THEME = Theme({
    **{k: v for k, v in PALETTE.items() if k != "toolbar_bg"},
    "markdown.h1": f"bold {PALETTE['accent']}",
    "markdown.h2": f"bold {PALETTE['accent']}",
    "markdown.h3": PALETTE["accent"],
    "markdown.link": PALETTE["accent"],
    "markdown.link_url": f"underline {PALETTE['muted']}",
    "markdown.code": PALETTE["tool"],
    "markdown.item.bullet": PALETTE["accent"],
    "markdown.item.number": PALETTE["accent"],
})

COMMANDS = {"/new": "start a new session", "/sessions": "switch session", "/help": "show commands", "/exit": "quit"}


def make_console() -> Console:
    return Console(theme=THEME, highlight=False)


# ---- replies --------------------------------------------------------------------------------

class _Tail:
    """The last lines of a Markdown document, so the live view never outgrows the screen
    (Rich cannot redraw a live region taller than the terminal)."""

    def __init__(self, source: str):
        self.source = source

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        lines = console.render_lines(Markdown(self.source, code_theme=CODE_THEME), options, pad=False)
        for line in lines[-max(console.height - 3, 5):]:
            yield from line
            yield Segment.line()


class ReplyView:
    """Shows a spinner while waiting, the reply as it streams, and one line per tool call.

    While streaming, a transient live view shows the tail of the reply; when a text block
    ends it is replaced by one final full Markdown render.
    """

    def __init__(self, console: Console):
        self.console = console
        self._buffer = ""
        self._live: Live | None = None
        self._status = None

    def waiting(self, label: str = "thinking") -> None:
        if self._status is None and self._live is None:
            self._status = self.console.status(f"[muted]{label}…[/]", spinner="dots", spinner_style="accent")
            self._status.start()

    def text(self, chunk: str) -> None:
        self._stop_status()
        if self._live is None:
            self._live = Live(_Tail(""), console=self.console, transient=True, refresh_per_second=10)
            self._live.start()
        self._buffer += chunk
        self._live.update(_Tail(self._buffer))

    def tool_use(self, event: ToolUse) -> None:
        self.flush()
        self.console.print(RichText.assemble(
            ("  → ", "tool"), (event.name, "bold tool"), ("  " + tool_detail(event), "muted")))
        self.waiting("working")

    def tool_end(self, event: ToolEnd) -> None:
        self._stop_status()
        status = ("ok", "ok") if event.ok else ("FAILED", "error")
        self.console.print(RichText.assemble(
            ("  ← ", "tool"), (event.name, "bold tool"), ("  ", ""), status,
            (f"  {event.seconds:.1f}s · {event.chars:,} chars", "muted")))
        self.waiting("thinking")

    def flush(self) -> None:
        """Finish the current text block: replace the live tail with the final render."""
        self._stop_status()
        if self._live is not None:
            self._live.stop()
            self._live = None
            self.console.print(Markdown(self._buffer, code_theme=CODE_THEME))
        self._buffer = ""

    def close(self) -> None:
        """Abandon whatever is on screen (interrupted or failed turn)."""
        self._stop_status()
        if self._live is not None:
            self._live.stop()
            self._live = None
        self._buffer = ""

    def _stop_status(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None


def stream_reply(console: Console, runner: LLMRunner, message: str) -> None:
    console.print()
    view = ReplyView(console)
    view.waiting()
    try:
        for event in runner.chat(message):
            if isinstance(event, Text):
                view.text(event.text)
            elif isinstance(event, ToolUse):
                view.tool_use(event)
            elif isinstance(event, ToolEnd):
                view.tool_end(event)
        view.flush()
    except KeyboardInterrupt:
        view.close()
        console.print("[error]interrupted · turn not saved[/]")
    except LLMError as e:
        view.close()
        console.print(RichText(f"error: {e}", style="error"))
    else:
        console.print(RichText("  " + stats_line(runner.last_stats), style="muted"))


def tool_detail(event: ToolUse) -> str:
    """The interesting argument of a tool call, not the raw JSON."""
    try:
        args = json.loads(event.detail)
    except ValueError:
        return event.detail  # server-side search: detail is already the query
    if isinstance(args, dict) and args:
        return str(next(iter(args.values())))
    return event.detail


def stats_line(stats: dict | None) -> str:
    if not stats:
        return ""
    u, t = stats["usage"], stats["timing"]
    parts = [f"{u['input_tokens']} in · {u['output_tokens']} out ({u['reasoning_tokens']} reasoning)", f"${u['cost']:.5f}"]
    if t["ttft"] is not None:
        parts.append(f"first token {t['ttft']:.1f}s")
    parts.append(f"total {t['total']:.1f}s")
    if u["output_tokens"] and t["total"] > 0:
        # output tokens (incl. reasoning) over the whole turn, so tool and thinking time count against speed
        parts.append(f"{u['output_tokens'] / t['total']:.0f} tok/s")
    if stats["searches"] or stats["tools"]:
        parts.append(f"{len(stats['searches'])} searches · {len(stats['tools'])} tool calls")
    return " · ".join(parts)


# ---- prompt ---------------------------------------------------------------------------------

def make_prompt(history_path: Path, toolbar) -> PromptSession:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    keys = KeyBindings()

    @keys.add("escape", "enter")  # Alt+Enter
    @keys.add("c-j")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    style = PromptStyle.from_dict({
        "prompt": f"bold {PALETTE['accent']}",
        "bottom-toolbar": f"noreverse bg:{PALETTE['toolbar_bg']} {PALETTE['muted']}",
    })
    return PromptSession(
        message=[("class:prompt", "you ❯ ")],
        history=FileHistory(str(history_path)),
        completer=WordCompleter(list(COMMANDS), meta_dict=COMMANDS, sentence=True),
        complete_while_typing=True,
        key_bindings=keys,
        bottom_toolbar=toolbar,
        style=style,
        enable_history_search=True,
    )


# ---- sessions and banners -------------------------------------------------------------------

def choose_session(console: Console, root: Path, name: str | None) -> Session:
    existing = Session.list(root) if root.exists() else []
    if not existing:
        return Session.new(root, name)

    table = Table(box=None, header_style="muted", padding=(0, 2))
    for column in ("#", "session", "turns", "last used"):
        table.add_column(column)
    for i, s in enumerate(existing, 1):
        when = datetime.fromtimestamp(s.path.stat().st_mtime).strftime("%b %d %H:%M")
        table.add_row(RichText(str(i), style="accent"), s.title or "(untitled)", str(s.turns), RichText(when, style="muted"))
    console.print(table)
    while True:
        choice = console.input("[muted]resume which? number, or Enter for a new session ❯ [/]").strip()
        if not choice:
            return Session.new(root, name)
        if choice.isdigit() and 1 <= int(choice) <= len(existing):
            return existing[int(choice) - 1]


def banner(console: Console, runner: LLMRunner) -> None:
    session = runner.session
    console.print()
    console.print(RichText.assemble(("ai-harness", "bold accent"), (f"  {runner.config.model}", "muted")))
    console.print(RichText("Enter sends · Alt+Enter newline · /help for commands", style="muted"))
    for role, text in _recent(session):
        console.print(RichText(f"  {role}: {text[:200]}", style="muted"))
    console.print()


def help_text(console: Console) -> None:
    for command, meaning in COMMANDS.items():
        console.print(RichText.assemble((f"  {command:<10}", "accent"), (meaning, "muted")))


def _recent(session: Session) -> list[tuple[str, str]]:
    """The last user message and assistant reply, for context after resuming."""
    def text(item: dict) -> str:
        content = item["content"]
        return content if isinstance(content, str) else "".join(p.get("text", "") for p in content)

    messages = [(i["role"], text(i)) for i in session.items if i.get("role") in ("user", "assistant")]
    return [(role, " ".join(body.split())) for role, body in messages[-2:]]
