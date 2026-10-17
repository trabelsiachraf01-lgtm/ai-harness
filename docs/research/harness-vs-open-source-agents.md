# ai-harness vs Hermes Agent, opencode, and Codex CLI

Date: 2026-10-10. Compared against primary source (shallow clones, read-only, never executed).

## TL;DR

1. **Your core loop is the same shape as all three.** Model call, execute tool calls, feed results back, stop when no calls. The differences are everything *around* the loop: retries, context budget, sandbox, persistence granularity.
2. **The two gaps that will actually hurt you first are context growth and transient-error handling.** You resend the whole history every call with no compaction, no output spilling, and no retry on 429/5xx. All three projects solve both, and they solve the cheap half first (prune old tool output, no LLM call) before summarizing.
3. **Your edit tool is the weakest design choice.** Making the model emit a *unified diff with correct hunk headers* is the failure-prone path. opencode and Hermes use exact-string replace with a fuzzy-match fallback chain; Codex uses a purpose-built `apply_patch` envelope that has no line numbers. Switch to `old_string/new_string` replace implemented in Python (not `sed`/`patch`), and render a real diff in the approval prompt.
4. **Your permission model is stricter than all three by default and coarser in key granularity.** Approve-everything is right for a personal harness. But `edit:sed` / `read:grep` "always" rules are keyed on program only; opencode keys on command-prefix patterns and Codex on argv-prefix rules. The prompt-only boundary also has at least one real hole (`rg --pre`, see Gaps).
5. **Nobody treats in-process allowlists as a security boundary.** Hermes's SECURITY.md says so explicitly: "The only security boundary against an adversarial LLM is the operating system." Codex is the only one of the three with real OS sandboxing on by default (Seatbelt / bwrap+seccomp / Landlock). Don't build a regex denylist; if you want a boundary, wrap the subprocess in `sandbox-exec` or run `--yes` only in a container.
6. **You have zero logging, and your own CLAUDE.md says observability is first-class.** All three have structured logs; Codex and opencode have OpenTelemetry; Hermes has rotating redacted log files plus an observer-hook contract. Per-turn `turn_stats` in the JSONL is good but is not a log: errors, retries, permission decisions, and raw request ids are not captured anywhere.
7. **Things you should deliberately not copy:** subagents, MCP, plugin/hook systems, multi-provider abstraction, gateway/messaging, WebSocket transports, tool parallelism. Hermes's own contributor rubric rejects "hooks with no concrete consumer" and "a new core tool when terminal + file already does the job".
8. **Where you are genuinely ahead:** atomic fsync'd turn writes with torn-line repair and an explicit "last safe boundary" for partial saves, a tool prompt assembled from the registry, `shell=False` + scrubbed env, and an honest usage/cost line per turn. At 2,900 lines with 107 deterministic tests, it is auditable end to end, which none of these three are.

## Method and sources

All repos were cloned with `--depth 1` into the scratchpad and read as untrusted data. Line references below are `path:line` at the pinned commit.

| Project | Repo (verified) | Commit | Commit date |
|---|---|---|---|
| Hermes Agent | https://github.com/NousResearch/hermes-agent (README: "The self-improving AI agent built by Nous Research"; `gh search` shows 252k stars) | `a62979dc3601` | 2026-10-10 |
| opencode | https://github.com/anomalyco/opencode (`gh repo view sst/opencode` resolves to `anomalyco/opencode`; default branch is `dev`) | `055d95bb7e27` | 2026-10-09 |
| Codex CLI | https://github.com/openai/codex (Rust workspace in `codex-rs/`) | `806d9732c974` | 2026-10-10 |
| ai-harness | local, `main` | `0d685655ad83` | |

Notes on identification:
- "Hermes Agent" is `NousResearch/hermes-agent`, a Python project. It is *not* a coding-agent-first tool; it is a personal agent with a gateway (Telegram/Discord/...), memory, skills, cron. It is the closest in spirit to your "personal AI assistant harness" (root `AGENTS.md`, "What Hermes Is").
- opencode is mid-migration: `packages/opencode/src` is the shipping runtime (Vercel AI SDK `streamText`, `session/llm.ts:9,280`), while `packages/llm` and `packages/core` hold a newer native client and v2 schemas (`session/llm.ts:10-12,224`). I cite `packages/opencode/src` unless stated. Product version `1.18.35` (`packages/opencode/package.json:3`).
- Version numbers for Codex and Hermes are placeholders (`0.0.0`) in manifests, so I cite commit SHAs only.
- Docs I did not read as primary source: Codex's security/AGENTS.md guides live on developers.openai.com (the repo's `docs/sandbox.md` and `docs/agents_md.md` are one-line pointers). Behavior claims for Codex come from source, not those guides.
- Not covered: Claude Code, Aider, pi-mono. Out of scope per "keep it focused".

Everything marked **(inference)** is my reading, not stated in source.

## Your harness, as verified

Python 3.14, ~2,900 lines in `src/harness`, 107 tests passing (`uv run pytest`: 107 passed).

