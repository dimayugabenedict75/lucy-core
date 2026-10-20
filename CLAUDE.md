# Lucy Core - standing instructions (read this first, every session)

Lucy Core is a local AI-agent harness: FastAPI server (port 8090) + web UI, driving **local GGUF models via
llama-server** (Lucy 12B on :8080, Lucy 4B on :8081). The runtime model is SMALL. You (the builder) are not.
Every design decision must hold up for a 4-12B model: few tools in the prompt, strict schemas, short descriptions,
constrained output, forgiving parsing, and harness-side guarantees instead of "the model will remember".

## Map (verify with grep before trusting; line numbers drift)
| Path | Role |
|---|---|
| `src/lucy/server/api.py` (~2,800 lines) | FastAPI app, SSE chat, sessions DB, `TOOLS` list (built from the registry), `execute_tool_call` (registry lookup), `_build_messages` (prompt, stable-first order), `_tools_for_turn` |
| `src/lucy/tools/` | One file per tool (SCHEMA/handler/TIER/LABEL or `TOOLS=[...]`), `registry.py` (auto-discovery), `routing.py` (which tools per turn, `tool_search`), `jobs.py` (process runner), `shell.py`, `files.py`, coding tools (`code_search.py`, `project_map.py`, `apply_patch.py`, `run_tests.py`, `git_tool.py`, `python_repl.py`, `lint.py`; helpers `_code.py`, `_verify.py`), `web.py`, `media.py`, `desktop.py`, `google_workspace.py`, Office (P3): `docx_tools.py`, `xlsx_tools.py`, `pptx_tools.py`, `pdf_tools.py`, `office_view.py`, helpers `_office.py`; PC (P5): `pc_admin.py` (service/process/winget/scheduled_task/registry/system_info), `pc_desktop.py` (window/desktop/screenshot/ui_click), `file_ops.py`, helper `_pc.py` |
| `src/lucy/broker/` | P5 admin helper (runs ELEVATED, on demand via UAC): `ops.py` (the fixed op menu, stdlib only), `server.py` (loopback + HMAC, hash-self-check), `client.py` (Lucy's side: start/talk/stop). CONSTITUTION |
| `src/lucy/mcp/` | P4 MCP client: `client.py` (stdio + streamable HTTP, stdlib only), `manager.py` (lazy servers, registry sync, arg guard, untrusted framing), `config.py` (`.core/mcp.json` + `runtime/data/mcp_state.json`), `schema.py` (names, compact schemas, results) |
| `src/lucy/selfdev/` | P7: `sandbox.py` + `_runner.py` (static check, child process, audit hook), `user_tools.py` (Lane A), `lane_b.py` (copy -> checks -> approve -> swap/rollback), `smoke.py` (canary + fake LLM), `trace_mining.py`, `__main__.py` (rollback CLI). CONSTITUTION. Lucy's tools: `src/lucy/tools_user/`; records `runtime/selfdev/` (agent-refused) |
| `src/lucy/server/agent_loop.py` | The loop: stream, validate/execute tools, retries, nudges, forced tool call, repeat guard, stall limits |
| `src/lucy/server/multistep.py` | `update_plan`, promise detection (`find_promise`), `expected_actions`, `finish_task` |
| `src/lucy/server/repeat_guard.py` | Streaming repetition guard (drops repeated sentences, cuts degenerate loops) |
| `src/lucy/server/library.py` | Content-addressed file cache (`cache/`), Library view API |
| `src/lucy/safety/` | P1 safety layer: policy (modes/tiers/rules), approvals, audit, undo backups, constitution lock, injection guard. CONSTITUTION |
| `src/lucy/server/file_guard.py` | Path resolution, tolerant edits, syntax checks, backups, "did you mean" paths |
| `src/lucy/proactive/` | Reminders, check-ins, follow-ups, the scheduler tick (`.core/proactive.yaml`); `cron.py` = cron repeat rules |
| `src/lucy/autonomy/` | P8 job queue: `store.py` (SQLite), `budget.py`, `gate.py` (park approvals), `runner.py` (JobRunner, run by the engine tick), `tools.py` (`task`), `web.py` (`/api/jobs/*`) |
| `src/lucy/memory/`, `src/lucy/rag/` | Long-term memory (`runtime/data/knowledge.sqlite`, SQLite+FTS5; `store.py`, `consolidate.py`; old JSON kept as backup), conversation search |
| `src/lucy/persona/` | P6: `self_store.py` (SELF file, caps, changelog, revert), `guard.py` (what may enter SELF), `loader.py` (CORE+SELF, hot reload), `reflect.py` (idle/nightly job + daily note). Not a constitution file |
| `.core/personas/lucy.md` | CORE persona (human-owned, constitution-locked). `lucy_self.md` = SELF (agent-writable via `update_self`). `.core/harness.md` = the agent's own reference doc |
| `ui/chat.{html,js,css}` | Web UI (the user is HAPPY with it: do not redesign; add only what a phase asks) |
| `runtime/` | sessions.sqlite, logs (`agent_trace.jsonl`), backups, data. Never commit. |
| `tests/` | pytest; scripted fake llama-server in `test_agent_loop.py` (no GPU needed) |

## Hard rules
1. **Tests gate everything.** `python -m pytest tests -q` must pass before and after each task. New behaviour gets a test
   (use the fake-server pattern in `tests/test_agent_loop.py`). Never weaken a test to make it pass; fix the code or
   say why the test is wrong.
2. **Small diffs.** Use `grep -n` + line ranges, not whole-file reads of api.py. Patch, do not rewrite files.
   One logical change per commit (`git init` first if there is no repo). Line endings differ per file: `src/**/*.py`, docs and tests are LF; `ui/chat.{html,js,css}` and `.core/personas/lucy.md` are CRLF. Check with Python (`open(p,'rb').read().count(b'\r\n')`), edit CRLF files in binary-safe mode, never rewrite a whole file in text mode.
3. **The loop never gives up on the user's task** (see agent_loop.py docstring): no step budget, failures trigger
   workarounds, only "done", user-stop, or 30 no-progress rounds end a turn. Do not reintroduce caps.
4. **Small-model friendliness is a requirement, not a nicety.** Tool descriptions <= 2 sentences. Prefer one tool with a
   few `action` values over five near-duplicate tools. Anything the harness can verify/enforce, enforce.
5. **Windows is the target OS** (paths like `C:/Users/...`, llama.cpp at `D:/llama.cpp`). Test paths/shell for Windows
   semantics; do not assume bash.
6. **Safety layer is the one thing the agent may not edit** (P1, `src/lucy/safety/`). Constitution files (hash-locked):
   `src/lucy/safety/*.py`, `src/lucy/tools/registry.py`, `src/lucy/tools/_common.py`, `src/lucy/server/file_guard.py`,
   `.core/permissions.yaml`, `.core/mcp.json` (P4), `.core/personas/lucy.md` (P6), `src/lucy/broker/*.py` (P5: this code runs as administrator), `src/lucy/selfdev/*.py` (P7). If YOU (the builder) change one, tell the user to run `seal.bat` (double-click; = `cd src && python -m lucy.safety seal`)
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
- Real-world finding: the user only needs background web search (built-in web_search/web_extract); the browser (Playwright, now `--headless`) is optional and a
  live test spammed tabs (re-called browser_navigate). If P5 or later touches the browser, add a repeat-navigate guard and read `agent_trace.jsonl` first.
- Status: P0-P5 done (431 tests green on the user's PC). Next: P6 (persona that grows), see PROGRESS.md "HANDOVER -> P6". Remember: after any change to a constitution file the user must run seal.bat.

## PC agent (P5 result)
- Lucy NEVER runs as admin. An elevated helper (`src/lucy/broker/`) runs a FIXED menu (`ops.py`) on demand: `client.py` writes a 32-byte
  secret, launches `server.py` via a Windows UAC prompt (`runas`), and talks to it over 127.0.0.1 with multiprocessing HMAC auth. The broker
  self-checks its own hashes against constitution.lock before serving, exits when idle (default 15 min) or when Lucy dies. Non-admin ops and
  reads run in Lucy's own process; `_pc.execute()` routes to the broker only when an op needs admin (or a protected path), and `process.kill`
  retries via the broker only after Windows says "access denied".
- New tools: `service`, `process`, `winget`, `scheduled_task`, `registry`, `system_info`, `file_ops`, `window`, `desktop`, `screenshot`,
  `ui_click`. All `system` tier except `system_info` (read) and `file_ops` (write). Every one takes `dry_run`. They register at the END of
  `api.TOOLS` and are kept out of the prompt by routing (new `pc_*` families); `describe_call()` in each module gives the approval card a plain summary.
- Safety: `policy.ACTION_TIERS` downgrades read actions; `HARD_ACTIONS` + `_reg_admin` force a card in every mode for kill/stop-service/uninstall/
  disable-task/registry-write; `DRY_RUN_TOOLS` makes a dry run read-tier. Whole-drive rules: `config.Config.drives`/`other_drive` +
  `drive_profile()`; `storage` makes deletes/moves-out ASK_HARD (via `_FILE_OPS_ROLES` and `_DELETE_VERBS` for shell). `_common.resolve_path`
  now also reaches `drive_roots()` so a new drive is usable without restart. `file_ops` to a readonly_paths location routes to the broker (ASK_HARD).
- Pause (Settings > Safety) also stops the broker. `/api/safety/broker` reports whether it is running; the Safety tab shows the drive profiles.
- Tests: `tests/test_broker.py` (18: ops menu, guard, loopback broker with an in-process launcher + fake runner), `tests/test_pc_tools.py`
  (tiers, admin routing, routing families, card summaries), `tests/test_p5_loop.py` (3, fake llama: read runs, control asks+runs, denial works around).
  Decisions used (user): drives C: ask / D: full / E: storage / new = storage; broker on demand (UAC).
- NOT testable here (no Windows): real PowerShell/winget/registry/UIA output, the real UAC launch, pywinauto. The op PowerShell/argv is checked via
  a fake runner. cua_* raw pixel tools are kept as a fallback. The broker uses loopback+HMAC (not a named pipe) so it is stdlib-only and testable.

## Earlier handover note (P8, now done; see "Tasks (P8 result)")
- Start: read this file, then PROGRESS.md from "HANDOVER -> P8", then the P8 prompt in OPUS_HANDOFF.md. Do ONLY P8.
- The user runs tests with `.venv\Scripts\python.exe -m pytest tests -q` (PowerShell blocks activate.ps1). Full suite takes ~4 min.
  Last result on the user's PC: 557 passed after P7; 558 expected after P7 fix 1 (confirm with the user).
- The sandbox cannot run the FastAPI tests or Windows-only behaviour: say so in PROGRESS.md and let the user's PC be the judge.
- Constitution edits cost the user a reseal: batch them. P8 should need NONE (job queue, scheduler and tools live outside
  safety/, broker/, selfdev/). If one turns out necessary, do it in ONE batch and say so up front.

## Persona that grows (P6 result)
- Prompt = CORE (`lucy.md`, locked) + SELF (`lucy_self.md`) via `persona.get_loader()`; both re-read on mtime change (`api._persona_text()`).
- SELF is written only through `SelfStore.edit` (tool `update_self`, the reflection job): fixed sections, caps, `guard.check_item`, daily limits, changelog, revert (user only, Settings > Persona / `/api/persona/*`). Hand edits are logged as `external_edit`; invalid lines never reach the prompt.
- Reflection (`persona/reflect.py`): proposals from the model are applied only through those guards; notes in `runtime/data/learned/`. Env: `LUCY_REFLECT=0` turns the automatic runs off.
- Memory is SQLite+FTS5; consolidation ARCHIVES (never deletes). Tests: `test_p6_persona.py`, `test_p6_memory.py`, `test_p6_reflect.py`, `test_p6_api.py` (needs FastAPI).

## Lessons that cost time (read before P7)
- `SelfError` (persona) subclasses `ValueError`: in any `try`, catch it BEFORE a generic `except (TypeError, ValueError)` or it is misreported (P6 revert-route bug).
- `tests/test_proactive_integration.py` uses the REAL `runtime/sessions.sqlite`; it can fail depending on the newest chat there (seen in P3 and P6). Apply the P3 cleanup; do not "fix" the product code for it.
- The builder sandbox has no pytest/fastapi/httpx (no network): a stand-in runner was used for pure tests; ALWAYS say what could not be run and let the user run `.venv\Scripts\python.exe -m pytest tests -q`.
- After any constitution edit (list in rule 6) the user must run `seal.bat`; batch such edits into ONE per phase.
- Test with PRODUCTION config, not empty defaults (P7 fix 1): the sandbox deny list was empty in the tests, so a real bug (the staging
  folder was itself denied -> every `user_tool create` failed) only showed on the user's PC. Build test objects through the same
  factory / config the app uses (e.g. `selfdev._deny_read()`, `safety.get()`), at least once per feature.
- Python gotcha: `str.lstrip("./")` strips CHARACTERS, not a prefix (it ate the dot of ".core/"); use a prefix loop / `removeprefix`.

## Self-evolution (P7 result)
- Lane A: tool `user_tool` (create|improve|list|disable; tier write, `list` read via policy.ACTION_TIERS; no card). Tools are NEVER imported into Lucy:
  `UserTools` registers a ToolSpec per enabled tool in `registry.default` whose handler runs `sandbox.run_job` (child process). Tier comes from
  `runtime/selfdev/user_tools.json` (default exec, user promotes in Settings > Evolve, floor = `needs`). Catalog only (like MCP): `api._user_tools_for_turn`,
  plus `ToolCatalog(live=...)` so a tool created mid-turn is callable in the same turn.
- Lane B: tool `core_change` (start|submit|status|discard|ideas; TIER exec for submit, the rest lower via ACTION_TIERS). `LaneB` freezes the change set at
  submit (hashes), runs `_default_tests` (pytest in the copy) + `smoke.run_canary`, approve = user-only route. Rollback = route or `rollback.bat`.
  Policy (builder's conservative default, user has not objected): every core change needs the approval click.
- Constitution batch (ONE reseal): constitution.py (locks + protects `selfdev/`), layer.py (`runtime/selfdev` in state_files), policy.py (ACTION_TIERS).
- Tests: `test_p7_sandbox.py` (incl. escape attempts), `test_p7_user_tools.py`, `test_p7_lane_b.py`, `test_p7_trace.py`, `test_p7_loop.py`, `test_p7_api.py` (FastAPI).
- NOT verifiable in the sandbox: the real canary (uvicorn + the full app), the real suite inside a copy (~3.5 min), Windows audit-hook/process behaviour.
- P7 fix 1: the sandbox runner always allows reading the job's own module; `runtime/selfdev` is NOT on the sandbox deny list.
- Status: P0-P7 done, tests green on the user's PC; real-world P7 checklist in progress (Lane A step 1 hit fix 1).
  Next: P8 (persistent job queue, autonomy). See PROGRESS.md "HANDOVER -> P8".

## Tasks (P8 result)
- Queue in `runtime/data/jobs.sqlite`; the proactive engine owns the `JobRunner` (`engine.jobs`, built in `_make_jobs`, ticked from `engine.tick`, stopped/checkpointed in `engine.stop`, recovered in `engine.start`). `ProactiveHost.job_hooks` (`autonomy.JobHostHooks`) is how api.py hands over the agent loop (`_jobs_make_agent`), the tool executor, persona, safety layer.
- Safety: `autonomy/gate.py` only CALLS the layer (decide / describe / gate). An "ask" PARKS the job (status `parked`, one approval in `jobs.park`); the user answers in Settings > Tasks (`/api/jobs/{id}/decision`); on resume `runner._repair` runs the approved call itself. NO constitution file was touched. Model-made `full` jobs start as `waiting_ok`. `jobs.blocked_tools` are refused inside jobs.
- Upkeep: `register_builtin(...)` handlers `reflect` + `trace_mining` replace the standalone loops unless `jobs.system_jobs: false` (api `_jobs_register_upkeep`). An empty summary = "nothing due", not recorded, not counted.
- Tests: `tests/test_p8_jobs.py` (real AgentLoop + fake llama + real safety layer). Pitfall: tests that cancel a run mid-way must not depend on how many scripted model replies were consumed (make the script order-independent).

## Handover note for the next session (P9)
- Status: P0-P8 done, 595 tests green on the user's PC. Start: read this file, then PROGRESS.md from "HANDOVER -> P9", then the P9 prompt in OPUS_HANDOFF.md. Do ONLY P9, one item per commit, in the order the prompt lists.
- P9 rules of thumb: keep the current UI look (add only what an item asks); put logic in importable modules with tests (api.py cannot be imported in the builder sandbox); avoid constitution edits (list in Hard rule 6) - if unavoidable, ONE batch + tell the user to run seal.bat; update `.core/harness.md` for every behaviour change.
- Reuse: the job queue (`engine.jobs`) for deep research; the parked-approval card pattern for plan mode; Settings tab helpers in chat.js; library.py for Library work.

## Desk features (P9 result)
- Package `src/lucy/desk/` (logic + tests, importable without the app) + `desk/web.py` router (`/api/projects`, `/api/desk/*`). api.py only wires it (`_desk_ctx`). No constitution file changed in P9.
- UI additions live at the END of `ui/chat.js` / `chat.css` (block "P9 desk") plus small `window.desk*` hooks in sendMessage / renderHistory / renderStoredMessage / loadLibrary. Keep CRLF.
- Plan mode = read-only tools + `plan_tool=False`; research = `desk/research.py` pipeline run as a chat turn; citations = `SourceCollector` footer; rewind = `Checkpoints` + `Backups.restore`.
- Tests: `test_p9_library.py`, `test_p9_desk.py`, `test_p9_api.py` (FastAPI). Status: see PROGRESS.md "P9".
