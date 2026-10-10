import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date

import httpx

from harness.config import LLMConfig
from harness.prompts import SYSTEM_PROMPT
from harness.sessions import Session
from harness.tools import ToolRegistry, default_registry

SERVER_SEARCH_ITEMS = {"web_search_call", "openrouter:web_search"}


class LLMError(Exception):
    pass


@dataclass
class Text:
    text: str


@dataclass
class ToolUse:  # a tool is starting (client tool) or has just run (server-side search)
    name: str
    detail: str


@dataclass
class ToolEnd:  # a client tool finished
    name: str
    seconds: float
    chars: int
    ok: bool


@dataclass
class _Final:  # internal: the completed response, last event of _stream
    response: dict


class LLMRunner:
    """Multi-turn streaming chat with tools, over one session.

    `chat()` yields Text / ToolUse / ToolEnd events as they happen. A turn can span several
    model calls (model -> tool -> model ...). It is saved only once the whole turn completes,
    so an interrupted or failed turn leaves the session unchanged.

    Extension points: override `_stream` to change provider; add tools through the registry.
    """

    def __init__(
        self,
        config: LLMConfig,
        session: Session,
        system_prompt: str = SYSTEM_PROMPT,
        tools: ToolRegistry | None = None,
        max_steps: int = 8,
        http: httpx.Client | None = None,
    ):
        self.config = config
        self.session = session
        self.system_prompt = system_prompt
        self.tools = default_registry() if tools is None else tools
        self.max_steps = max_steps
        self.last_stats: dict | None = None
        self._http = http or httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout,
            headers={"Authorization": f"Bearer {config.api_key}", "X-Title": "ai-harness"},
        )

    def chat(self, user_message: str) -> Iterator[Text | ToolUse | ToolEnd]:
        self.last_stats = None
        turn = [{"role": "user", "content": user_message}]  # everything this turn adds to the session
        stats = {"model": None, "steps": 0, "searches": [], "tools": [],
                 "usage": {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "cost": 0.0}}
        start = time.perf_counter()
        first_token: float | None = None

        for _ in range(self.max_steps):
            response = None
            for event in self._stream(self._instructions(), [*self.session.items, *turn]):
                if isinstance(event, _Final):
                    response = event.response
                    continue
                if isinstance(event, Text) and first_token is None:
                    first_token = time.perf_counter() - start
                yield event
            output = response["output"]
            _tally(stats, response)
            turn += [kept for item in output if (kept := _persistable(item))]

            calls = [item for item in output if item["type"] == "function_call"]
            if not calls:
                if not _text_of(output):
                    raise LLMError("model returned an empty reply")
                break
            for call in calls:
                yield ToolUse(call["name"], _short(call["arguments"]))
                t0 = time.perf_counter()
                result = self.tools.run(call["name"], call["arguments"])
                seconds = time.perf_counter() - t0
                yield ToolEnd(call["name"], seconds, len(result.output), result.ok)
                stats["tools"].append({"name": call["name"], "seconds": round(seconds, 3), "ok": result.ok})
                turn.append({"type": "function_call_output", "call_id": call["call_id"], "output": result.output})
        else:
            raise LLMError(f"tool loop did not finish within {self.max_steps} steps")

        stats["timing"] = {"ttft": first_token, "total": time.perf_counter() - start}
        self.session.append_turn(turn, stats)
        self.last_stats = stats

    def _instructions(self) -> str:
        # Without the date the model writes stale queries ("... 2025 ..."). It changes once a day,
        # so it only breaks the provider's prompt cache at midnight.
        return f"{self.system_prompt}\nToday's date: {date.today().isoformat()}."

    def _stream(self, instructions: str, items: list[dict]) -> Iterator[Text | ToolUse | _Final]:
        """One model call over OpenRouter's Responses API (stateless: full history every call)."""
        c = self.config
        payload: dict = {"model": c.model, "instructions": instructions, "input": items, "stream": True}
        if specs := self.tools.specs():
            payload["tools"] = specs
        if c.max_tokens:
            payload["max_output_tokens"] = c.max_tokens
        if c.temperature is not None:
            payload["temperature"] = c.temperature
        if c.reasoning_effort:
            payload["reasoning"] = {"effort": c.reasoning_effort}

        try:
            with self._http.stream("POST", "/responses", json=payload) as r:
                if r.status_code >= 400:
                    r.read()
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:500]}")
                for line in r.iter_lines():
                    if not line.startswith("data: "):  # skip blank lines and keepalive comments
                        continue
                    if line == "data: [DONE]":
                        break
                    event = json.loads(line[6:])
                    kind = event.get("type")
                    if kind == "response.output_text.delta":
                        yield Text(event["delta"])
                    elif kind == "response.output_item.done" and event["item"]["type"] in SERVER_SEARCH_ITEMS:
                        yield ToolUse("web_search", (event["item"].get("action") or {}).get("query", ""))
                    elif kind == "response.completed":
                        yield _Final(event["response"])
                        return
                    elif kind in ("response.failed", "response.incomplete", "error"):
                        raise LLMError(f"{kind}: {_reason(event)}")
        except httpx.HTTPError as e:
            raise LLMError(f"network error: {e}") from e
        raise LLMError("stream ended before the response completed")


def _persistable(item: dict) -> dict | None:
    """What to keep in history. Reasoning and function calls must come back verbatim; messages
    lose bulky citation annotations; server-side search items are dropped (the answer text and
    its links already carry what matters, and the queries are kept in the turn stats)."""
    kind = item["type"]
    if kind in ("function_call", "reasoning"):
        return item
    if kind == "message":
        parts = [
            {k: ([] if k in ("annotations", "logprobs") else v) for k, v in part.items()}
            for part in item["content"]
        ]
        return {**item, "content": parts}
    return None


def _tally(stats: dict, response: dict) -> None:
    stats["steps"] += 1
    stats["model"] = response.get("model") or stats["model"]
    u, total = response.get("usage") or {}, stats["usage"]
    total["input_tokens"] += u.get("input_tokens", 0)
    total["output_tokens"] += u.get("output_tokens", 0)
    total["cached_tokens"] += (u.get("input_tokens_details") or {}).get("cached_tokens", 0)
    total["reasoning_tokens"] += (u.get("output_tokens_details") or {}).get("reasoning_tokens", 0)
    total["cost"] += u.get("cost") or 0.0
    for item in response["output"]:
        if item["type"] in SERVER_SEARCH_ITEMS:
            action = item.get("action") or {}
            stats["searches"].append({"query": action.get("query"), "sources": [s["url"] for s in action.get("sources", [])]})


def _text_of(output: list[dict]) -> str:
    return "".join(
        part.get("text", "") for item in output if item["type"] == "message" for part in item["content"]
    )


def _short(arguments: str, limit: int = 100) -> str:
    return arguments if len(arguments) <= limit else arguments[:limit] + "…"


def _reason(event: dict) -> str:
    response = event.get("response") or {}
    return str(response.get("error") or response.get("incomplete_details") or event.get("error") or event)