| Area | What the code does | Where |
|---|---|---|
| Loop | `for _ in range(max_steps=20)`; each iteration one streaming `/responses` call; calls executed sequentially; `else:` branch stops with a user-visible note | `runner.py:103-136` |
| Transport | httpx SSE against OpenRouter Responses API, stateless, full `[*session.items, *turn]` every call | `runner.py:152-188` |
| Turn persistence | `turn` list only appended to session on completion in a single `write` + `flush` + `fsync`; `safe` index marks last complete step; on error/Ctrl-C/step limit, `turn[:safe]` is saved if any tool ran | `runner.py:87-143`, `sessions.py:77-89` |
| Torn write repair | `load` truncates a partial final line | `sessions.py:44-47` |
| Context reset | `/clear` writes a `context_clear` marker; earlier lines stay on disk | `sessions.py:68-75`, `:51-52` |
| Tools | one file per tool; pydantic `Args` produces JSON schema and validates; `ToolRegistry.run` never raises; 20k-char output cap | `tools/base.py`, `tools/registry.py:39-72` |
| CLI tools | `shlex.split` + `shell=False`, per-tool program allowlist, glob expansion done in-process, workspace path confinement, credential file/suffix blocklist, `find -exec` etc. blocked, `sed w/e` blocked, env reduced to 7 vars, 30s timeout | `tools/cli.py` |
| Edit | `sed -i` or `patch -p0` with unified diff in `stdin`; dry-run first so patch is all-or-nothing; no `.orig/.rej`; diff header paths checked | `tools/edit.py:29-62,86-96` |
| Write | `tee`, `touch`, `mkdir`, `cp`, `mv`; approval prompt warns on overwrite and previews 15 lines | `tools/write.py:43-58` |
| fetch_url | public-IP check on every redirect hop, 2 MB cap, text only, paged output | `tools/fetch_url.py:48-84` |
| Permissions | per-call approval; rules `tool` or `tool:scope`; once/session/always; always persisted in `~/.ai-harness/permissions.json`; deny with reason fed back to model | `permissions.py:50-97` |
| Prompt | system prompt + `tools_prompt(registry)` assembled from each tool's `usage` + date | `prompts.py:14-29`, `runner.py:145-150` |
| Stats | per-turn: tokens in/out/cached/reasoning, cost, ttft, total, searches, tool timings, permission source, `stopped` reason | `runner.py:89-91,207-219` |
| UI | prompt_toolkit + rich, in-process; approver injected as a callable | `ui.py`, `cli.py` |
| Server tool | OpenRouter `web_search`, cannot be gated by approvals | `tools/web_search.py` |

### Confirmed absent (by grep over `src/`)

`grep -rniE` for retry, backoff, compact, summar, subagent, mcp, hook, AGENTS, CLAUDE.md, logging, logger, otel, telemetry, git, sandbox, seatbelt, undo, checkpoint, asyncio, thread, concurrent, cache_control, export returned only comments/UI strings. So, confirmed absent:

- retries or backoff on any HTTP error (`runner.py:166-169` raises `LLMError` immediately)
- context compaction, token budgeting, or context-window awareness (only `max_output_chars`)
- subagents, MCP, hooks, plugins, skills, custom commands
- project instruction loading (AGENTS.md / CLAUDE.md)
- any `logging` use at all
- OS sandboxing
- git integration, undo, checkpoints, diff display of what actually changed (approval shows the *proposed* diff only for patch)
- parallel tool calls (sequential `for call in calls`; `parallel_tool_calls` not sent)
- provider abstraction beyond OpenRouter's base URL
- explicit prompt-cache control (you rely on provider-side automatic caching; you do record `cached_tokens`)
- cancellation of an in-flight HTTP stream other than via `KeyboardInterrupt` propagating

## Hermes Agent (NousResearch/hermes-agent)

Scale: ~122k lines in `agent/` alone, 5,882 test files (`find tests -name "*.py"`). It is a platform: CLI, TUI (Ink), Electron desktop, ~20 messaging adapters, cron, kanban, memory, skills.

**1. Loop.** Synchronous `while` over an `IterationBudget`, split into ~25 `turn_*.py` phase modules (`agent/AGENTS.md`, "Agent loop"). Loop head: `agent/conversation_loop.py:1649`. Default `max_iterations` is `sys.maxsize` in the constructor (`run_agent.py:241`) although docs say 500 (`agent/AGENTS.md`, `agent/iteration_budget.py:3-5`); I did not trace which wins in the CLI path. A "grace call" lets the model wrap up after the budget is exhausted (`conversation_loop.py:1649`). Subagents get their own budget (default 50). Parallel tool calls: planner splits a batch into ordered parallel/sequential segments (`agent/tool_executor.py:1911-1927`), 8 worker threads (`:130`); only a whitelist is parallel-safe (`agent/tool_dispatch_helpers.py:30-45`), and file readers/writers are admitted by canonical-path overlap (`:47-52,264-290`). Retries: error taxonomy with recovery hints (`agent/error_classifier.py:34-60`: auth, billing, rate_limit, overloaded, context_overflow, ...), jittered backoff (`agent/retry_utils.py:121`), credential rotation and model fallback chains.

