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

## Tools and permissions

Every tool that has side effects asks before each call. You choose: **once**, **this session**,
**always** (saved to `~/.ai-harness/permissions.json`), or **deny** (with an optional reason that goes
back to the model). Rules are keyed on `tool:program`, e.g. `read:grep`; `S` / `A` remember the whole
tool. `/permissions` lists rules and `/permissions revoke <rule>` removes one. `--yes` disables
approvals (sandboxes only).

| Tool | Runs | What it does |
|---|---|---|
| `read` | here, no shell | One read-only command: cat, head, tail, grep, rg, ls, find, wc, file, stat, tree, diff |
| `write` | here, no shell | tee (content in `stdin`), touch, mkdir, cp, mv. Paths must stay in the workspace |
| `edit` | here, no shell | sed or patch (diff in `stdin`). Paths must stay in the workspace |
| `fetch_url` | here | Reads a page as text, paged. Refuses non-public addresses |
| `plan` | here | A checklist the model keeps current; needs no approval |
| `web_search` | OpenRouter | Server tool, about $0.007 per search. Cannot be gated by approvals |

Commands run with `shell=False`, so `;`, `|`, `$(...)` and `>` are plain arguments. The shell only gets
`PATH`, `HOME` and a few other variables, never your API key. Credential files (`.env`, keys) are
blocked, and the file tools are confined to the workspace (`HARNESS_WORKSPACE`, default: where you
started). These checks prevent accidents; the approval prompt is the real control.

The model's tool guidance is assembled from the registered tools (`tools_prompt` in `prompts.py`) and
appended to the system prompt. To add a tool, create `src/harness/tools/<name>.py` with a `Tool`
subclass (`name`, `description`, `usage`, a pydantic `Args`, `run()`) and register it in
`tools/__init__.py`.

A turn that already ran tools is saved even if it stops early (error, Ctrl-C, step limit), so the
model remembers what it did.

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
