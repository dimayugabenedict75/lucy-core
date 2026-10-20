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

## P1 step 1 - safety package + enforcement points
- New src/lucy/safety/: config.py (permissions.yaml + defaults), policy.py (4 modes x 5 tiers, levels auto/ask/ask_hard/deny, built-in
  deny/ask command regexes, path + host rules), approvals.py, audit.py (append-only, secrets masked), backups.py (snapshot before
  write, restore, Recycle Bin), constitution.py (hash lock, trust-on-first-start, CRLF-proof), taint.py (untrusted frame + copy
  detection), layer.py (gate for the loop; before/after hooks for the registry), __main__.py (user CLI: status/seal/backups/restore).
- registry.ToolSpec.run calls safety before/after for EVERY execution (hard rules, snapshots, taint wrap, audit). The loop's gate asks.
- agent_loop: optional `gate`; side-event channel streams approval cards while the call waits; denial = result, no workaround playbook.
- New tool restore_from_backup (own chat only; asks for protected targets) + "undo" routing family. Shell jobs no longer inherit DEV_API_KEY.
- Tests: test_safety_policy.py (23, ran OK in sandbox). 2 registry tests now unwrap the untrusted frame (intended behaviour change).

## P1 step 2 - api/UI wiring; P1 DONE
- api.py: safety.install() at startup; chat refuses to start the agent (SSE error with the reason) if the constitution is broken;
  gate passed to the chat loop; background tasks get a no-approver gate; approval_request/resolved SSE events; endpoints
  /api/safety/{status, approvals/{id}, mode, pause, backups, backups/{id}/restore, audit}. Proactive engine skips ticks while paused.
- UI: approval card (summary, why, diff/command, Approve / Deny / Always allow), "Waiting for your OK..." label, Settings > Safety tab
  (mode, pause, status, recent changes with Restore). CRLF kept.
- Tests: test_safety_loop.py (9, fake server, ran OK in sandbox: approve, deny, timeout, always-allow, Stop, background, hard deny,
  injection in Full auto, pause). api endpoints smoke-tested with a stubbed FastAPI only: RUN THE REAL SUITE on your PC.
- Defaults chosen (decision points still open): mode ask_for_risky; read = home + Lucy Core; write = Lucy Core + ~/Documents.
- Known limits: shell commands can't be path-confined (they ask unless auto_in_allowed/full_auto); in Full auto a shell command could
  still edit files outside the lock list; the runtime hash check halts the agent if it touches a constitution file. cua_* always ask
  (slow until P5). lucy.md is not yet a constitution file (P6 splits CORE/SELF). Optional: pip install send2trash.
- Next: P2 (coding tools). Before that: user answers mode + allowed folders, edits permissions.yaml, runs `python -m lucy.safety seal`.

## P1 follow-up - whole-drive scope (user decision: C:, D:, E:)
- permissions.yaml: read C:/ D:/ E:/; write C:/ D:/; E: (backup HDD) + AppData + Lucy's src/ui/.core always ask; system folders
  read-only (Windows, Program Files, ProgramData, Recovery, Boot, page/hiber files...); secrets denied (Credentials, DPAPI Protect,
  Vault, browser profiles, Discord/Telegram tokens, password managers, wallets, Steam config, SAM hives, .ssh/.aws/...).
- tools/_common.resolve_path now also searches every folders.read root (an existing file in another root beats a guess);
  /api/file serves files outside SAFE_ROOT when the safety layer allows reading them (send_file from D:/E: works).
- Tests: +2 (Windows path rules for the shipped yaml via ntpath; multi-root resolution). Mode kept at ask_for_risky.


## P1 fix - Windows test run (4 failures)
- Real bug: readonly "C:/Documents and Settings" is a junction; realpath() resolved it to C:/Users, making ALL user folders read-only.
  Removed it and "C:/Users/All Users" from permissions.yaml; config.load now reports any readonly/denied entry that covers a write folder.
- Test isolation: safety Env now installs its layer via monkeypatch (restored even when a test fails); that leak caused the 2 Google failures.
- outside_root message test compares case-insensitively (Windows paths are normalised to lower case). +1 regression test.

## P1 fix 2 - Lucy thought she could not write to D:/E:
- Cause: file-tool descriptions + the harness digest still said "only under <SAFE_ROOT>" (C:/Users/<you>); the tools themselves worked.
- _common.allowed_roots_text() / safety_hint() now feed files.py descriptions and harness_info (e.g. "anywhere under C:/, D:/, E:/").
  Descriptions are built at startup: after editing folders.read, restart Lucy Core. _common.py is a constitution file: reseal. +1 test.

### P1 STATUS: DONE (280 tests green on the user's Windows PC)
- User decisions: mode ask_for_risky; read C:/ D:/ E:/; write C:/ D:/ (system folders read-only, AppData asks); E: (backup HDD) asks.
- Added seal.bat in the root (user found `python -m lucy.safety seal` needs `cd src` first). Sealing works; user finds it strict but OK.
- Residual risks (documented): shell can't be path-confined; Full auto shell could edit unlocked files; cua_* asks every action (P5).