**2. Context.** OpenAI chat-completions message format (`agent/AGENTS.md`). Compression at 50% of window by default (`hermes_cli/config.py:3074`), protect last 20 messages, prune old tool results first with no LLM call, then structured summary via an auxiliary model (`agent/AGENTS.md`, "Compression"; `agent/context_compressor.py`). Large tool outputs are persisted to a spillover file and replaced with a preview + path (`tools/tool_result_storage.py:1-7`); per-tool caps of 100k chars (`tools/file_tools.py:1396-1421`). Prompt caching is treated as an invariant: system prompt byte-stable for the life of a conversation; mid-conversation injections ride user/tool messages; compression is the only sanctioned cache break (`AGENTS.md` root, "Two invariants"; `agent/prompt_caching.py:1-5`: 4 `cache_control` breakpoints).

**3. Tools.** `registry.register()` at import time with a `check_fn` for availability, JSON-string results, toolsets (`tools/AGENTS.md`). Edit: `patch` tool with `replace` mode using a 9-strategy fuzzy chain (`tools/fuzzy_match.py:289-298`: exact, line_trimmed, whitespace_normalized, indentation_flexible, escape_normalized, trimmed_boundary, unicode_normalized, block_anchor, context_aware; similarity strategies refuse `replace_all`, `:302`) plus a V4A multi-file patch mode (`tools/file_tools.py:148-173,931-942`).

**4. Permissions/sandbox.** Inverse of yours: the terminal runs by default and a *denylist* of ~hundreds of regexes triggers approval (`tools/approval_detection.py:228` `DANGEROUS_PATTERNS`, plus an unconditional `HARDLINE_PATTERNS` floor `:~72-110`). Modes manual/smart (a guardian LLM, `tools/approval_smart.py`)/off; per-session and permanent allowlists (`tools/approval.py:62-69`); consecutive-denial circuit breaker (`:55-70`); `HERMES_YOLO_MODE` frozen at import so a skill cannot flip it (`:51-53`). Isolation is by terminal *backend* (local, docker, ssh, modal, daytona, singularity: `tools/AGENTS.md`). `SECURITY.md` §2.2 and §2.4 state the approval gate is "a heuristic" and "structurally incomplete"; real boundary = OS/container or whole-process wrapping (Docker, NVIDIA OpenShell).

**5. Sessions.** SQLite `SessionDB` with FTS5 search (`hermes_state_schema.py` FTS DDL; ~35 `hermes_state_*.py` siblings: rewind, compression archive, usage). Checkpoints: transparent shadow-git store before file-mutating ops, once per turn, rollback to any (`tools/checkpoint_manager.py:1-45`). `/steer` and rewind exist (`agent/AGENTS.md`; `hermes_state_rewind.py`).

**6. Extensibility.** Plugins (`plugins/`, `~/.hermes/plugins/<name>/plugin.yaml`), skills (agentskills.io compatible), MCP client (`tools/mcp_tool.py:1-4`: stdio/HTTP/SSE, daemon-thread event loop), subagents (`tools/delegate_tool.py:1-12`: child AIAgent, parent sees only the summary), context files `AGENTS.md`/`.cursorrules`/`SOUL.md` scanned for injection and capped (`agent/prompt_builder.py:36-89`). Contributor rubric: "hooks with no concrete consumer" are rejected; footprint ladder prefers skill over plugin over core tool (root `AGENTS.md`).

**7. Providers.** Adapter per wire format: chat_completions, codex_responses, anthropic, bedrock, gemini (`agent/AGENTS.md` `api_mode`; `agent/codex_responses_adapter.py`, `anthropic_adapter.py`, `bedrock_adapter.py`). Provider profiles are plugins; separate "auxiliary" model routes for compression, vision, title (`agent/AGENTS.md`).

**8. Observability.** `hermes_logging.py:1-8`: `agent.log`, `errors.log`, `gateway.log`, `gui.log`, rotating, queue-driven, `RedactingFormatter` so secrets never reach disk. Observer-hook contract for Langfuse/OTel-style plugins (`website/docs/developer-guide/observer-hooks.md`; `plugins/observability/langfuse`). Usage/billing modules (`agent/billing_usage.py`, `account_usage.py`).

**9. UI.** Python JSON-RPC backend (`tui_gateway/`) serving an Ink TUI and the Electron app; gateway process for messaging (`AGENTS.md` root layout). Headless: `batch_runner.py`, cron, `chat()` API.

**10. Tests.** Behavior-contract tests, per-file subprocess isolation, credentials unset, temp `HERMES_HOME`; banned shapes: change-detector and source-reading tests (`tests/AGENTS.md`). `evals/` dir with probes.

## opencode (anomalyco/opencode)

TypeScript on Bun + Effect; HTTP server + clients (TUI, web, desktop, SDKs).

**1. Loop.** `while (true)` in `packages/opencode/src/session/prompt.ts:1088`. It **re-reads the message history from the DB every iteration** (`:1093`) and decides to exit from `lastAssistant.finish` (`:1107-1127`), tolerating providers that return "stop" with tool calls (`:1100-1104`). Step limit is per-agent `steps` (default `Infinity`, `:1178`); on the last step it appends a `MAX_STEPS_PROMPT` assistant message that disables tools and demands a summary (`:1281`; text in `packages/core/src/session/runner/max-steps.ts`) instead of cutting off. Doom-loop detector: three identical consecutive calls trigger a `doom_loop` permission ask (`session/processor.ts:29,356-373`). Retry: exponential backoff 2s x2, jitter 0.25, max 5 retries, honors `retry-after(-ms)`, regex-classified retryable messages (`session/retry.ts:24-60`). Tool execution concurrency: `Effect` `concurrency: "unbounded"` in processor (`processor.ts:588`) **(inference: tool calls from one response run concurrently)**.

