import argparse
import sys
from dataclasses import replace

from rich.text import Text as RichText

from harness import ui
from harness.config import LLMConfig, sessions_dir
from harness.runner import LLMRunner
from harness.sessions import Session


def main() -> int:
    parser = argparse.ArgumentParser(prog="harness")
    parser.add_argument("--model", help="override the model for this run")
    parser.add_argument("--name", help="title for a new session (default: first message)")
    args = parser.parse_args()

    config = LLMConfig.from_env()
    if args.model:
        config = replace(config, model=args.model)
    if not config.api_key:
        print("OPENROUTER_API_KEY is not set", file=sys.stderr)
        return 1

    root = sessions_dir()
    console = ui.make_console()
    runner = LLMRunner(config, ui.choose_session(console, root, args.name))
    ui.banner(console, runner)
    last_log: list = []

    prompt = ui.make_prompt(
        root.parent / "history",
        toolbar=lambda: f" {runner.config.model} · {runner.session.title or 'new session'} · {runner.session.turns} turns ",
    )
    while True:
        try:
            line = prompt.prompt().strip()
        except KeyboardInterrupt:  # Ctrl-C clears the line, Ctrl-D exits
            continue
        except EOFError:
            return 0
        if not line:
            continue
        if not line.startswith("/"):
            last_log = ui.stream_reply(console, runner, line)
            continue

        command = line.split()[0]
        if command in ("/exit", "/quit"):
            return 0
        if command == "/new":
            runner, last_log = LLMRunner(config, Session.new(root)), []
            ui.clear_screen(console)
            ui.banner(console, runner)
        elif command == "/sessions":
            runner, last_log = LLMRunner(config, ui.choose_session(console, root, None)), []
            ui.clear_screen(console)
            ui.banner(console, runner)
        elif command == "/clear":
            runner.session.clear()
            last_log = []
            ui.clear_screen(console)
            ui.banner(console, runner)
            console.print(RichText("  context cleared · the transcript is still in the session file", style="muted"))
        elif command == "/details":
            ui.render_details(console, last_log)
        elif command == "/help":
            ui.help_text(console)
        else:
            console.print(f"[error]unknown command {command}[/] [muted](try /help)[/]")