## HANDOVER -> P2 (coding tools). Start here.
- Read CLAUDE.md (esp. "Safety layer" section), then the P2 prompt in OPUS_HANDOFF.md. No git repo on the user's PC yet.
- Tiers to use: code_search/project_map read; apply_patch write (expose target file paths via `file_path`, or a list in `path`
  per file -> see CLAUDE.md PATH_KEYS note; describe() in safety/layer.py builds approval diffs for write/edit only: apply_patch
  cards will show raw args unless you add a preview, which is a constitution edit -> batch all safety edits into ONE reseal);
  run_tests/python REPL/format_and_lint exec (they will ask in ask_for_risky: consider poll-style actions as read in effective_tier);
  git status/diff/log read, commit/checkout/stash write.
- Parallel READ calls (agent_loop): gate must still run per call; `_with_side_events` handles ONE call -> merge queues if you gather.
- VERIFY step after code edits: run inside the harness (not as a model tool call) so it needs no approval; append to the tool result.
- api.py is ~2,900 lines; keep edits small (CRLF). ui/ CRLF too.

## P2 - coding tools, parallel reads, VERIFY (DONE; no constitution file touched -> NO reseal needed)
- New tools (src/lucy/tools/): code_search (rg if present else Python; skips blocked/secret paths), project_map (cached outline), apply_patch (edits or unified diff, one file, atomic),
  run_tests (pytest/npm/dotnet auto-detect, condensed failures, long runs continue as a shell job), git (local actions only), python (per-chat REPL confined by an audit hook), format_and_lint. Helpers _code.py, _verify.py.
