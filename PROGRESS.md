# Progress log (append-only, <= 10 lines per task)

## P0 item 1 - repo hygiene (partial, done in chat sandbox)
- Added .gitignore, requirements.txt, requirements-dev.txt (from real imports; tzdata added for Windows zoneinfo).
- Fix: added python-multipart (FastAPI form/upload routes need it; found when pytest failed to collect test_proactive_integration).
- NOT verified: sandbox has no pytest/fastapi/internet, so `pytest` was not run. Run on your PC: python -m venv .venv; pip install -r requirements-dev.txt; python -m pytest tests -q
- Still TODO in P0: git init + first commit, items 2-6 (tool registry, tool routing, shell rewrite, prompt order, harness.md/timezone).
- Heads-up for item 2: src/lucy/tools.py already exists as a FILE; a package src/lucy/tools/ would clash with it. Needs a rename or a different package name.
- Heads-up: google_client_secret.json is in the zip; .gitignore covers it. Consider rotating it if the zip was ever shared.

## P0 item 6 (part) - harness.md drift + timezone note
- .core/harness.md: removed stale "max 8 tool rounds" / "max 2 reminders"; now describes no round cap, 30-round stall limit, plan nudges.
- .core/proactive.yaml: comment explaining quiet hours run in UTC until `timezone` is set (value left unchanged on purpose).
- Baseline on user's Windows PC: 168 passed, 1 failed (tests/test_stickers.py::test_media_marker_matches_send_file_convention, assumes POSIX path `MEDIA:/`; FIXED: regex now accepts `C:/` drive letters; re-run to confirm).

## P0 item 2, step 1 - tool registry skeleton (no tools moved yet)
- New package src/lucy/tools/: registry.py (auto-discovery, strict validation, sync handlers run in a thread), __init__.py,
  google_drive.py (= old src/lucy/tools.py, unchanged; `from lucy.tools import Toolset` still works). DELETE old src/lucy/tools.py.
- api.py (CRLF kept): registry schemas merged into TOOLS (replace same-name inline schema in place), execute_tool_call asks the
  registry first (after proactive/memory), activity label falls back to the registry label. Old if/elif code untouched.
- Tests added: tests/test_tool_registry.py (13 cases, run OK in sandbox), tests/test_tool_registry_api.py (2 cases, NOT run in sandbox: no fastapi).
- Next: step 2 batch A = move google tools (list_drive_files, create_google_doc, get_doc_content) into src/lucy/tools/.
- Open: Toolset.run_powershell_admin is unused and unguarded; do not carry forward (P1).

## P0 item 2, step 2 batch A - Google tools moved
- New src/lucy/tools/google_workspace.py (list_drive_files, create_google_doc, get_doc_content; tier "network", labels drive/docs/docs).
  Schemas verified identical to the old inline ones. Handlers still call google_drive.Toolset; now run in a worker thread (old code blocked the loop).
- api.py: removed the 3 inline schemas, the 3 if-branches and 3 activity-map entries. These 3 tools now sit at the END of the tool list (prompt order only).
- agent_loop.py references these names (read-only set, workaround hints): names unchanged, no edit needed.
- Tests added: 1 in test_tool_registry.py (ran OK in sandbox), 1 in test_tool_registry_api.py (not run in sandbox).
- Next: batch B = file tools (read_local_file, edit_local_file, write_local_file, list_local_files, find_file, send_file).

## P0 item 2, step 2 batch B - file tools moved
- New src/lucy/tools/files.py (read/edit/write/list/find/send_file; tiers read/write/write/read/read/read) generated from the old code:
  bodies copied as-is (only _resolve_path->resolve_path, SAFE_ROOT->_paths.SAFE_ROOT); schemas verified identical. Shared helpers in
  src/lucy/tools/_common.py. api.py shrank 3,637 -> 3,267 lines; removed 6 schemas, 6 branches, 3 activity-map entries.
- Tests: +1 in test_tool_registry.py (write/read/search/edit/list/send_file round trip; ran OK in sandbox), +1 in test_tool_registry_api.py (not run in sandbox).
  Relaxed my own description-length guard 300 -> 700 chars (several legacy descriptions are long; trimming belongs with model-aware descriptions).
- KNOWN ISSUE (unchanged on purpose): find_file shells out to `bash -c find ...`, so it fails on Windows without Git-Bash. run_shell_command same (item 4).
  Fix find_file in pure Python together with item 4.
