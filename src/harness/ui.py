"""Terminal UI: palette, streamed Markdown replies, tool activity, prompt, session picker.

Inline rather than full-screen, so the conversation stays in the terminal's own scrollback.
The runner yields events; everything visual lives here.
"""

import json
import shutil
import sys
import termios
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from prompt_toolkit import PromptSession, prompt
from prompt_toolkit.application import Application
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.data_structures import Point
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style as PromptStyle
from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.segment import Segment
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text as RichText
from rich.theme import Theme

from harness.permissions import Decision, Permissions, Request
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

FILE_CHANGES = {"write_file": "wrote", "edit_file": "edited"}  # always shown, never folded away

COMMANDS = {"/new": "start a new session", "/sessions": "switch session", "/details": "show tool calls of the last reply", "/clear": "restart this session's context", "/permissions": "list or revoke allow rules", "/help": "show commands", "/exit": "quit"}


def make_console() -> Console:
    return Console(theme=THEME, highlight=False)


# ---- replies --------------------------------------------------------------------------------

class _Tail:
    """The last lines of a Markdown document, so the live view never outgrows the screen
    (Rich cannot redraw a live region taller than the terminal)."""

    def __init__(self, source: str, max_lines: int | None = None):
        self.source = source
        self.max_lines = max_lines

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        lines = console.render_lines(Markdown(self.source, code_theme=CODE_THEME), options, pad=False)
        budget = self.max_lines or max(console.height - 3, 5)
        for line in lines[-budget:]:
            yield from line
            yield Segment.line()


def _checklist(display: str, indent: str = "    ") -> RichText:
    text = RichText()
    for i, line in enumerate(display.splitlines()):
        style = {"[x]": "ok", "[~]": "accent"}.get(line[:3], "muted")
        text.append(("\n" if i else "") + indent + line, style=style)
    return text


class ReplyView:
    """One reply, drawn in three parts:
    - a live region (spinner, or the streaming reply) that updates in place;
    - the plan, a checklist printed once per change, just before the reply text;
    - tool calls, kept in `log` and folded into one summary line (see /details).
    """

    def __init__(self, console: Console):
        self.console = console
        self.log: list[ToolUse | ToolEnd] = []
        self._buffer = ""
        self._live: Live | None = None
        self._plan: str | None = None
        self._plan_dirty = False
        self._last_use: ToolUse | None = None

    def waiting(self, label: str = "thinking") -> None:
        spinner = Spinner("dots", text=RichText(f"{label}…", style="muted"), style="accent")
        self._show(Group(spinner, _checklist(self._plan)) if self._plan else spinner)

    def text(self, chunk: str) -> None:
        self._buffer += chunk
        self._show(_Tail(self._buffer))

    def tool_use(self, event: ToolUse) -> None:
        self.flush_text()
        self.log.append(event)
        self._last_use = event
        self.waiting(event.name)

    def tool_end(self, event: ToolEnd) -> None:
        self.log.append(event)
        if event.name == "plan" and event.ok and event.display:
            self._plan, self._plan_dirty = event.display, True
        if event.name in FILE_CHANGES and event.ok and self._last_use is not None:
            self.console.print(file_change_line(event.name, tool_detail(self._last_use)))
        self.waiting("thinking")

    def pause(self) -> None:
        """Stop the spinner so a prompt can use the terminal."""
        self._stop()

    def flush_text(self) -> None:
        """End the current text block: print a changed plan, then the reply as Markdown."""
        self._stop()
        if self._buffer:
            if self._plan_dirty:
                self._print_plan()
            self.console.print(Markdown(self._buffer, code_theme=CODE_THEME))
            self._buffer = ""

    def finish(self) -> None:
        self.flush_text()
        if self._plan_dirty:
            self._print_plan()

    def close(self) -> None:
        """Abandon what is on screen (interrupted or failed turn)."""
        self._stop()
        self._buffer = ""

    def summary(self) -> str | None:
        counts = Counter(e.name for e in self.log if isinstance(e, ToolUse))
        if not counts:
            return None
        names = ", ".join(f"{name} ×{n}" if n > 1 else name for name, n in counts.items())
        total = sum(counts.values())
        return f"▸ {total} tool call{'s' if total != 1 else ''}: {names} · /details to expand"

    def _print_plan(self) -> None:
        self.console.print(RichText("  plan", style="bold accent"))
        self.console.print(_checklist(self._plan))
        self._plan_dirty = False

    def _show(self, renderable) -> None:
        if self._live is None:
            self._live = Live(renderable, console=self.console, transient=True, refresh_per_second=10)
            self._live.start()
        else:
            self._live.update(renderable)

    def _stop(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None


APPROVAL_KEYS = "osSaA"
PERMISSION_LABELS = {"once": "allowed once", "session": "allowed (session rule)",
                     "always": "allowed (saved rule)", "denied": "denied"}


def _drain_input(settle: float = 0.3) -> None:
    """Throw away keys typed before the prompt appeared. Someone typing their next message while
    the model works must not answer an approval by accident (`s` and `o` are approvals)."""
    if not sys.stdin.isatty():
        return
    termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    time.sleep(settle)  # keys still arriving as the prompt opens
    termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)