**2. Context.** Messages and parts stored structurally; `time.compacted` marks pruned tool outputs. Pruning keeps the last 40k tokens of tool output, prunes only if >20k reclaimable, protects `skill` outputs (`session/compaction.ts:28-31,271-315`). Overflow check on last finished assistant's token usage triggers an LLM summarization "compaction" task (`prompt.ts:1136-1147`). Tool output truncated to 2000 lines / 50 KB with the full text written to a file and a hint to read it (`tool/truncate.ts:21-22,36-39`; 7-day retention). Prompt caching via per-provider `cacheControl` markers on system + last messages (`provider/transform.ts:358-379,483`).

**3. Tools.** `Tool.define` with Effect Schema params, one `.txt` description per tool (`tool/*.txt`), registry (`tool/registry.ts`). Built-ins: read, write, edit, apply_patch, glob, grep, shell, webfetch, websearch, todo, task (subagent), skill, lsp, question, plan. Edit is exact replace with a replacer chain borrowed from cline/gemini-cli (`tool/edit.ts:1-5,695-703`: Simple, LineTrimmed, BlockAnchor, WhitespaceNormalized, IndentationFlexible, EscapeNormalized, TrimmedBoundary, ContextAware, MultiOccurrence), per-file semaphore (`:29-41`), then LSP diagnostics and formatter. Shell commands parsed with web-tree-sitter bash/powershell (`tool/shell.ts:9,257-258`).

**4. Permissions.** Ruleset of `{permission, pattern, action: allow|ask|deny}`, **last match wins** (`permission/index.ts:28-35`), wildcard matching, merged from defaults, agent, user config. **Default is `"*": "allow"`**; only `external_directory` outside whitelist, `.env` reads, and `doom_loop` ask (`agent/agent.ts:119-140`). Per-agent profiles: `build` (default), `plan` (denies edit except plan files, `:156-181`), `general` subagent. Bash commands are reduced to a human-meaningful prefix via an arity table (`permission/arity.ts:1-35`, e.g. `git checkout`, `npm run dev`) so "always" is keyed on `git checkout *` not on the exact line. "Always" approvals are held in an in-memory `approved` list per instance (`permission/index.ts:44-50,136-141`) **(inference: not persisted across restarts; I saw no store write)**. Rejection can carry feedback (`CorrectedError`, `:119-124`) and rejecting cancels all pending asks for the session. No OS sandbox found in `packages/opencode/src` (grep for seatbelt/landlock/bwrap not run on all packages; `containers/` exists at top level for CI/dev images, not examined).

**5. Sessions.** SQLite via drizzle: `SessionTable`, `MessageTable`, `PartTable`, `TodoTable` (`packages/core/src/session/sql.ts:22,68,82,100`). Revert/unrevert by message or part using a **shadow git snapshot** (`session/revert.ts:14-30`; `snapshot/index.ts:23-37` `track/patch/restore/revert/diff`, 7-day prune). Because parts are persisted as streamed, there is explicit handling of "orphaned interrupted tool" parts (`prompt.ts:1100-1127`) **(inference: this is the cost of per-part persistence that your atomic turn write avoids)**.

**6. Extensibility.** MCP client with OAuth (`src/mcp/`), plugins with hooks such as `chat.params`, `tool.execute.before` (`packages/plugin/src/index.ts:247,266`), skills, custom agents/subagents via `task` tool, `AGENTS.md` and (by default) `CLAUDE.md` loaded from global and project; first project-level match wins (`session/instruction.ts:61-66,122`), custom commands (`src/command`).

**7. Providers.** Vercel AI SDK with ~25 `@ai-sdk/*` providers (`packages/opencode/package.json:58+`), models.dev catalog, per-provider transforms (`provider/transform.ts`). A native Responses/Chat client in `packages/llm` is being introduced behind a seam (`session/llm.ts:224`).

**8. Observability.** Effect logger layers + OTLP tracing (`packages/core/src/observability.ts:1-25`, `observability/otlp.ts`); `Effect.logInfo("loop", {session.id, step})` throughout (`prompt.ts:1089`); per-message `cost` and `tokens {input, output, reasoning, cache{read,write}}` (`prompt.ts:1186-1194`); session share.

**9. UI.** Server-first: `packages/server` HTTP API + generated SDK; TUI, web app, desktop, ACP and Slack are clients (`AGENTS.md` dependency-direction rule; `packages/*` layout). Headless `opencode run`.

**10. Tests.** 255 `*.test.ts` in `packages/opencode/test`, temp-dir + optional git repo fixtures (`test/AGENTS.md`), fake provider dir, `packages/http-recorder` for recorded HTTP.

## Codex CLI (openai/codex)

Rust workspace, ~154 crates (`codex-rs/Cargo.toml`). Core in `codex-rs/core`.