- agent_loop: parallel READ calls (PARALLEL_TOOLS, gate lock keeps approval cards one at a time), VERIFY hook after edit/write/apply_patch (LUCY_VERIFY=syntax|tests|off), playbook + claim-check entries for the new tools.
- routing.py: code_* families + CODING_ADDENDUM injected by api._stream_response on coding turns. multistep: apply_patch counts as "change the file".
- Tests: +27 (test_code_tools 18, test_p2_loop 9). Suite in sandbox: 252 passed; 4 files need FastAPI (not installable here): test_proactive, test_proactive_integration, test_prompt_order, test_tool_registry_api -> RUN THE FULL SUITE ON YOUR PC.
- Changed an existing test: test_file_claim_needs_a_successful_edit (message now lists apply_patch too; asserts the names instead of one exact string).
- Not testable here: real pytest/ripgrep/npm/dotnet/ruff output (condenser tested on canned output; rg via a fake emitting rg's JSON); Windows paths for the new tools.
- Open: apply_patch/git/python approval cards show raw args (describe() is constitution: batch with a future reseal). Poll for run_tests is via run_shell_command poll (effective_tier is constitution).
  Coding turn shows up to ~15 tool schemas (goal ~10): tune FAMILIES from trace events tool_search/tool_auto_added. python REPL: audit hook is a guard rail, not a sandbox.
- Next: P3 (Office documents).

### P2 follow-up - verified on the user's Windows PC
- Full suite on the PC: 306 passed, 1 failed (test_independent_reads_run_side_by_side: total-time < 1.5 s assertion was machine-speed dependent). FIXED: the test now counts concurrent reads (peak == 3) instead of timing. test_p2_loop.py: 9 passed on the PC.
- The 4 FastAPI-dependent test files ran and passed on the PC. P2 is DONE. Start P3 in a NEW session: read CLAUDE.md + this file, then the P3 prompt in OPUS_HANDOFF.md.


## P3 - Office documents (DONE in sandbox; run the full suite on your PC; NO constitution file touched -> NO reseal)
- New tools (src/lucy/tools/): docx_create/docx_edit, xlsx_create/xlsx_edit, pptx_create/pptx_edit, pdf_create, office_read, office_render; helper _office.py. Input = markdown
  or structured blocks/sheets/slides. Houses: Word report|resume (+letter|a4), one 16:9 deck theme, PDF with a Unicode TTF (Arial/Segoe/Calibri/Liberation/DejaVu, LUCY_PDF_FONT).
- Delivery: success -> `MEDIA:` (chat + Library; a path with spaces is delivered as an underscore copy). Problems (formula errors via LibreOffice, crowded slide, missing font) ->
  plain text, file saved but NOT delivered, model fixes and re-calls. Edits are all-or-nothing; xlsx_edit refuses charts/pivots unless allow_chart_loss.
- Wiring: routing.py office_* families (noun + create/edit verb) + office_guidance() (.core/skills/definitions/office/SKILL.md, hot reload) injected by api._stream_response; agent_loop: office_read
  parallel + read-only, DELIVERS_FILES (a delivered file counts as send_file, so no nudge), claim-check + playbook entries; multistep: "change the file" accepts the new tools.
- Tests: test_office_tools.py (36, all passed in sandbox incl. LibreOffice ones), test_p3_loop.py (4; only the pure one ran here: no httpx). Existing runnable tests identical before/after.
- Decisions: pdf_extract folded into office_read (CLAUDE.md rule 4); office_render is tier "read" (writes only runtime/tmp); markdown input added next to blocks (best for 4-12B models);
  multistep "send the file" set left as {send_file} because test_persistence asserts it. Bugs caught by tests: numbering restart only on first item; blocked-image error lacked slide/block no.
- Not testable here: Windows fonts/paths, real Word/PowerPoint opening the files (LibreOffice rendering looked right), the real model's use of the tools.
- Watch in agent_trace.jsonl: tool_search / tool_auto_added for office tools (tune FAMILIES); a coding/office turn shows ~12-14 schemas (goal ~10). Old .doc/.xls need LibreOffice.
- Next: P4 (MCP client + browser).

- P3 follow-up: office guidance moved to .core/skills/definitions/office/SKILL.md (old .core/skills/office/ still read as a fallback). The skills manager reads src/lucy/skills/definitions, a different folder, so nothing rewrites it.

- P3 fix (found by the user opening a file in real Word: "unreadable content"): `_ordered_insert` relied on python-docx `_tag_seq`, which does not exist, so pBdr / tblBorders / shd / numPr were appended
  OUT of schema order. LibreOffice tolerates that, Word does not. Now explicit SCHEMA_ORDER tables (pPr, tblPr, tcPr, trPr), footer field in separate runs, settings zoom percent set.
  Verified with the official XSD validator on report / resume / edited docx and on pptx (decks, charts, image, edits): all pass. New test checks the order (and fails on the old behaviour).
  Lesson: LibreOffice rendering is NOT proof Word opens a file; validate generated files against the OOXML schemas.

- P3 fix 2 (PowerPoint, found by auditing after the Word bug): pptx_edit delete-then-add produced TWO parts named slideN.xml in the file (new slide named after the slide count, old slides never renumbered):
  PowerPoint offers to repair that, the schema validator cannot see it. Now slide parts are renumbered after every delete and before saving. New test checks duplicate names, dangling links and slide count.
  Excel: xlsx_tools writes no raw XML (openpyxl API only); no XSD validator exists for xlsx here, so it is verified by reopening in openpyxl + LibreOffice, NOT in real Excel.

### P3 OPEN ISSUE - Word rejects the generated .docx ("unreadable content"), Google Docs / LibreOffice open it fine
- Reported by the user on their PC (Word). The OOXML schema validator (docx skill `scripts/office/validate.py`) PASSES report / resume / edited files, and the one real bug found on the way
  (properties out of schema order) is fixed, but Word still complains: the remaining cause is NOT visible from the sandbox (no Word here). User decided to live with it for now (Google Docs is their main viewer).
- NOT yet tried: the failing file itself; a minimal `# Hi` doc; bisecting by feature. Suspects, in order: footer PAGE field (fldChar runs), numbering.xml additions (_new_restarted_num: w:num appended + lvlOverride),
  tblBorders/tblLayout fixed + cell widths, docProps/core.xml (empty title, author), style edits in styles.xml (rFonts attrs, w:pBdr on Title/Heading 1), page size / margins (Inches of floats), leftover empty paragraph after tables.
- Next step when picked up: get the failing .docx; unzip; remove features one at a time (footer, numbering, tables, styles) until Word opens it; add a test for the culprit. A real Word / `docx` (Open XML SDK) validation would settle it.
- Same caution for .pptx / .xlsx: they pass every check available here (XSD for pptx, package integrity, reopen in openpyxl/LibreOffice) but are NOT confirmed in real PowerPoint / Excel.

## HANDOVER -> P4 (MCP client + browser). Start here.
- State: P0-P3 done. Suite on the user's PC: all green after the P3 fixes (347 + the 2 newer office tests; run `python -m pytest tests -q`). No constitution file touched in P3 -> no reseal pending.
- Read CLAUDE.md (esp. "Adding a tool", "Safety layer", "Office tools"), then the P4 prompt in OPUS_HANDOFF.md. P4 asks the user only: which MCP servers first. Tools from MCP servers must go through tool_search (not all in the prompt),
  default tier "network", respect the injection guard; `mcp.json` exists but holds an OAuth client id (never commit secrets).
- Test hygiene lessons from P3: (1) tests/test_proactive_integration.py used the REAL runtime/sessions.sqlite when run in the full suite (lucy.paths already imported); now uses unique session ids + purge. Any new api-level test must do the same.
  (2) Never leave a safety default layer pointing at a deleted temp dir: call safety.set_default(None) in teardown. (3) LibreOffice rendering is not proof that Word opens a file.
- Env on the user's PC: Windows, Python 3.12 venv, LibreOffice may or may not be installed (optional), user mostly uses Google Docs/Drive; models run via llama-server (12B on :8080, 4B on :8081).
- Real-model testing of the Office tools (routing, retries, MEDIA delivery) is still pending: watch agent_trace.jsonl for tool_search / tool_auto_added on office tools.


## P4 - MCP client + browser (DONE; 403 tests green on the user's PC; RESEAL with seal.bat after copying)
- New src/lucy/mcp/ (client.py stdio + streamable HTTP, stdlib only; manager.py lazy servers, idle stop, registry sync, arg guard; config.py; schema.py). Tools register as `server__tool` in registry.default, tier from .core/mcp.json (default network), never in the prompt: api._mcp_for_turn -> ToolCatalog (tool_search / auto-add); tool_search description names connected servers; the search answer lists a server's other tools (routing.ToolCatalog keywords/core/more_hint, 3-line hook in agent_loop).
- ONE constitution edit: safety/constitution.py also locks .core/mcp.json (a server entry = a program that runs on the PC). Shipped .core/mcp.json: playwright (--isolated --headless), github (remote, needs GITHUB_TOKEN), filesystem, google-workspace (user's entry); ALL enabled:false. UI toggle (runtime/data/mcp_state.json) needs no reseal.
- Guards in the manager: untrusted framing + taint, paths in lists/file: URLs vs denied rules, MEDIA: neutralised, nothing launches while the constitution is broken. Browser tiers: snapshot/screenshot read; navigate/click network; type/fill exec; evaluate/upload system.
- UI: "MCP servers" table in Settings > Connectors (list, on/off, refresh, last error). api: /api/mcp/status, /{key}/enabled|refresh|stop. Tests: test_mcp.py (44), test_mcp_loop.py, test_mcp_api.py, fake_mcp_server.py. test_proactive_integration.py now uses unique session ids (the user had an older copy with fixed ids that broke on leftover rows in the real runtime DB).
- Real-world result: Lucy's built-in web_search/web_extract already cover the user's need (search in the background, report results), so browser typing/forms was NOT pursued. A live Playwright test opened MANY google.com tabs (model kept re-calling browser_navigate, never snapshot/typed). Cause not diagnosed (trace not collected; model unknown). Not fixed.
- Open: (1) repeat-navigate guard (same URL/tabs piling up) if the browser is used again: collect runtime/logs/agent_trace.jsonl first. (2) Unverified offline: GitHub URL, Playwright flags, npm name @modelcontextprotocol/server-google-workspace; real Google MCP never run (Lucy's own Google tools remain; do not replace them without an end-to-end test). (3) taint.suspicious ignores network tier (a URL copied from a page opens without a card). (4) Server tool descriptions could carry injection. (5) No OAuth/SSE/resources for MCP. (6) Needs Node.js for npx servers.
- Next: P5 (Windows PC agent, admin broker). Needs P1 (done). Ask the user ONLY: which drives/folders are in scope, and broker as a service or on demand.

## P5 - Windows PC agent + admin broker (DONE in sandbox; RESEAL with seal.bat; run the full suite on your PC)
- Admin broker src/lucy/broker/ (CONSTITUTION, locked): ops.py = the FIXED op menu (stdlib only: services, process kill, winget, scheduled tasks, registry read/write,
  disk/firewall/events/updates info, fs copy/move/delete-to-Recycle-Bin/mkdir; strict arg checks, dry_run, protects core services/processes/registry keys & drive roots & Lucy Core).
  server.py runs it ELEVATED on demand (UAC), 127.0.0.1 + multiprocessing HMAC auth, self-hash-check vs constitution.lock, exits idle/parent-gone. client.py = Lucy's side.
- Tools (src/lucy/tools/, registered at END of api.TOOLS, kept out of the prompt by new pc_* routing families): service, process, winget, scheduled_task, registry, system_info (read),
  file_ops (write), window, desktop, screenshot, ui_click (UIA click by name; needs pywinauto). All system tier bar the two noted. Each takes dry_run. describe_call() -> plain card summary.
  _pc.py routes admin ops to the broker, reads/non-admin to Lucy's own process; process.kill retries via broker only on "access denied". Lucy herself is never run as admin.
- Safety (ONE batch of constitution edits -> reseal): policy.ACTION_TIERS (read actions auto), HARD_ACTIONS + _reg_admin (kill/stop/uninstall/disable/registry-write always ask, every mode),
  DRY_RUN_TOOLS (dry run = read). Whole-drive rules in config (drives/other_drive/drive_profile/present_drives): user decision C: ask, D: full, E: storage, new drives = storage.
  storage = deleting or moving OUT always asks (file_ops _FILE_OPS_ROLES + _DELETE_VERBS for shell). _common.resolve_path reaches drive_roots() so a NEW drive works without restart.
  file_ops to a readonly system folder -> broker (ASK_HARD, UAC). constitution.py now locks src/lucy/broker/*.py. layer.describe uses each PC module's describe_call.
- permissions.yaml: new `drives:` block (C ask / D full / E storage / other storage); folders.read/write now just EXTRAS on top of the drive rules; dropped the old E: ask_paths line.
- UI (minimal, existing style): Settings > Safety shows the drive profiles + an "Admin helper: running/not running" line (/api/safety/broker). Pause now also stops the broker.
  Approval cards already render PC actions via describe_call -> summary. chat.js re-normalised to CRLF.
- Tests added: test_broker.py (18), test_pc_tools.py (8), test_p5_loop.py (3). Sandbox (no fastapi/httpx -> shimmed): 372 passed; the 5 FastAPI-only files still need your PC.
- NOT testable here (no Windows): real PowerShell/winget/registry/UIA, the real UAC launch, pywinauto. Op PowerShell/argv checked via a fake runner. cua_* kept as a pixel fallback.
- RESEAL REQUIRED: constitution files changed (policy.py, config.py, layer.py, constitution.py, _common.py, permissions.yaml, + new src/lucy/broker/*.py). Copy in, then double-click seal.bat.
- Next: P6 (persona that grows). Needs P1 (done).

### P5 follow-up - verified on the user's Windows PC
- Full suite on the PC: 431 passed, 1 failed (test_broker.py::test_guard_blocks_drive_roots_with_windows_path_rules). Real bug, not just the test: `Guard.check` ran realpath BEFORE the
  bare-drive test, and on Windows realpath("C:") = current folder on C:, so "C:" slipped through. FIXED in broker/ops.py (raw text tested first). ops.py is a constitution file -> RESEAL (seal.bat).
- Real-life prompt checklist for P5 was given to the user (read-only prompts run; C: asks; E: delete/move-out hard-asks; kill/stop/uninstall/registry-write always ask; dry_run; refusals; pause mid-install; new drive F:; UAC declined).
  Results of that live testing are NOT in yet: when the user reports, mine runtime/logs/agent_trace.jsonl (tool_search / tool_auto_added on PC tools -> tune routing.FAMILIES pc_*), audit.jsonl, runtime/broker/broker.log.

## HANDOVER -> P6 (persona that grows). Start here.
- State: P0-P5 done. Suite on the user's PC: 431 passed after the guard fix (run `.venv\Scripts\python.exe -m pytest tests -q`; the user uses PowerShell, so call the venv python directly, activate.ps1 is blocked by execution policy).
  Pending for the user: copy files in, double-click seal.bat (P5 changed constitution files + broker/*), pip install pywinauto (optional, for ui_click) and send2trash (optional).
- Read CLAUDE.md (esp. "Safety layer", "PC agent"), then the P6 prompt in OPUS_HANDOFF.md. P6 asks the user only what the prompt lists (OPUS_HANDOFF section 7 item 7: should the reflection job edit lucy_self.md silently, or also post a daily "what I changed" note - recommended both).
- P6 touches the persona: `.core/personas/lucy.md` becomes CORE (human-owned; ADD it to the constitution list: src/lucy/safety/constitution.py config_files -> one reseal), new `.core/personas/lucy_self.md` is agent-writable (not constitution).
  The agent must not be able to edit lucy.md: file tools already refuse constitution files once it is listed. `update_self` is a new tool (tier write, path arg must be in PATH_KEYS or handled inside the tool since it writes a fixed file).
  Batch ALL safety edits into ONE reseal and tell the user. Persona is currently read once at startup (finding #6): add an mtime cache so edits apply without a restart.
- Memory today: runtime/data/knowledge_store.json (src/lucy/memory/), conversation search in src/lucy/rag/. P6 upgrades it to SQLite + FTS5 with a migration: keep the JSON as a backup, never delete it.
- Lessons carried forward: (1) tests that touch api need unique session ids + purge (see test_proactive_integration); (2) call safety.set_default(None) in test teardown; (3) LibreOffice/PowerShell
  output is not proof it works in real Word/Windows: say clearly what could not be verified in the sandbox; (4) the sandbox has no pytest/fastapi/httpx: a stand-in shim was used to run ~372 tests, the real run is on the user's PC;
  (5) a regex/line-ending edit can break a file: run `python -m py_compile` on every touched .py and keep chat.js/chat.html CRLF; (6) Windows semantics (drive letters, realpath("C:")) can only be tested via ntpath monkeypatching here.
- Open items carried over: P3 Word "unreadable content" (user lives with it); P4 browser tab-spam guard; taint.suspicious ignores network-tier calls; api.py still ~2,900 lines (P0 goal <1,500 not met);
  git init + first commit never done on the user's PC; TTFT before/after numbers not measured; proactive.yaml timezone still UTC (P8 fixes it).

## P6 - persona that grows + memory on SQLite (DONE in sandbox; RESEAL with seal.bat; run the full suite on your PC)
- New src/lucy/persona/: guard.py (secrets/links/markup/rule-override refusal), self_store.py (6 sections, caps 6-8 lines, 2,400 chars, daily limits agent 8 / reflection 8, changelog `.core/personas/self_changelog.jsonl`, revert, external-edit detection), loader.py (CORE+SELF, mtime hot reload), reflect.py (idle 30 min / nightly 03:00 job, strict-JSON proposal applied only through the guards, daily note in runtime/data/learned/).
- Tool update_self (tier write, no card; routing family for correction phrases; memory rules mention it). CORE `lucy.md` is now a CONSTITUTION file (constitution.py config_files: ONE edit) -> file tools hard-refuse it; `lucy_self.md` is not locked (seeded empty). file_guard/apply_patch messages no longer say "restart".
- Memory: runtime/data/knowledge.sqlite (SQLite+FTS5, memory/store.py); knowledge_store.json imported once, copied to .pre-sqlite.bak, never touched again. memory/consolidate.py: merge near-duplicates (ARCHIVE, merged_into), flag likely contradictions (shown in recall_memory), decay 45 d, archive stale importance-1 after 180 d.
- api.py: `_persona_text()` replaces the read-once `_persona_prompt`; /api/persona, /changelog, /revert, /reflect, /notes; scheduler start/stop; UI: Settings > Persona (SELF, changes + Revert, Reflect now, daily note).
- FIXED unrelated bug: memory/history.py used a column `rowid_ref` that does not exist, so `search_conversations` raised on the real schema (regression test added).
- Tests: +48 persona/memory/reflect tests (all pass in sandbox) +5 in test_p6_api.py (NOT run here: no FastAPI). Changed an existing test: test_prompt_order now compares with `api._persona_text()` (the old module variable is gone). Existing runnable tests: identical results before/after.
- Not verified here: api wiring + UI in a browser, reflection with the real 12B/4B (JSON quality of small models: watch runtime/data/learned/ notes), real Windows paths. Decision taken: reflection edits SELF silently AND writes the daily note (shown in Settings, not posted into chat).
- Open: conflict detection is a heuristic (opposites, negation, numbers, weekdays); `taint.suspicious` still ignores network tier (P4 item).
- Next: P7 (self-evolving tools, gated). RESEAL first: copy files in, double-click seal.bat.

### P6 follow-up - verified on the user's Windows PC
- First run: 497 passed, 2 failed. (1) test_p6_api::test_status_changelog_revert_routes: REAL bug, the revert route caught `ValueError` before `SelfError` (a ValueError subclass), so refusals returned 400 instead of 409. FIXED in api.py (separate try for the id parse). (2) test_proactive_integration (real runtime DB, same issue as P3): not caused by P6; fixed on the PC with the P3 cleanup.
- After the fixes: **499 passed** on the user's PC. P6 is DONE. seal.bat was part of the install (constitution.py now also locks `.core/personas/lucy.md`).
- Explained to the user: sealing protects against Lucy's own mistakes / prompt injection, not against other people; SELF adds to CORE, never replaces it.

## HANDOVER -> P7 (self-evolving code and tools, gated). Start here.
- State: P0-P6 done, 499 tests green on the user's PC (`.venv\Scripts\python.exe -m pytest tests -q`). Nothing pending to seal if the user ran seal.bat after P6; P7 will need at least one more reseal if it edits constitution files (batch them).
- Read CLAUDE.md (esp. "Safety layer", "Persona that grows", "Lessons that cost time"), then the P7 prompt in OPUS_HANDOFF.md. Decision points to ASK the user: only Lane B policy (OPUS_HANDOFF section 7 item 6: core changes need an approval click - confirm or change) and anything the P7 prompt lists. Choose the conservative option otherwise and note it here.
- Hooks P7 builds on: `tools/registry.py` auto-discovery (constitution file) -> a second package `src/lucy/tools_user/` needs discovery added there (a constitution edit; keep it to ONE small edit) and every user tool must still pass `ToolSpec.run -> safety.before/after`; user tools default tier "exec" until the user promotes them; `PATH_KEYS` only sees fixed arg names (a generated tool that writes files must use them); routing: add the new tools to `tool_search` (ToolCatalog) rather than the always-on prompt; `update_self`/`persona` show the pattern for "agent writes a file, harness validates + logs + user reverts" (changelog, caps, guard, daily limits) - reuse it for `create_tool` provenance/probation (changelog, auto-disable) and for trace mining.
- Lane B (changes to Lucy Core itself): work in a COPY of the repo, run the full suite, canary instance on another port + smoke script, show diff, swap only on the user's approval, keep the previous version for rollback; NEVER touch constitution files; no git repo exists on the user's PC yet (P0 item never done) - rollback must not depend on git.
- Trace mining input: `runtime/logs/agent_trace.jsonl` (events tool/ok, broken_promise, forced_call, unbacked_claim, repeat_cut, tool_denied, tool_search, tool_auto_added); P6's `persona/reflect.py::_trace_summary` already parses it and `runtime/data/learned/` holds the daily notes.
- Open items carried over: P3 Word "unreadable content" (user lives with it); P4 browser tab-spam guard; `taint.suspicious` ignores network-tier calls; api.py ~3,100 lines (P0 goal <1,500 not met); git init + first commit never done; TTFT numbers not measured; proactive.yaml timezone still UTC (P8); reflection never tried with the real 12B/4B models: check `runtime/data/learned/` notes and `.core/personas/self_changelog.jsonl` after a few days and tune `reflect.SYSTEM_PROMPT` / `FAMILIES` (`self`) if the small models produce junk.
- Env on the user's PC: Windows, PowerShell, Python 3.12 venv, models via llama-server (12B :8080, 4B :8081); activate.ps1 is blocked, call `.venv\Scripts\python.exe` directly.

## P7 - self-evolving tools + gated core changes (DONE in sandbox; RESEAL with seal.bat; run the full suite on your PC)
- New src/lucy/selfdev/ (CONSTITUTION): sandbox.py/_runner.py (AST check + child process `-I -B`, temp cwd, keyless env, timeout, audit hook blocking writes/net/programs/ctypes/registry per `needs`), user_tools.py (Lane A: tests gate, staged improve, probation 10, auto-off after 3 fails, hash tamper check, provenance + version files, user-only tier/enable/revert, daily limit 12), lane_b.py (copy -> frozen change set -> pytest + canary + smoke -> user Approve -> backup + atomic swap -> rollback), smoke.py (fake LLM, canary on a free port, autostart off), trace_mining.py (weekly ideas), __main__.py + rollback.bat.
- Tools: user_tool, core_change. Routing families selfdev_tool/selfdev_core. ToolCatalog(live=) so new tools work in the same turn. api.py: catalog wiring, /api/selfdev/* (status, proposal, approve/reject, rollback, tools tier/enable/disable/revert/delete, log, mine), weekly mining loop. UI: Settings > Evolve (CRLF kept).
- ONE constitution batch -> RESEAL: constitution.py, layer.py, policy.py (+ new selfdev/*.py). Bug found by a test and fixed before shipping: `lstrip("./")` ate the dot of `.core/` in the protected-file check.
- Decision: Lane B = approval click for every change (handoff recommendation; conservative). Ideas are suggestions only (no auto-written PRs). submit asks once (runs agent code unsandboxed in the copy).
- Tests: +58 (sandbox 17, user tools 12, lane B 22 incl. the canary harness against a stub app, trace 3, loop 2, api 2 [FastAPI, not run here]). Sandbox run: 490 passed (434 old + 56 new); the 7 FastAPI-only files need your PC.
- Not verified here: the canary against the REAL app (the harness itself is tested with a stub uvicorn), the suite inside a copy on Windows, audit hook on Windows paths. Open: workspace edits ask on C: (optional `{RUNTIME}/workspaces` in folders.write); child processes of timed-out `needs=programs` tools.

### P7 fix 1 - found on the user's PC (557 passed there before this)
- Every `user_tool create` failed: "blocked: reading runtime/selfdev/staging/<name>.py". Cause: the staging folder was inside a folder on the sandbox's deny list (`selfdev._deny_read`); the tests used an empty deny list so they missed it.
- Fixed: `_runner.py` always allows reading the job's own module; `runtime/selfdev` removed from the deny list (no secrets; agent writes there are still refused by the safety layer). Regression test uses the production deny list. Constitution files changed -> RESEAL.

## HANDOVER -> P8 (proactivity and long-running autonomy). Start here.
- State: P0-P7 done. User's PC: 557 passed after P7; P7 fix 1 shipped (4 files, reseal) -> expect 558. Real-world P7 checks
  (Lane A tools, Lane B canary on Windows) are still being tried by the user: if they report problems, fix those FIRST (small diffs).
- Open from P7 (not P8 work unless the user asks): canary vs the real app on Windows unverified; Lane B workspace edits ask on C:
  (optional `{RUNTIME}/workspaces` in folders.write); child processes of timed-out `needs=programs` tools may survive.
- P8 building blocks that already exist (reuse, do not rebuild):
  * AgentLoop (src/lucy/server/agent_loop.py): no step cap, 30 no-progress rounds end a turn. Run jobs through it (same tools/routing/gate).
  * Safety in the background: the gate is `gate(name, args, emit)`; with `emit=None` a call that needs approval is DENIED with
    "background task" text (layer.py ~203). With an emit, it creates a broker request, emits `approval_request` and WAITS for
    `broker.resolve` until the approval timeout (timeout = deny). P8 "park the job": the job runner passes its own emit that stores the
    request for the Tasks panel; check the timeout first (a job parked for hours needs either a long wait or: deny now, checkpoint, and
    re-run the step after the user approves). Prefer doing this in the runner WITHOUT editing layer.py (constitution file).
  * Scheduler: src/lucy/proactive/engine.py (`_loop`/`tick`, quiet hours, `pause`, `busy`, timezone via `tz()`); store.py (SQLite);
    tools.py (reminder tools pattern); web.py (router `/api/proactive/*`). Fake-clock test patterns: tests/test_proactive*.py, test_p6_reflect.py.
  * Weekly trace review already runs from api._mine_loop (P7); P6 reflection runs on idle/nightly. P8 can move both onto the job queue.
- P8 must also: fix the timezone default. .core/proactive.yaml still says `timezone: app` (= UTC, quiet hours wrong for the user in Manila);
  set the example/default to "Asia/Manila" (tzdata is in requirements.txt).
- Tools to add (small-model shaped, one tool with actions is preferred): schedule_task / list_tasks / cancel_task -> consider ONE `task`
  tool (action: schedule|list|cancel|status). Budgets (time, tool calls, tokens) and checkpoints so a job resumes after a restart.
- UI: a minimal "Tasks" panel (progress cards, parked approvals). Match the existing style; ui/*.{html,js,css} are CRLF.
- Decision points to ask the user: none listed in the P8 prompt. Choose conservative defaults (jobs park on approvals, respect quiet
  hours, modest default budgets) and note them here.

## P8 - proactivity and long-running autonomy (DONE in sandbox; NO constitution file touched -> NO reseal; run the full suite on your PC)
- New src/lucy/autonomy/ (store, budget, gate, runner, tools, web) + proactive/cron.py. Queue = runtime/data/jobs.sqlite. The proactive engine is the scheduler (engine.jobs; tick/stop/recover hooks, ProactiveHost.job_hooks). Cron added as a repeat-rule type (reminders too).
- Jobs run through the SAME AgentLoop (api `_jobs_make_agent`), budget (20 min / 60 calls / 80k tokens default, clamped; Continue adds a round), checkpoint after every message so a restart/pause/stop resumes. Start rules: quiet hours, pause (safety + proactive "all"), rest days (upkeep only), chat busy/idle, model up, daily caps (8 runs / 400 calls / 3 h); "Run now"/Continue/approval bypass quiet+caps.
- Approvals PARK the job instead of blocking (gate.py calls safety.decide/describe/gate only); answer in Settings > Tasks; harness runs the approved call itself on resume; denied/expired(48 h) = a result. Chat has priority between tool calls (wait not billed).
- Tool: ONE `task` (add|list|status|cancel|resume); schedule_task/set_reminder kept as they were. Model-made full-tools jobs wait for the user's OK; `task`, `core_change`, `user_tool`, `proactive_control` blocked inside jobs. Routing family `tasks` (bare word "task" is not a trigger).
- Upkeep on the queue: reflection (every 5 min check, own due() logic) + weekly trace review; `jobs.system_jobs: false` restores the old loops. Lane B canary never runs jobs (LUCY_CANARY).
- proactive.yaml: timezone now "Asia/Manila" + `jobs:` block. UI: Settings > Tasks (add, approve/deny/always, OK, Run now, Continue, Cancel). Event key `job*` is excluded from the old follow-up daily cap.
- Tests: tests/test_p8_jobs.py (37, ran 5x stable in sandbox with stand-in pytest/fastapi/httpx incl. the REAL AgentLoop). Old proactive/agent_loop/safety_loop tests unchanged.
- NOT verified here: api.py wiring (cannot import it: no pydantic/uvicorn), UI in a browser (only `node --check`), reflection/trace upkeep on the queue with the real models, Windows. Decisions: Lane/jobs defaults conservative; one job at a time.
- Next: P9 (Claude-Desktop-style features), only when asked.

### P8 follow-up - verified on the user's Windows PC
- First run: 593 passed, 2 failed (both my own): a racy budget test (when the Nth tool call exactly hits the budget the run can stop before the next model request; the test now asserts the calls made AT the stop) and `test_yaml_defaults_file_matches_code_defaults` (the yaml must mirror DEFAULTS: DEFAULTS timezone is now "Asia/Manila" too). After fix1: **595 passed** on the PC. P8 is DONE.

## HANDOVER -> P9 (Claude-Desktop-style features and polish). Start here.
- State: P0-P8 done, 595 tests green on the user's PC (`.venv\Scripts\python.exe -m pytest tests -q`, ~4 min; PowerShell blocks activate.ps1). No reseal pending (P8 touched no constitution file). Real-world P8 checks the user may still report on: Tasks tab in the browser, parking/approving a step, restart-resume, upkeep jobs (reflect / trace_mining on the queue; `jobs.system_jobs: false` restores the old loops).
- Read CLAUDE.md (esp. "Tasks (P8 result)", "Lessons"), then the P9 prompt in OPUS_HANDOFF.md. P9 has NO listed decision points: choose conservative defaults, one commit/item, keep the current UI look (add only what an item asks; ui/chat.{html,js,css} are CRLF - edit binary-safe, `node --check chat.js` after).
- Building blocks to REUSE: (a) job queue (`engine.jobs.add_job(...)`, `JobRunner`, budgets, parked approvals) = the natural engine for item 8 "deep research" (a `full`/`read` job whose report is a file in the Library) and item 5 plan mode can reuse the parked-approval card pattern; (b) Settings tab pattern: tab button + `panel-<x>` + `switchSettingsTab` hook + `evolveBtn`/`tasksCall` helpers in chat.js; (c) `/api/*` routes use `Depends(verify_api_key)`, UI fetches with `?api_key=${API_KEY}`; (d) tool routing families + `ToolCatalog` for any new tool; (e) sessions DB + `_get_messages` in api.py for edit/regenerate/branch/export; (f) library.py (content-addressed cache) for item 9 (size cap, pin, cleanup).
- Test sandbox notes: no pytest/fastapi/httpx offline; a stand-in runner + a working httpx shim (http.client based) let the REAL AgentLoop tests run in the builder sandbox, but api.py cannot be imported there (no pydantic/uvicorn): api-level changes can only be proven on the user's PC - keep them small, py_compile them, and put logic in importable modules (like autonomy/) with tests.
- Open items carried over: P3 Word "unreadable content" (user lives with it); P4 browser tab-spam guard; `taint.suspicious` ignores network-tier calls; api.py ~3,300 lines (P0 goal <1,500 not met; consider moving routes into routers as P9 adds endpoints); git init + first commit never done; TTFT numbers not measured; reflection never judged with the real 12B/4B (check runtime/data/learned/); Lane B canary vs the real app on Windows unverified; P8: a job waits (does not run) while the user chats between tool calls, but the model call itself can still contend for the single llama slot; `schedule_task` (read-only follow-up) and `task` (jobs) coexist on purpose.
- Env on the user's PC: Windows, PowerShell, Python 3.12 venv, models via llama-server (12B :8080, 4B :8081), timezone Asia/Manila.

## P9 - Claude-Desktop-style features (DONE in sandbox; NO constitution file touched -> NO reseal; run the full suite on your PC)
- New `src/lucy/desk/` (db, projects, convo, commands, sources, plan, checkpoints, research, usage, quick, web). api.py: ~170 lines of wiring (`_desk_ctx`, project block in `_build_messages`, `remember` -> project notes, `plan_mode` in `_stream_response`, citations footer, `_stream_research`, per-turn checkpoint, `mode`/`reuse_shas` form fields, SSE events `turn`/`turn_done`/`attachment`, routes at the bottom). library.py: `library_keep`/`library_meta` tables, pins, cap (default 2 GB, `LUCY_CACHE_CAP_MB`), cleanup, routes.
- Items: 1 projects (sidebar list + editor, files kept via library_keep, project notes), 2 edit/regenerate/branch (message hover buttons; edit = rewind + resend), 3 `/commands` + `@files` popup (paths only; content never inlined, reads still go through the gate), 4 bar under the chat: model select, Thinking, context meter (estimate), 5 plan mode (read-only tools, no update_plan, Approve button sends "Plan approved..."), 6 rewind (`Backups.restore` as user; only file-tool changes), 7 citations (harness footer), 8 deep research (a chat turn, NOT the job queue: deterministic harness pipeline is steadier for 4-12B), 9 Library pin/cap/cleanup/side pane, 10 Settings > Overview, 11 export md/json (session menu, `/export`), 12 `quick.bat` (Ctrl+Alt+Space, opens `/?q=`; tray only if pystray+Pillow installed).
- Tests: `test_p9_library.py` (6), `test_p9_desk.py` (23) ran in the builder sandbox with a stand-in runner; `test_p9_api.py` (3, FastAPI + real app) was written blind and NEVER RUN.
- NOT verified: api.py (py_compile only), the UI in a browser (only `node --check`), the hotkey script on Windows, research against real Bing/Jina, small-model quality of plan mode / research notes, full suite.
- Defaults chosen: cap 2 GB; project files <= 20 MB; research <= 5 queries, 8 pages, 20 min; commit granularity was coarser than one-per-item.
- Open: research/plan mode untested with the real 12B/4B (tune `plan.PLAN_NOTE`, `research.py` prompts); regenerate deletes proactive messages after the last user message; edit/regenerate do not undo file changes (use Rewind).