def _ask_key() -> str:
    """One keypress. Anything unrecognised, including Enter, means deny."""
    _drain_input()
    keys = KeyBindings()
    for key in APPROVAL_KEYS:
        keys.add(key)(lambda event, key=key: event.app.exit(result=key))
    for key in ("d", "n", "escape", "c-c"):
        keys.add(key)(lambda event: event.app.exit(result="d"))
    session = PromptSession(
        message=[("class:prompt", " choose ❯ ")], key_bindings=keys, erase_when_done=True,
        style=PromptStyle.from_dict({"prompt": PALETTE["muted"]}),
    )
    try:
        return session.prompt()
    except (KeyboardInterrupt, EOFError):
        return "d"


class TerminalApprover:
    """Asks the person at the terminal. Every call shows exactly what will run."""

    def __init__(self, console: Console):
        self.console = console
        self.view: ReplyView | None = None  # set by stream_reply so the spinner can step aside

    def __call__(self, request: Request) -> Decision:
        if self.view is not None:
            self.view.pause()
        rule = f"{request.tool}:{request.scope}" if request.scope else request.tool
        self.console.print(Panel(
            RichText(request.detail), title=RichText.assemble((request.tool, "bold tool"), (" wants to run", "muted")),
            title_align="left", border_style="tool", padding=(0, 1)))
        self.console.print(RichText.assemble(
            ("  [o]", "accent"), (" once   ", "muted"), ("[s]", "accent"), (f" this session ({rule})   ", "muted"),
            ("[a]", "accent"), (f" always ({rule})   ", "muted"), ("[d]", "accent"), (" deny   ", "muted"),
            ("S/A = whole tool", "muted")))
        decision = _decision_for(_ask_key(), self._ask_reason)
        if self.view is not None and decision.allow:
            self.view.waiting(request.tool)
        return decision

    @staticmethod
    def _ask_reason() -> str | None:
        try:
            return prompt("  reason for denying (optional, Enter to skip) ❯ ").strip() or None
        except (KeyboardInterrupt, EOFError):
            return None


def _decision_for(key: str, ask_reason) -> Decision:
    if key == "o":
        return Decision(True)
    if key in ("s", "S"):
        return Decision(True, "session", whole_tool=key == "S")
    if key in ("a", "A"):
        return Decision(True, "always", whole_tool=key == "A")
    return Decision(False, reason=ask_reason())


def permissions_text(console: Console, permissions: Permissions) -> None:
    if not permissions.session and not permissions.always:
        console.print(RichText("  no allow rules: every call asks", style="muted"))
        return
    for label, rules in (("this session", permissions.session), ("always", permissions.always)):
        for rule in sorted(rules):
            console.print(RichText.assemble(("  ", ""), (rule, "accent"), (f"  {label}", "muted")))
    console.print(RichText("  /permissions revoke <rule> removes one", style="muted"))


