import argparse
import sys
from dataclasses import replace
from datetime import datetime

from harness.config import LLMConfig, sessions_dir
from harness.runner import LLMError, LLMRunner, Text, ToolEnd, ToolUse
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

    session = pick_session(args.name)
    runner = LLMRunner(config, session)
    print(f"\nmodel: {config.model} | /exit to quit\n")

    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line in ("/exit", "/quit"):
            return 0
        if not line:
            continue
        print("ai> ", end="", flush=True)
        at_line_start = False
        try:
            for event in runner.chat(line):
                if isinstance(event, Text):
                    print(event.text, end="", flush=True)
                    at_line_start = event.text.endswith("\n")
                    continue
                if not at_line_start:
                    print()
                at_line_start = True
                print(f"  {_describe(event)}", file=sys.stderr, flush=True)
        except KeyboardInterrupt:
            print("\n[interrupted, turn not saved]")
            continue
        except LLMError as e:
            print(f"\n[error] {e}", file=sys.stderr)
            continue
        print()
        print(f"  [{stats_line(runner.last_stats)}]", file=sys.stderr)


def _describe(event: ToolUse | ToolEnd) -> str:
    if isinstance(event, ToolUse):
        return f"→ {event.name} {event.detail}"
    status = "ok" if event.ok else "FAILED"
    return f"← {event.name} {status} · {event.seconds:.1f}s · {event.chars:,} chars"


def stats_line(stats: dict | None) -> str:
    if not stats:
        return ""
    u, t = stats["usage"], stats["timing"]
    reasoning = f" ({u['reasoning_tokens']} reasoning)"
    parts = [f"{u['input_tokens']} in / {u['output_tokens']} out{reasoning}", f"${u['cost']:.5f}"]
    if t["ttft"] is not None:
        parts.append(f"first token {t['ttft']:.2f}s")
    parts.append(f"total {t['total']:.2f}s")
    if u["output_tokens"] and t["total"] > 0:
        # output tokens (incl. reasoning) over the whole turn, so tool and thinking time count against speed
        parts.append(f"{u['output_tokens'] / t['total']:.0f} tok/s")
    if stats["steps"] > 1 or stats["searches"]:
        parts.append(f"{stats['steps']} model calls, {len(stats['searches'])} searches, {len(stats['tools'])} tool calls")
    return " · ".join(parts)


def pick_session(name: str | None) -> Session:
    root = sessions_dir()
    existing = Session.list(root) if root.exists() else []
    if not existing:
        return Session.new(root, name)

    print("Sessions:")
    for i, s in enumerate(existing, 1):
        when = datetime.fromtimestamp(s.path.stat().st_mtime).strftime("%b %d %H:%M")
        print(f"  {i}) {s.title}  ({s.turns} turns, {when})")
    while True:
        choice = input("Resume which? (number, or Enter for new): ").strip()
        if not choice:
            return Session.new(root, name)
        if choice.isdigit() and 1 <= int(choice) <= len(existing):
            session = existing[int(choice) - 1]
            for role, text in _recent(session):
                print(f"  {role}: {text[:300]}")
            return session


def _recent(session: Session) -> list[tuple[str, str]]:
    """The last user message and assistant reply, for context after resuming."""
    def text(item: dict) -> str:
        content = item["content"]
        return content if isinstance(content, str) else "".join(p.get("text", "") for p in content)

    messages = [(i["role"], text(i)) for i in session.items if i.get("role") in ("user", "assistant")]
    return messages[-2:]