**1. Loop.** `run_turn` (`core/src/session/turn.rs:164`) with an unbounded `loop` (`:425`); I found **no max-steps constant** in `core/src` (`grep max_turns|max_steps|max_iterations` empty outside tests). Termination = model returns no follow-up and no pending steered input (`:~574-575`, `needs_follow_up`), then Stop hooks may force continuation (`:~658-690`). Mid-turn user input ("steer") is drained between steps (`:~427-440`). Cancellation via `CancellationToken` threaded through tool calls (`tools/parallel.rs:14,170-220`). Parallel tool calls: `parallel_tool_calls: true` in the request (`turn.rs:1577`); a `RwLock` where parallel-safe tools take the read lock and others the write lock (`tools/parallel.rs:51,141-216`; `tools/router.rs:246`). Retries: stream retry budget, connection retry delay 5s up to 60s, server `Retry-After` advice honored, transport fallback WebSocket to HTTP (`responses_retry.rs:22-23,33-60`).

**2. Context.** Responses API items as history (`codex-history`). Auto-compaction when token limit reached, with local summarization and a remote "compaction v2" path (`compact.rs`, `compact_remote_v2.rs`, `session/turn.rs:~610-760`); pre/post-compact hooks (`compact.rs:1-40`). `WorldState` fragments track environment context diffs per step (`context/world_state`, `session/world_state.rs`). Output truncation policy in `utils/output-truncation` and `protocol/src/error.rs:663`. Server-side caching: `prompt_cache_key` and `previous_response_id` over a per-turn cached WebSocket (`core/src/client.rs:12-18,271`). So Codex is **not** stateless like your harness; it sends deltas when it can **(inference from client.rs docs; fallback to full history exists)**.

**3. Tools.** Spec plan per model (`core/src/tools/spec_plan.rs`), shell/exec tools, `apply_patch`, `update_plan`, web_search, MCP, view_image, multi-agent. `apply_patch` uses a purpose-built envelope: `*** Begin Patch`, `*** Add File:`, `*** Update File:`, `*** Move to:`, `@@` context lines, no line numbers (`apply-patch/src/parser.rs:1-45`), with a streaming parser so diffs can be shown while the model is still writing (`streaming_parser.rs`) and a `TurnDiffTracker` aggregating the turn's diff (`core/src/turn_diff_tracker.rs:49`).

**4. Permissions/sandbox.** Two orthogonal axes. `AskForApproval`: `untrusted | on-request (default) | granular | never` (`protocol/src/protocol.rs:1092-1115`). `SandboxPolicy`: `read-only | workspace-write {writable_roots, network_access} | danger-full-access | external-sandbox` (`:1178-1210`). Enforcement: macOS Seatbelt with a closed-by-default `.sbpl` profile (`sandboxing/src/seatbelt_base_policy.sbpl:1-12`, `seatbelt.rs`), Linux bubblewrap + seccomp + Landlock (`linux-sandbox/src`, `sandboxing/src/landlock.rs`), Windows sandbox, and a managed network proxy for egress policy (`network-proxy`). Command safety: `shell-command/src/command_safety/is_dangerous_command.rs`; bash parsed with tree-sitter (`shell-command/src/bash.rs`). Policy language: Starlark `prefix_rule(pattern, decision=allow|prompt|forbidden, justification, match, not_match)` with examples validated at load time (`execpolicy/README.md:5-30`). Plus a Guardian reviewer (LLM) path (`core/src/guardian*`). Enterprise requirements layering (`config` requirements.toml, `core/src/config/mod.rs:4201,4941`).

**5. Sessions.** "Rollouts": JSONL files (`rollout/src/recorder.rs:1`) with a SQLite state index and reverse-JSONL scanner for fast tail reads (`rollout/src/state_db.rs`, `reverse_jsonl_scanner.rs`), writer lock, optional compression, resume/fork via thread store (`thread-store`, `rollout/src/list.rs`). Git helpers for worktrees/ghost snapshots in `git-utils`; I did not confirm a user-facing undo.

**6. Extensibility.** MCP client (`rmcp-client`, `codex-mcp`) and server mode, hooks with 12 events (`protocol/src/protocol.rs:1688-1701`: PreToolUse, PermissionRequest, PostToolUse, Pre/PostCompact, SessionStart/End, UserPromptSubmit, SubagentStart/Stop, Stop, Interrupt) and handler types command/mcp_tool/prompt (`:1705+`), skills (`skills/`), plugins, AGENTS.md concatenated from project root (marker `.git`) down to cwd (`core/src/agents_md.rs:1-15`), multi-agent (`core/src/session/multi_agents.rs`).

**7. Providers.** Responses API first-class (`codex-api`, `core/src/client.rs`), `model-provider-info` for custom providers, ollama and lmstudio crates; reasoning effort per step (`session/reasoning_effort.rs`).

**8. Observability.** `tracing` everywhere; `codex-otel` crate with OTLP exporters, traces, metrics, tool-result and skill-invocation events (`otel/src/lib.rs`); analytics crate; `rollout-trace`; `response-debug-context`.

**9. UI.** Ratatui TUI backed by an `app-server` speaking a JSON-RPC protocol (`tui/Cargo.toml:32-34`; `app-server/README.md`); `exec` crate for non-interactive `codex exec` (`docs/exec.md`); SDKs in `sdk/`.

**10. Tests.** ~243 integration suite files under `core/tests/suite`, snapshot tests (`session/snapshots`), mock-server based; Bazel build.

## Comparison matrix