- Next: batch C = web + media (web_search, web_extract, analyze_image, send_tts, send_sticker).

## P0 item 2, step 2 batch C - web + media tools moved
- New src/lucy/tools/web.py (web_search, web_extract + their helpers) and media.py (analyze_image, send_tts, send_sticker + _analyze_image helper).
  Tiers: web network, analyze_image read, send_tts exec, send_sticker read. Schemas verified identical. api.py now 2,897 lines.
- ToolContext gained `tool_names` (api passes the real tool-name list; send_tts needs it). Sticker library is built per call (stateless).
- BUG CAUGHT BY TESTS before shipping: generated handler names collided with helper names (handler `_web_search` shadowed helper `_web_search`
  -> infinite self-call). Handlers are now `_h_<tool>`; new test fails if any tool module defines a top-level name twice.
- Tests: +4 in test_tool_registry.py (ran OK in sandbox, httpx stubbed there), +1 in test_tool_registry_api.py (not run in sandbox).
- Remaining inline in api.py: run_shell_command, cua_* (batch D), memory + proactive adapters (batch E).

## P0 item 2, step 2 batch D - shell + cua tools moved
- New src/lucy/tools/shell.py: run_shell_command (tier exec, label run; still `bash -c`, 10 s, replaced in item 4) and cua_cursor/mouse/keyboard
  (tier system, label control; one shared dispatcher `_run_cua`, now sync so it runs in a worker thread). Schemas verified identical.
- api.py: 2,724 lines. execute_tool_call is now: proactive -> memory -> registry -> "Unknown tool". The if/elif chain is gone.
- Tests: +3 in test_tool_registry.py (ran OK in sandbox, subprocess/cua faked), +1 in test_tool_registry_api.py (not run in sandbox).
- Next: batch E = memory + proactive adapters into the registry, drop leftovers (TOOL_REGISTRY dict duplicates, dead imports), check api.py for unused imports.

## P0 item 2, step 2 batch E - memory + reminder tools registered; ITEM 2 DONE
- remember / recall_memory / forget / search_conversations / set_reminder / schedule_task / manage_reminders / proactive_control are registered as
  ADAPTERS (api.py `_register_adapter_tools`): their state (memory manager, sessions DB, current session, proactive engine) stays where it is.
  Conservative choice on purpose: moving that state out of api.py blind is risky; revisit when splitting api.py.
- execute_tool_call is now: registry -> "Unknown tool". `_TOOL_ACTIVITY_MAP` deleted (labels come from the registry).
- Tiers: remember/forget/reminder tools write, recall/search_conversations read. api.py 2,736 lines (it grew a little: the adapter block).
- Tests: +2 in test_tool_registry_api.py (not run in sandbox). New invariant: every tool in api.TOOLS is registered with tier+label+handler.
- api.py needs ~1,250 more lines out to hit the P0 "< 1,500" goal: candidates = _build_messages/prompt, voice/stt/tts endpoints, sessions DB, library/upload routes.
- Next: Item 3 (tool routing: select_tools + tool_search).

## P0 item 3 - tool routing for small models (two steps, one zip)
- Step 3a: src/lucy/tools/routing.py: ALWAYS set (read/write/edit file, remember, recall_memory, send_sticker), FAMILIES (regex on the user's words + last 2
  user messages -> tools), `select_tools`, `ToolCatalog` (search/get), `TOOL_SEARCH` schema. Typical chat turn: 8 schemas (6 + update_plan + tool_search)
  instead of ~33; worst case seen ~14.