def stream_reply(console: Console, runner: LLMRunner, message: str,
                 approver: "TerminalApprover | None" = None) -> list:
    """Draw one turn. Returns the tool log of this turn, for /details."""
    console.print()
    view = ReplyView(console)
    if approver is not None:
        approver.view = view
    view.waiting()
    try:
        for event in runner.chat(message):
            if isinstance(event, Text):
                view.text(event.text)
            elif isinstance(event, ToolUse):
                view.tool_use(event)
            elif isinstance(event, ToolEnd):
                view.tool_end(event)
        view.finish()
    except KeyboardInterrupt:
        view.close()
        console.print("[error]interrupted · turn not saved[/]")
    except LLMError as e:
        view.close()
        console.print(RichText(f"error: {e}", style="error"))
    else:
        if summary := view.summary():
            console.print(RichText("  " + summary, style="muted"))
        console.print(RichText("  " + stats_line(runner.last_stats), style="muted"))
    finally:
        if approver is not None:
            approver.view = None
    return view.log


def render_details(console: Console, log: list) -> None:
    """The full tool log of the last reply, as it happened."""
    if not log:
        console.print(RichText("  no tool calls in the last reply", style="muted"))
        return
    for event in log:
        if isinstance(event, ToolUse):
            console.print(RichText.assemble(("  → ", "tool"), (event.name, "bold tool"), ("  " + tool_detail(event), "muted")))
            continue
        status = ("ok", "ok") if event.ok else ("FAILED", "error")
        console.print(RichText.assemble(
            ("  ← ", "tool"), (event.name, "bold tool"), ("  ", ""), status,
            (f"  {event.seconds:.1f}s · {event.chars:,} chars", "muted"),
            (f" · {PERMISSION_LABELS[event.permission]}" if event.permission else "", "muted")))
        if event.display:
            console.print(_checklist(event.display))


def file_change_line(tool: str, path: str) -> RichText:
    return RichText.assemble(("  ✎ ", "tool"), (f"{FILE_CHANGES[tool]} ", "muted"), (path, "bold"))


def tool_detail(event: ToolUse) -> str:
    """The interesting argument of a tool call, not the raw JSON."""
    try:
        args = json.loads(event.detail)
    except ValueError:
        return event.detail  # server-side search: detail is already the query
    if isinstance(args, dict) and args:
        value = next(iter(args.values()))
        if isinstance(value, list):
            return f"{len(value)} items"
        return str(value)
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

PICKER_SHARE = 0.4  # the session picker never takes more than this share of the terminal height


def picker_max_height(terminal_lines: int) -> int:
    return max(4, int(terminal_lines * PICKER_SHARE))


def picker_fragments(sessions: list[Session], index: int, width: int = 80) -> list:
    """One line per session, fitted to the terminal width: the title takes the room that is left."""
    title_width = max(16, width - 30)  # marker, turn count, date and margins
    rows = []
    for i, s in enumerate(sessions):
        title = " ".join((s.title or "(untitled)").split())
        if len(title) > title_width:
            title = title[: title_width - 1] + "…"
        when = datetime.fromtimestamp(s.path.stat().st_mtime).strftime("%b %d %H:%M")
        turns = f"{s.turns} turn{'s' if s.turns != 1 else ''}"
        selected = i == index
        marker = "▸ " if selected else "  "
        rows.append(("class:current" if selected else "class:row",
                     f"{marker}{title:<{title_width}} {turns:>8}  {when}" + ("\n" if i < len(sessions) - 1 else "")))
    return rows


