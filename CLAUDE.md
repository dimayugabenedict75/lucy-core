# Lucy Core - standing instructions (read this first, every session)

Lucy Core is a local AI-agent harness: FastAPI server (port 8090) + web UI, driving **local GGUF models via
llama-server** (Lucy 12B on :8080, Lucy 4B on :8081). The runtime model is SMALL. You (the builder) are not.
Every design decision must hold up for a 4-12B model: few tools in the prompt, strict schemas, short descriptions,
constrained output, forgiving parsing, and harness-side guarantees instead of "the model will remember".

## Map (verify with grep before trusting; line numbers drift)
| Path | Role |
|---|---|
| `src/lucy/server/api.py` (~2,800 lines) | FastAPI app, SSE chat, sessions DB, `TOOLS` list (built from the registry), `execute_tool_call` (registry lookup), `_build_messages` (prompt, stable-first order), `_tools_for_turn` |
| `src/lucy/tools/` | One file per tool (SCHEMA/handler/TIER/LABEL or `TOOLS=[...]`), `registry.py` (auto-discovery), `routing.py` (which tools per turn, `tool_search`), `jobs.py` (process runner), `shell.py`, `files.py`, coding tools (`code_search.py`, `project_map.py`, `apply_patch.py`, `run_tests.py`, `git_tool.py`, `python_repl.py`, `lint.py`; helpers `_code.py`, `_verify.py`), `web.py`, `media.py`, `desktop.py`, `google_workspace.py`, Office (P3): `docx_tools.py`, `xlsx_tools.py`, `pptx_tools.py`, `pdf_tools.py`, `office_view.py` (read + render), helpers `_office.py` |
| `src/lucy/mcp/` | P4 MCP client: `client.py` (stdio + streamable HTTP, stdlib only), `manager.py` (lazy servers, registry sync, arg guard, untrusted framing), `config.py` (`.core/mcp.json` + `runtime/data/mcp_state.json`), `schema.py` (names, compact schemas, results) |
| `src/lucy/server/agent_loop.py` | The loop: stream, validate/execute tools, retries, nudges, forced tool call, repeat guard, stall limits |
| `src/lucy/server/multistep.py` | `update_plan`, promise detection (`find_promise`), `expected_actions`, `finish_task` |
| `src/lucy/server/repeat_guard.py` | Streaming repetition guard (drops repeated sentences, cuts degenerate loops) |
| `src/lucy/server/library.py` | Content-addressed file cache (`cache/`), Library view API |
| `src/lucy/safety/` | P1 safety layer: policy (modes/tiers/rules), approvals, audit, undo backups, constitution lock, injection guard. CONSTITUTION |
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
6. **Safety layer is the one thing the agent may not edit** (P1, `src/lucy/safety/`). Constitution files (hash-locked):
   `src/lucy/safety/*.py`, `src/lucy/tools/registry.py`, `src/lucy/tools/_common.py`, `src/lucy/server/file_guard.py`,
   `.core/permissions.yaml`, `.core/mcp.json` (P4: every server entry is a program that runs on the PC). If YOU (the builder) change one, tell the user to run `seal.bat` (double-click; = `cd src && python -m lucy.safety seal`)
   after copying it in, otherwise the agent refuses all tools. Never add a code path that lets a model-issued tool call modify
   permissions, approval logic, or the guard itself. Minimise constitution edits: each one costs the user a reseal.
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

## Safety layer (P1 result) - what every new tool must respect
- Every tool runs through `registry.ToolSpec.run` -> `safety.before/after` (hard rules, undo snapshot, untrusted framing, audit).
  The agent loop's `gate` asks the user (approval card). Nothing to wire per tool: give it the right `TIER` and path arg names.
- The policy only sees paths in these arg names: `file_path, dir_path, image_path, path, cwd, folder, directory, source, destination,
  dest, target` (`policy.PATH_KEYS`). A tool that writes files under another name (e.g. a multi-file patch) must expose its
  target paths through one of these, or get an explicit rule; otherwise writes bypass path rules + snapshots. Changing
  PATH_KEYS is a constitution edit.