| Dimension | ai-harness | Hermes | opencode | Codex |
|---|---|---|---|---|
| Loop bound | 20 steps, hard stop | budget (docs 500; ctor `sys.maxsize`) + grace call | per-agent `steps` (default inf) + "tools disabled, summarize" prompt | none found; ends when model stops |
| Tool parallelism | sequential | segment planner, 8 threads, path-overlap aware | concurrent (inference) | RwLock: parallel-safe tools share |
| Retries | none | classified errors, jittered backoff, key rotation, fallback models | 5x exp. backoff, honors Retry-After | budgeted, Retry-After, WS to HTTP fallback |
| Streaming/cancel | SSE, Ctrl-C | threads + interrupt flag, `/steer` | Effect fibers, abort | async + cancellation tokens, steer queue |
| History | Responses items, stateless | chat-completions msgs | structured parts in SQLite | Responses items; `previous_response_id` when possible |
| Compaction | none | prune tool results, then aux-LLM summary at 50% | prune old tool output, then LLM summary on overflow | auto-compact (local + remote) on token limit |
| Tool output cap | 20k chars, truncate | per-tool caps + spill to file | 2000 lines/50 KB + full text to file | truncation policy |
| Prompt caching | none explicit (cached_tokens recorded) | explicit 4 breakpoints + byte-stable prefix invariant | per-provider markers | prompt_cache_key + previous_response_id |
| Schema validation | pydantic | JSON schema, arg coercion | Effect Schema | typed (serde) |
| Edit mechanic | `sed -i` / `patch -p0` unified diff (dry-run, atomic) | fuzzy replace (9 strategies) + V4A patch | fuzzy replace (9 replacers) + apply_patch | `apply_patch` envelope, streaming parser |
| Approval default | ask every side-effect call | ask only on dangerous-regex hit | **allow all**, ask for external dir/.env/doom loop | on-request, within sandbox |
| Rule granularity | `tool:program` | exact-command allowlist + deny rules | wildcard rules, command-prefix arity | argv prefix rules (Starlark) |
| Rule persistence | JSON file | config + permanent set | in-memory (inference) | config/rules files |
| OS sandbox | none | backend isolation (docker/ssh/...); not default | none found | Seatbelt / bwrap+seccomp / Landlock, network proxy |
| Command parsing | shlex, shell=False, allowlist | regex denylist | tree-sitter bash | tree-sitter + is_dangerous |
| Session store | JSONL per conversation, atomic turn append | SQLite + FTS5 | SQLite | JSONL rollouts + SQLite index |
| Resume/fork/undo | resume, `/clear` | resume, rewind, shadow-git checkpoints | resume, fork, git-snapshot revert | resume/fork; undo not confirmed |
| MCP | no | client (stdio/HTTP/SSE) | client + OAuth | client + server |
| Hooks/plugins | no | plugins + observer hooks | plugin hooks | 12 hook events |
| Subagents | no | `delegate_task` | `task` tool, agent profiles | multi-agent |
| Project instructions | no | AGENTS.md/.cursorrules/SOUL.md, injection-scanned | AGENTS.md (+CLAUDE.md) | AGENTS.md root-to-cwd |
| Providers | OpenRouter Responses only | adapters per wire format + plugins | Vercel AI SDK, ~25 | Responses-first + custom/ollama/lmstudio |
| Observability | per-turn stats in JSONL; no logs | rotating redacted logs, observer hooks, Langfuse | Effect logs + OTLP, per-message cost/tokens | tracing + OTLP + analytics |
| UI | in-process TUI | JSON-RPC backend, Ink TUI, desktop, gateway | HTTP server + many clients | app-server + ratatui, `exec` |
| Tests | 107 deterministic, fake SSE | thousands, per-file isolation, evals | 255 files, fixtures, HTTP recorder | large suite, snapshots, mock server |

## Where the harness is already as good or better

- **Turn atomicity.** One `write` + `flush` + `fsync` per turn with torn-tail repair (`sessions.py:44-47,85-88`), and a `safe` marker so a partial save never contains a `function_call` without its output (`runner.py:88,132`; test `test_a_step_interrupted_between_call_and_output_is_not_saved`). opencode persists per part and needs "orphaned interrupted tool" cleanup (`prompt.ts:1100-1127`). Hermes needs lease/fence machinery for concurrent writers (`agent/AGENTS.md`, compression section). Yours is simpler and correct for a single-writer CLI.
- **Execution without a shell.** `shlex` + `shell=False` + in-process glob expansion + env scrub is a stronger default than Hermes's regex denylist, which its own SECURITY.md calls "structurally incomplete". Neither opencode nor Hermes scrubs the model's subprocess env by default as aggressively (Hermes does strip provider keys, `SECURITY.md` §2.3).
- **Tool prompt from the registry** (`prompts.py:14-29`). Hermes had to add post-processing so schemas don't reference tools from disabled toolsets (`tools/AGENTS.md`, "Schema descriptions must not name tools from other toolsets"). You avoid the problem by construction.
- **Denials are information.** Deny-with-reason is fed back with "do not retry the same call" (`tools/registry.py:56-60`). opencode has the same idea (`CorrectedError`); Codex does not surface a user reason as directly.
- **Honest cost line.** Per-turn tokens, cached, reasoning, cost, ttft, tok/s, search count. None of the three print this inline by default.
- **Plan tool that needs no approval and has validated state** (`tools/plan.py`), with alias tolerance for model drift.
- **SSRF guard re-validating each redirect hop** (`fetch_url.py:48-84`).
- **Auditability.** The entire security-relevant surface is `tools/cli.py` (140 lines) + `permissions.py` (97 lines). That is a real property; the three projects cannot claim it.