def choose_session(console: Console, root: Path, name: str | None) -> Session:
    """Inline picker. Arrows / PageUp / PageDown move, Enter opens, n or Esc starts a new session.
    The list scrolls inside a window capped at PICKER_SHARE of the terminal height."""
    sessions = Session.list(root) if root.exists() else []
    if not sessions:
        return Session.new(root, name)

    index = 0
    width = shutil.get_terminal_size().columns
    body = Window(
        content=FormattedTextControl(lambda: picker_fragments(sessions, index, width),
                                     get_cursor_position=lambda: Point(0, index)),
        height=Dimension(min=1, preferred=len(sessions), max=picker_max_height(shutil.get_terminal_size().lines)),
    )
    hint = Window(height=1, content=FormattedTextControl(
        [("class:hint", f" {len(sessions)} sessions · ↑/↓ move · PgUp/PgDn page · Enter open · n new")]))

    keys = KeyBindings()

    @keys.add("up")
    def _up(event):
        nonlocal index
        index = max(0, index - 1)

    @keys.add("down")
    def _down(event):
        nonlocal index
        index = min(len(sessions) - 1, index + 1)

    @keys.add("pageup")
    def _pgup(event):
        nonlocal index
        index = max(0, index - 5)

    @keys.add("pagedown")
    def _pgdn(event):
        nonlocal index
        index = min(len(sessions) - 1, index + 5)

    @keys.add("enter")
    def _open(event):
        event.app.exit(result=sessions[index])

    @keys.add("n")
    @keys.add("escape")
    @keys.add("c-c")
    def _new(event):
        event.app.exit(result=None)

    app = Application(
        layout=Layout(HSplit([body, hint])),
        key_bindings=keys,
        full_screen=False,
        erase_when_done=True,  # the list disappears once you choose
        style=PromptStyle.from_dict({
            "current": f"bold {PALETTE['accent']}", "row": "", "hint": PALETTE["muted"],
        }),
    )
    chosen = app.run()
    return chosen if chosen is not None else Session.new(root, name)


def replay(console: Console, session: Session) -> None:
    """Draw the saved conversation the way it looked live: each question, the plan checklist,
    the reply as Markdown, and the folded tool-call summary."""
    items = session.items
    names = {i["call_id"]: i["name"] for i in items if i.get("type") == "function_call"}
    arguments = {i["call_id"]: i["arguments"] for i in items if i.get("type") == "function_call"}
    tool_counts: Counter = Counter()
    plan: str | None = None  # a checklist waiting to be printed, as in the live view

    def flush_tools() -> None:
        if tool_counts:
            total = sum(tool_counts.values())
            listing = ", ".join(f"{n} ×{c}" if c > 1 else n for n, c in tool_counts.items())
            console.print(RichText(f"  ▸ {total} tool call{'s' if total != 1 else ''}: {listing}", style="muted"))
            tool_counts.clear()

    def flush_plan() -> None:
        nonlocal plan
        if plan:
            console.print(RichText("  plan", style="bold accent"))
            console.print(_checklist(plan))
            plan = None

    for item in items:
        if item.get("role") == "user":
            flush_plan()
            flush_tools()
            console.print()
            console.print(RichText.assemble(("you ❯ ", "bold accent"), (_text(item), "")))
        elif item.get("type") == "function_call":
            tool_counts[item["name"]] += 1
        elif (item.get("type") == "function_call_output" and names.get(item["call_id"]) in FILE_CHANGES
              and not item["output"].startswith("Error:")):
            path = tool_detail(ToolUse(names[item["call_id"]], arguments[item["call_id"]]))
            console.print(file_change_line(names[item["call_id"]], path))
        elif item.get("type") == "function_call_output" and names.get(item["call_id"]) == "plan":
            lines = [l for l in item["output"].splitlines() if l[:3] in ("[ ]", "[~]", "[x]")]
            plan = "\n".join(lines) or plan
        elif item.get("role") == "assistant":
            flush_plan()
            console.print()
            console.print(Markdown(_text(item), code_theme=CODE_THEME))
    flush_plan()
    flush_tools()
    turns = session.turns
    console.print()
    console.print(RichText(f"── resumed · {turns} turn{'s' if turns != 1 else ''} ──", style="muted"))


def clear_screen(console: Console) -> None:
    """Wipe the visible screen and the scrollback, like `clear` does."""
    if console.is_terminal:
        console.file.write("\x1b[2J\x1b[3J\x1b[H")
        console.file.flush()


def banner(console: Console, runner: LLMRunner) -> None:
    console.print()
    console.print(RichText.assemble(("ai-harness", "bold accent"), (f"  {runner.config.model}", "muted")))
    console.print(RichText("Enter sends · Alt+Enter newline · /help for commands", style="muted"))
    if runner.session.items:
        replay(console, runner.session)
    console.print()


def help_text(console: Console) -> None:
    for command, meaning in COMMANDS.items():
        console.print(RichText.assemble((f"  {command:<10}", "accent"), (meaning, "muted")))


def _text(item: dict) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content or [])