- `effective_tier()` in policy.py downgrades harmless actions (shell poll/list/kill) to read; new action-style tools may need an entry.
- Results of untrusted tools (`untrusted.tools` in permissions.yaml) are wrapped `[UNTRUSTED CONTENT ...]`; tests use `taint.unwrap`.
- Tests: use `tests/_safety_helpers.Env(mode, monkeypatch)` for a temp safety layer (restored automatically). Loop-level
  approval tests: `tests/test_safety_loop.py` (`drive()` answers cards).
- User controls: Settings > Safety (mode, pause, undo list); `runtime/logs/audit.jsonl`; `seal.bat`.

## Coding tools (P2 result)
- Seven tools in `src/lucy/tools/` (see harness.md). None needed a constitution edit: `apply_patch` takes ONE `file_path`, `git`/`lint` take `path`, `run_tests`/`python` take `cwd`
  (all in `PATH_KEYS`). Tools that run programs take NO free-form command (the policy only reads shell commands); args are typed and go to the program as argv items.
- Loop: parallel read-only calls (`PARALLEL_TOOLS`, `LUCY_PARALLEL`), harness VERIFY after edits (`LUCY_VERIFY`), coding-mode prompt note (`routing.CODING_ADDENDUM`).
- Tests: `tests/test_code_tools.py`, `tests/test_p2_loop.py`.
- Status: P0-P3 done (P3 has one open issue: Word rejects generated .docx, see PROGRESS.md; Google Docs / LibreOffice open them). P4 (MCP) is done, see below.

## Office tools (P3 result)
- Nine tools (see harness.md). Input is markdown or structured blocks/sheets/slides; the model never writes library code. `_office.py` holds the shared parts
  (forgiving block normaliser, markdown parser, atomic save, MEDIA delivery, LibreOffice discovery, formula-error check).
- A tool returns `MEDIA:<path>` when fine (file goes to chat + Library) or a plain "Saved ... NOT sent to the chat yet, because ..." text when it found a problem.
- Safety: all write tools use `path` (in PATH_KEYS). Pictures named INSIDE the args are checked with `_office.check_read` (policy.decide). No constitution edit.
- Routing: `routing.FAMILIES` office_* families (noun alone -> office_read; noun + create/edit verb -> that tool). Guidance: `.core/skills/definitions/office/SKILL.md` via `routing.office_guidance()`.
- Tests: `tests/test_office_tools.py` (needs python-docx, openpyxl, python-pptx, reportlab, pypdf, pypdfium2; LibreOffice-only tests skip themselves), `tests/test_p3_loop.py`.

## MCP (P4 result)
- Servers: `.core/mcp.json` (constitution file; user edits + `seal.bat`). Tools are registered in `registry.default` as `server__tool` by `mcp.McpManager`
  (tier from the config, default `network`), so the normal gate / hard rules / audit apply. They are NOT in `api.TOOLS` and never in the prompt: `api._mcp_for_turn`
  feeds them to `ToolCatalog` (tool_search / auto-add by name); the tool_search description names the connected servers (`routing.tool_search_schema`).
- Extra guards live in the manager, not in the constitution: `check_args` (paths in lists / nested args / `file:` URLs vs the denied rules), results framed as
  untrusted + remembered by the taint store. Known gap: `taint.suspicious` does not look at `network`-tier calls, so a URL copied from a page can be opened
  without a card (same as `web_extract`); typing / forms are `exec` tier and DO escalate when the text came from a page.
- Tests: `tests/test_mcp.py` (44: real fake server over stdio, local HTTP server, manager, safety), `test_mcp_loop.py` (fake llama), `test_mcp_api.py` (FastAPI).
  `tests/fake_mcp_server.py` is the scripted server. Register test managers in `registry.default` (`make(env, use_default=True)`): the gate looks tiers up there.
- Status: P0-P4 done. Next: P5 only after the user asks (admin broker).