## Gaps, ranked by value for effort

Ranking uses: likelihood of hurting you in daily use x portfolio signal / effort. Each item names where it goes.

### 1. Structured logging (effort: S, value: high)

Today a failed turn leaves only a terminal message. Add `logging` with a JSONL file handler in `~/.ai-harness/logs/harness.log` (rotating), configured in `cli.py`; emit events from `runner.py` (`request` with model/step/input item count, `response` with usage/response id, `retry`, `tool_start/tool_end` with duration and permission source, `stopped`) and from `permissions.py` (decision, rule key). Include `session_id` and a per-turn `turn_id`. Redact `Authorization` and tool stdin/outputs beyond N chars (Hermes redacts at the formatter: `hermes_logging.py:1-8`). Do **not** add OpenTelemetry yet; Codex/opencode use it because they have multi-process architectures. Plain JSON lines are greppable and enough. Store the OpenRouter generation id per call so cost can be reconciled later.

### 2. Retry with backoff before first token (S, high)

`runner.py:165-188`. Retry only when nothing has been yielded from this call yet (connect error, HTTP 408/429/5xx, `response.failed` before any delta). Use exponential backoff with jitter, honor `Retry-After`, cap at ~4 attempts, log each retry (needs #1). Show a muted "retrying in Ns" line in the UI. Do not retry mid-stream: you'd duplicate visible text, and no project does it cheaply (Hermes's stall-retry machinery is the cautionary tale). Classification can be 15 lines, not Hermes's 1,500 (`error_classifier.py`); take the shape of `session/retry.ts:24-60` as a template.

### 3. Exact-replace edit tool in Python (M, high)

Replace `sed -i`/`patch` as the main edit path with `edit(path, old_string, new_string, replace_all=False)` implemented in `tools/edit.py` directly: read file, require exactly one match unless `replace_all`, return a clear error with match count, write via temp file + `os.replace`. Reuse `CliTool._check_path` for confinement and credential blocks. Generate a real unified diff with `difflib.unified_diff` and show it in `describe()` (approval) and `display()` (result). Add a *small* fuzzy fallback (line-trimmed, then whitespace-normalized; both refuse `replace_all`) from `tools/fuzzy_match.py:289` in Hermes or `tool/edit.ts:695` in opencode; skip the similarity strategies at first. Keep `patch` only if you see multi-file changes. Rationale: unified-diff hunk headers are the failure mode all three projects engineered around.

### 4. Context budget: spill and prune, not summarize (M, high)

Two steps, in order. (a) In `ToolRegistry.run`, when output exceeds the cap, write the full text to `~/.ai-harness/spill/<session>/<n>.txt` and return head + path + "use `read` with `head/tail/grep`" (opencode `tool/truncate.ts`; Hermes `tool_result_storage.py`). This turns your lossy truncation into a recoverable one. (b) In `LLMRunner.chat`, before building `input`, if the last response's `usage.input_tokens` exceeded ~60% of a configured `context_window`, replace tool *outputs* older than the last K steps with a one-line stub (opencode's pruning: `compaction.ts:271-315`, no LLM call). Persist stubs as a marker like `context_clear` so the JSONL stays append-only. Only add LLM summarization after you've hit a real overflow with pruning on. Caveat: pruning edits history and breaks provider prompt-cache prefixes; Hermes treats that as a "sanctioned break" (`agent/AGENTS.md`). Accept it once per overflow, not every turn.

### 5. Project instructions (AGENTS.md) (S, medium-high)

Codex walks root (`.git`) to cwd and concatenates (`core/src/agents_md.rs:1-15`); opencode takes the first project-level match and also reads `CLAUDE.md` (`session/instruction.ts:61-66,122`); Hermes caps size and scans for injection (`prompt_builder.py:36-89`). Do the opencode version: in `_instructions()` (`runner.py:145`), append the first of `AGENTS.md`/`CLAUDE.md` found walking up from workspace root, capped at ~8k chars, labelled as untrusted project data. Load once per session start to keep the prefix stable. This also matters for you personally: this repo's `CLAUDE.md` would immediately apply.

### 6. Undo for write/edit (M, medium)

Before an approved write/edit mutates a path, copy the original to `~/.ai-harness/undo/<session>/<step>/<relpath>` (or record "did not exist"). Add `/undo` that restores the last step's files. This is the 80% version of opencode's shadow-git snapshot (`snapshot/index.ts`) and Hermes's checkpoint manager (`tools/checkpoint_manager.py:1-45`) in ~60 lines and no git dependency. Skip a shadow-git store until you want per-turn diffs across many files.

### 7. Close the `rg --pre` hole and tighten rule keys (S, medium; do soon)

Verified by reading `CliTool.parse` (`tools/cli.py:76-97`): tokens starting with `-` skip all path checks and only `find`/`sed` have program-specific rules. `rg --pre <cmd>` (and `--pre-glob`) executes `<cmd>` per file, so `rg --pre sh '' file` runs file contents as a shell script, turning `read` into code execution. Not verified by running it; I read the code only. Fix: add `rg` to `_check_program` to reject `--pre*`; likewise check `grep`/`diff`/`file`/`tree` flags against an explicit allowed-flag set rather than a blocklist. Also consider keying "always" on `tool:program:subcommand-or-flag-class`, or at minimum making `A` ("always, whole tool") require a second confirmation: `edit:sed` always-allow currently permits `sed -i` on any workspace file with no prompt.