- Step 3b: agent_loop.py: optional `tool_catalog`; `tool_search` handled inside the loop (adds the found schemas for the rest of the turn, counts as bookkeeping
  but capped at 4 like update_plan so it can't spin); a real tool called by name but not shown is auto-added and run (trace event `tool_auto_added`);
  the loop copies its tool list (the caller's list is never mutated). api.py: `_tools_for_turn` picks the tools per turn (skill_list still merges in);
  TTS filter still knows ALL tool names. Escape hatch: env LUCY_TOOL_ROUTING=0 = old behaviour (all tools every turn).
- harness.md updated (tools section, registry, routing, how to add a tool).
- Tests: tests/test_tool_routing.py (5 pure ran OK in sandbox; 5 fake-server loop tests NOT run here: no httpx, but the loop methods were exercised directly),
  +1 in test_tool_registry_api.py (not run in sandbox).
- Watch in real use: agent_trace.jsonl events `tool_search` / `tool_auto_added` show which tools the model needed that routing hid -> tune FAMILIES.
- Next: Item 4 (new shell tool: PowerShell, timeouts, background jobs; pure-Python find_file).

## P0 item 4 - new shell tool (two steps, one zip)
- Step 4a: src/lucy/tools/jobs.py: Popen + reader threads (works on any event loop, incl. Windows), per-job max runtime timer, kill of the whole process tree
  (taskkill /T /F on Windows, killpg elsewhere), incremental `read_new` with tail truncation, stdin closed (a prompt gets EOF, never a hang), env + cwd,
  JobManager (ids, list, prune: keeps 30 finished jobs for 1 h).
- Step 4b: src/lucy/tools/shell.py rewritten: `run_shell_command(action: run|start|poll|kill|list, command, job_id, shell, timeout, cwd, env, wait)`.
  Default shell PowerShell on Windows (bash elsewhere), `LUCY_SHELL` overrides; cmd supported. Default timeout 60 s, max 3600 s; a foreground `run` is
  capped at LUCY_TOOL_TIMEOUT-10 s (the loop abandons tools after that) with a note to use `start`. Failures start with "Command failed" so the loop counts
  them. Stop button -> task cancelled -> process killed. cua_* tools moved to src/lucy/tools/desktop.py (code unchanged).
- find_file is now pure Python (case-insensitive glob, 4 levels, skips .git/node_modules/venv/..., 20 results, 10 s cap): works without bash.
- Tests: tests/test_jobs.py (9) + tests/test_shell_tool.py (17) use real subprocesses with a fake "shell" (the Python interpreter), all ran OK in the sandbox;
  removed the obsolete subprocess.run-patching test; api shell test updated (not run in sandbox).
- SAFETY NOTE: the old tool was capped at 10 s with no background jobs; this one can run for an hour and in the background. There is still NO permission layer:
  do P1 before relying on it unattended (exec tier -> "Ask every time").
- Next: Item 5 (prompt order for KV-cache reuse + time-to-first-token script) and Item 6 (wrap-up).

## P0 items 5 + 6 - prompt order for the KV cache, measuring, wrap-up
- llm_manager.request_extras() now returns {"cache_prompt": True} (it returned {} before, so the server relied on its default).
- _build_messages: stable blocks first (persona, memory rules, harness digest, tool rules), then per-message facts + skills, then history, then the user
  message. Before, [USER FACTS] sat right after the persona, so everything behind it was re-read every turn. LUCY_PROMPT_ORDER = static_first (default) |
  late (facts+skills ride on the last user message: history stays cached too) | legacy (old order, for before/after measurements).
- The tool list is part of the prompt (chat templates put it first), so routing (item 3) could have cost more cache than it saved: tools now keep api.TOOLS
  order, tools picked in the last 3 turns of a chat stay in the list, and tools the model pulled in mid-turn (tool_search / by name) are remembered
  (AgentLoop `on_tools_added` -> api._note_added_tools).
- tools/measure_ttft.py: 3-turn chat against the running server, time to first token per turn, results in runtime/ttft_results.jsonl, `--compare`.
  NOT measured here (no GPU): run it twice per setting and compare (see README_FIRST in the zip).
- Tests: tests/test_prompt_order.py (8; need fastapi, not run in sandbox - the same logic was exercised by loading the real functions with stubs) and 1 loop test.
- docs: harness.md has a new "Prompt order and speed" section; CLAUDE.md map/rules refreshed (stale note removed).

### P0 STATUS: done except two stated goals
- NOT met: "api.py < 1,500 lines" (it is ~2,800, was ~3,600). Tools are out; what is left is prompt building, sessions DB, voice/TTS/STT endpoints, library/upload
  routes, settings. Suggested P0.5 if wanted: split those into routers. P1 does not depend on it.
- NOT done (needs the user's machine): `git init` + first commit (.gitignore is ready), TTFT before/after numbers.
- Summary for the user: Lucy shows ~8 tools instead of ~33 per turn (tool_search / auto-add for the rest); new tools = one file; the shell is PowerShell-native with
  timeouts, background jobs and Stop-kills-process; find_file works without bash; the prompt is cache-friendly. Everything is behind env knobs
  (LUCY_TOOL_ROUTING, LUCY_PROMPT_ORDER, LUCY_SHELL) so any of it can be switched off.
- NEXT: P1 (safety layer: permissions, approvals, audit, undo, constitution files). Recommended model: Opus 5.5.
