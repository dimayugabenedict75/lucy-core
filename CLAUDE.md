# Lucy Core - standing instructions (read this first, every session)

Lucy Core is a local AI-agent harness: FastAPI server (port 8090) + web UI, driving **local GGUF models via
llama-server** (Lucy 12B on :8080, Lucy 4B on :8081). The runtime model is SMALL. You (the builder) are not.
Every design decision must hold up for a 4-12B model: few tools in the prompt, strict schemas, short descriptions,
constrained output, forgiving parsing, and harness-side guarantees instead of "the model will remember".

## Map (verify with grep before trusting; line numbers drift)
| Path | Role |
|---|---|
| `src/lucy/server/api.py` (~2,800 lines) | FastAPI app, SSE chat, sessions DB, `TOOLS` list (built from the registry), `execute_tool_call` (registry lookup), `_build_messages` (prompt, stable-first order), `_tools_for_turn` |
| `src/lucy/tools/` | One file per tool (SCHEMA/handler/TIER/LABEL or `TOOLS=[...]`), `registry.py` (auto-discovery), `routing.py` (which tools per turn, `tool_search`), `jobs.py` (process runner), `shell.py`, `files.py`, `web.py`, `media.py`, `desktop.py`, `google_workspace.py` |
| `src/lucy/server/agent_loop.py` | The loop: stream, validate/execute tools, retries, nudges, forced tool call, repeat guard, stall limits |
| `src/lucy/server/multistep.py` | `update_plan`, promise detection (`find_promise`), `expected_actions`, `finish_task` |
| `src/lucy/server/repeat_guard.py` | Streaming repetition guard (drops repeated sentences, cuts degenerate loops) |
| `src/lucy/server/library.py` | Content-addressed file cache (`cache/`), Library view API |
| `src/lucy/server/file_guard.py` | Path resolution, tolerant edits, syntax checks, backups, "did you mean" paths |
| `src/lucy/proactive/` | Reminders, check-ins, background tasks (`.core/proactive.yaml`) |
| `src/lucy/memory/`, `src/lucy/rag/` | Long-term memory (`runtime/data/knowledge_store.json`), conversation search |
| `.core/personas/lucy.md` | Persona = system prompt. `.core/harness.md` = the agent's own reference doc |
| `ui/chat.{html,js,css}` | Web UI (the user is HAPPY with it: do not redesign; add only what a phase asks) |
| `runtime/` | sessions.sqlite, logs (`agent_trace.jsonl`), backups, data. Never commit. |
| `tests/` | pytest; scripted fake llama-server in `test_agent_loop.py` (no GPU needed) |

## Hard rules
1. **Tests gate everything.** `python -m pytest tests -q` must pass before and after each task. New behaviour gets a test
   (use the fake-server pattern in `tests/test_agent_loop.py`). Never weaken a test to make it pass; fix the code or
   say why the test is wrong.
2. **Small diffs.** Use `grep -n` + line ranges, not whole-file reads of api.py. Patch, do not rewrite files.
   One logical change per commit (`git init` first if there is no repo). Source files use CRLF: preserve line endings.
3. **The loop never gives up on the user's task** (see agent_loop.py docstring): no step budget, failures trigger
   workarounds, only "done", user-stop, or 30 no-progress rounds end a turn. Do not reintroduce caps.
4. **Small-model friendliness is a requirement, not a nicety.** Tool descriptions <= 2 sentences. Prefer one tool with a
   few `action` values over five near-duplicate tools. Anything the harness can verify/enforce, enforce.
5. **Windows is the target OS** (paths like `C:/Users/...`, llama.cpp at `D:/llama.cpp`). Test paths/shell for Windows
   semantics; do not assume bash.
6. **Safety layer is the one thing the agent may not edit** (see OPUS_HANDOFF.md, "Constitution files"). Never add a
   code path that lets a model-issued tool call modify permissions, approval logic, or the guard itself.
7. **Secrets**: `google_client_secret.json`, API keys, `runtime/` must stay out of git and out of zips. Check `.gitignore`.
8. Update `.core/harness.md` whenever behaviour it describes changes (the agent reads it to understand itself).
   (Brought up to date in P0; keep it that way.)

## Working style (this is how we keep cost down)
- Do ONE phase prompt at a time from `OPUS_HANDOFF.md`. Do not start the next phase uninvited.
- Keep `docs/PROGRESS.md` (append-only, <= 10 lines per task: what changed, files touched, tests added, open issues)
  so a fresh session can resume without re-reading the codebase.
- Before coding a phase: write a plan of <= 15 lines, list the files you will touch, then proceed. Ask the user only at
  the decision points the phase prompt lists; otherwise choose the conservative option and note it in PROGRESS.md.
- Finish each phase with: tests green, harness.md updated, a 10-line summary of what the user can now do and how to try it.

## Commands
- Tests: `python -m pytest tests -q`   (fast subset while developing: `python -m pytest tests/test_tool_registry.py tests/test_tool_routing.py tests/test_jobs.py tests/test_shell_tool.py tests/test_prompt_order.py -q -x`)
- Run server: see `README`/`PROACTIVE_README.md`; env `LUCY_ROOT`, `LUCY_SAFE_ROOT`, `DEV_API_KEY`, `LUCY_MAX_TOOL_ROUNDS` (0 = none), `LUCY_CACHE_DIR`.
- Trace of every tool call / nudge / forced call: `runtime/logs/agent_trace.jsonl` (mine it when debugging agent behaviour).

## Adding a tool (P0 result)
Drop ONE file in `src/lucy/tools/` exporting `SCHEMA`, `handler(args, ctx)`, `TIER`, `LABEL` (or `TOOLS = [...]`), then add its keywords to `FAMILIES` in `tools/routing.py` so it is shown when relevant. Env knobs: `LUCY_TOOL_ROUTING=0` (show all tools), `LUCY_PROMPT_ORDER`, `LUCY_SHELL`.