### 8. Cache awareness (S, low-medium)

You record `cached_tokens` but never act. Cheap win: keep `instructions` byte-stable (the date line is the only per-day change, fine), keep tool order stable (it is), and log cache hit ratio per turn in the stats line when > 0. Don't add explicit `cache_control` unless you move to Anthropic models via OpenRouter; then follow Hermes's 4-breakpoint layout (`agent/prompt_caching.py:1-5`).

### 9. Step-limit behavior (XS, low)

On `max_steps`, you stop abruptly with a bracketed note. opencode instead injects a "tools disabled, summarize progress" message for the last step (`prompt.ts:1178-1179,1281`), so the user gets a useful summary. One extra model call with `tools` omitted on step `max_steps`.

### 10. Eval/regression harness (M, portfolio value)

Your unit tests cover mechanics. A tiny replayable eval set (5-10 tasks run against a temp workspace with assertions on file state and tool-call counts) would give you a metric when you change edit mechanics or prompts. Hermes has `evals/`; opencode has `perf/`. This is the item most aligned with your goals (LLM-as-judge/eval background), but do it after #3 so you have something to measure.

### Skip, for now (premature)

- **OS sandbox.** With per-call approval, `shell=False`, and workspace confinement, the boundary you'd add (Seatbelt) mostly protects against *you approving something you didn't read*. Revisit when you add unattended modes (cron, `--yes`). If you do: generate a `sandbox-exec` profile from Codex's `seatbelt_base_policy.sbpl` as a template and wrap `_run` in `tools/cli.py`; note `sandbox-exec` is deprecated by Apple but is what Codex ships (`sandboxing/src/seatbelt.rs`).
- **Regex dangerous-command denylist** (Hermes). Their own security policy disowns it as a boundary; it only makes sense in a "run by default" model, which you don't have.
- **Parallel tool calls.** Your tools are fast and approval is serial by nature. Complexity (ordering, path overlap: Hermes `tool_dispatch_helpers.py`) is large for little latency win.
- **Subagents, MCP, hooks, plugins, skills.** No concrete consumer yet. Hermes's rubric makes the same call ("hooks with no concrete consumer" are rejected). If you need an external capability, one more file in `tools/` is cheaper than an MCP client.
- **Multi-provider adapters.** OpenRouter already is the abstraction. Revisit only if you need provider-specific features (Anthropic prompt caching, Responses compaction).
- **Client/server split.** Codex and opencode need it for multiple frontends. You have one.
- **SQLite sessions.** JSONL is fine at your scale and is greppable. Revisit for cross-session search (Hermes FTS5) only if you want it.

## Recommended next 3 steps

1. **Logging + retry together** (items 1, 2). Retry without logs is invisible; logs without retry just record failures. One PR, ~150 lines, tests via the existing fake-SSE helpers (`tests/test_runner.py:13-60`).
2. **New `edit` tool + diff in approval** (item 3) and the `rg --pre` fix (item 7) in the same sitting since both touch `tools/edit.py` / `tools/cli.py`. Add a test that `rg --pre` is rejected.
3. **Spill-to-file + old-output pruning** (item 4), then AGENTS.md loading (item 5). This is the change that makes long sessions work; do (a) spill first, which is independently useful.

Deliberately not building: OS sandbox, subagents, MCP, hooks, parallel tool calls, provider abstraction, LLM summarization compaction (until pruning proves insufficient).

## Open questions and unverified items

- Hermes loop bound: constructor default `sys.maxsize` (`run_agent.py:241`) vs docs "default 500" (`agent/AGENTS.md`, `iteration_budget.py:3-5`). I did not trace the CLI path to see which value reaches `AIAgent`.
- opencode "always" approval persistence: I saw only an in-memory `approved` list in `permission/index.ts`; the docs or config layer may persist elsewhere. Also did not search every package for OS sandboxing.
- opencode tool-call concurrency is inferred from `concurrency: "unbounded"` at `processor.ts:588`, not from reading the tool-dispatch code.
- Codex undo/ghost-snapshot: `git-utils` and a `tasks/mod.rs` comment mention "ghost snapshots"; I did not confirm a user-facing `/undo`.
- Codex: stateful `previous_response_id` behavior is read from `client.rs` module docs; I did not trace when it is used versus full-history resend.
- Codex AGENTS.md semantic details beyond the module header, the Guardian reviewer's trigger conditions, and the shell safe-command lists were not read in depth.
- Hermes `SECURITY.md` and approval modes: I read §2.1-2.4 and the first ~110 lines of `approval_detection.py`; the default `approvals.mode` value was not confirmed (code normalizes unknown values to `manual`, `approval_context.py:217-230`).
- `rg --pre` exploitability against your harness was reasoned from source, not executed.
- Claims about "what no one else does inline" (cost line) are from README/UI behavior I did not exhaustively check for all three projects; treat as opinion.
- Not covered: Claude Code, Aider, pi-mono, and the opencode/Codex official docs sites.
