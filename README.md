# ai-harness

A small personal assistant harness: streaming, multi-turn chat with tools and sessions saved to
disk. Talks to [OpenRouter](https://openrouter.ai) through its Responses API. Default model is
`openai/gpt-6-luna` (cheap and fast).

## Quick start

Requires [uv](https://docs.astral.sh/uv/). It installs Python 3.14.7 for you.

```bash
git clone <this repo> && cd ai-harness
uv sync
echo 'OPENROUTER_API_KEY=sk-or-...' > .env     # .env is gitignored
uv run --env-file .env harness
```

On start it lists your saved sessions. Type a number to resume one, or press Enter for a new
one. Type `/exit` (or Ctrl-D) to quit. Ctrl-C during a reply cancels it and the turn is not saved.

Tool activity is shown as it happens, and every reply ends with a stats line:

```
  → web_search Anthropic newest Claude model released ...
  → fetch_url {"url":"https://www.python.org/downloads/","start":0}
  ← fetch_url ok · 0.3s · 12,122 chars
  [11987 in / 158 out (86 reasoning) · $0.00079 · first token 6.06s · total 6.24s · 25 tok/s · 2 model calls, 0 searches, 1 tool calls]
```

Commands: `/new`, `/sessions`, `/clear` (reset the model's context and the screen; the transcript stays
in the file), `/details` (full tool log of the last reply), `/help`, `/exit`. Resuming a session replays
the whole conversation.

Options:

```bash
uv run --env-file .env harness --name "blog post"      # title for a new session
uv run --env-file .env harness --model z-ai/glm-5.3-flash
```

## Tools

| Tool | Where it runs | Notes |
|---|---|---|
| `web_search` | OpenRouter (server tool) | Exa engine, about $0.007 per search, at most 3 per model call. The model writes the queries; guidance is in the system prompt. |
| `plan` | here | A checklist the model keeps up to date on multi-step tasks; shown in place with green checks. |
| `fetch_url` | here | Reads a page as text, 12k characters at a time (the model pages with `start`). Refuses non-public addresses, including via redirects. |

Tool output is capped at 20k characters so one result can't flood the context.

To add a tool, create `src/harness/tools/<name>.py` with a `Tool` subclass (`name`, `description`,
a pydantic `Args` model, `run()`), then register it in `tools/__init__.py`. The same `Args` model
produces the schema the model sees and validates what it sends.

## Sessions

One JSONL file per conversation in `~/.ai-harness/sessions/`, so you can resume from any
directory. The title defaults to your first message. A turn is saved only after it fully completes
(all its model calls and tool calls), so an interrupted turn never leaves a half-finished tool
exchange behind.

Each file holds the exact items replayed to the model on the next turn (messages, reasoning, tool
calls and outputs), plus a `turn_stats` line per turn with usage, cost, timing, and the search
queries and sources used.

## Settings

All optional, set in `.env` or the environment:

| Variable | Default | Notes |
|---|---|---|
| `OPENROUTER_API_KEY` | | required |
| `HARNESS_MODEL` | `openai/gpt-6-luna` | any OpenRouter model id |
| `HARNESS_REASONING_EFFORT` | `medium` | `low`, `medium`, `high`, or `off` to not send it. Reasoning tokens bill as output. |
| `HARNESS_MAX_TOKENS` | unset | sent as `max_output_tokens` |
| `HARNESS_TEMPERATURE` | unset | |
| `HARNESS_TIMEOUT` | `120` | seconds |
| `HARNESS_BASE_URL` | `https://openrouter.ai/api/v1` | |
| `HARNESS_HOME` | `~/.ai-harness` | where sessions are stored |

The system prompt lives in `src/harness/prompts.py`. It is applied at call time (with today's
date appended), so edits also affect resumed sessions. The search engine and per-call search cap
are arguments of `web_search_spec()` in `tools/web_search.py`.

## Layout

```
src/harness/
  runner.py    LLMRunner.chat(): model <-> tool loop, streams events, saves the turn. Override _stream to change provider.
  sessions.py  JSONL session files
  config.py    LLMConfig, loaded from env
  prompts.py   system prompt
  cli.py       session picker and chat loop
  tools/       one file per tool, plus base.py (Tool) and registry.py (validation, errors, output cap)
tests/         uv run pytest
```

## Development

```bash
uv run pytest
```
