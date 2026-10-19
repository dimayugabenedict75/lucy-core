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


## P4 - MCP client + browser (DONE in sandbox; run the full suite on your PC; ONE constitution edit -> RESEAL with seal.bat)
- New src/lucy/mcp/ (client.py stdio + streamable HTTP, stdlib only; manager.py lazy servers, idle stop, registry sync, arg guard; config.py; schema.py). Tools register as `server__tool` in registry.default, tier from .core/mcp.json (default network), never in the prompt: api._mcp_for_turn -> ToolCatalog (tool_search / auto-add); tool_search description names connected servers; search answer lists a server's other tools (routing.ToolCatalog keywords/core/more_hint, 3-line hook in agent_loop).
- Constitution edit: safety/constitution.py now also locks .core/mcp.json (a server entry = a program that runs on the PC). Shipped .core/mcp.json: playwright (--isolated), github (remote, needs GITHUB_TOKEN), filesystem; all enabled:false. UI toggle (runtime/data/mcp_state.json) needs no reseal.
- Guards in the manager: untrusted framing + taint, paths in lists/file: URLs vs denied rules, MEDIA: neutralised, nothing launches while constitution is broken. Browser tiers: snapshot/screenshot read; navigate/click network; type/fill exec; evaluate/upload system.
- UI: MCP servers table in Settings > Connectors (list, on/off, refresh, last error). api: /api/mcp/status, /{key}/enabled|refresh|stop.
- Tests: test_mcp.py 44 (ran OK in sandbox with a pytest shim), test_mcp_loop.py + test_mcp_api.py NOT run (need httpx/FastAPI); loop handling checked by calling _execute_one directly. Old tests identical before/after on the subset I could run.
- Open: unverified offline: GitHub URL, Playwright flags, real npx on Windows. taint.suspicious ignores network tier (URL copied from a page opens without a card). Tool descriptions from servers could carry injection. No OAuth/SSE/resources. Needs Node.js for npx servers; Playwright may ask to run browser_install.
- Next: P5 only when asked.
