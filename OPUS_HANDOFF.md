# Lucy Core -> Opus handoff

Put `CLAUDE.md` in the Lucy_Core root (Claude Code reads it automatically), put this file next to it, then paste ONE phase
prompt at a time. Phases are ordered so each makes the next cheaper (P0 shrinks the files Opus has to read; P1 is the
safety layer everything after it depends on).

**Why phases instead of one giant prompt:** a single "build everything" prompt makes Opus hold 20k lines in context and
re-derive decisions repeatedly. Phase prompts + `docs/PROGRESS.md` let each session start cold and cheap.

---------------------------------------------------------------------------------------------------------

## 1. Verified findings (facts checked in the code, not guesses)

| # | Finding | Impact | Phase |
|---|---|---|---|
| 1 | `run_shell_command` runs `["bash","-c",cmd]` with a **hard 10 s timeout**, no background jobs, no streaming | On Windows without Git-Bash it fails outright; builds/installs/tests always time out. Blocks all coding + PC-agent work | P0 |
| 2 | `mcp.json` exists but **nothing reads it** (dead config); Google tools are hand-rolled | No MCP ecosystem (Office, GitHub, browser, filesystem...) | P4 |
| 3 | There is **no permission model**: any tool call runs immediately | Cannot safely grant "admin over the drive" | P1 |
| 4 | `api.py` is a 3,600-line monolith; adding a tool = 3 edits in different places (def, `if` branch, label) | Every new tool costs Opus a big read; error-prone | P0 |
| 5 | `skill_list` tool gating exists in `_stream_response` but `chat_endpoint` never passes it: **all tools are always in the prompt** | Prompt bloat grows with every tool; a 12B model gets worse as tool count grows | P0 |
| 6 | Persona (`lucy.md`) is read **once at startup** | Self-written personality changes would not apply until restart | P6 |
| 7 | `.core/harness.md` is stale ("max 8 tool rounds", "max 2 reminders") | The agent's picture of itself is wrong | P0 |
| 8 | No `requirements.txt` / `pyproject.toml` | Not reproducible; Opus can't install/verify | P0 |
| 9 | `google_client_secret.json` and an OAuth client id sit in the project root / `mcp.json`; no `.gitignore` | Leaks on every push/zip | P0 |
| 10 | `proactive.yaml`: timezone `app` resolves to **UTC** (the file's own comment warns about it) | Quiet hours 23:00-07:00 are applied in UTC, i.e. wrong hours for you. Set `timezone: "Asia/Manila"` (needs `pip install tzdata`) or `"+08:00"` | P0 |
| 11 | Tool calls run **sequentially** | Slow multi-read tasks | P2 |
| 12 | Prompt order: dynamic `[USER FACTS]` sits after the persona | Breaks llama.cpp prompt-prefix cache -> slower every turn | P0 |
| 13 | Cache/Library has no size cap or cleanup UI | Grows forever | P9 |

Already fixed in earlier sessions (do not redo): attachments persist (Library + `cache/`), refresh opens a draft start page,
no round cap, workaround playbooks, repetition guard, harness-made constrained tool call, in-progress-narration detection.

---------------------------------------------------------------------------------------------------------

## 2. Phase prompts (paste one at a time)

### P0 - Foundation (do this first: it makes everything after it cheaper)
```text
Read CLAUDE.md. Phase P0: foundation. Goals, in this order, one commit each:
1. Repo hygiene: git init if needed; add .gitignore (runtime/, cache/, models/, *.gguf, google_client_secret.json,
   __pycache__, .env, *.sqlite*); add requirements.txt (+ requirements-dev.txt) by reading actual imports; confirm
   `python -m pytest tests -q` is green on a clean venv.
2. Tool registry: split tools out of api.py into package src/lucy/tools/. One module per tool (or per small family) that
   exports: SCHEMA (OpenAI function def), handler(args, ctx) -> str, TIER ("read"|"write"|"exec"|"system"|"network"),
   LABEL (activity text). Auto-discover modules at startup. Keep behaviour byte-identical; move code, do not rewrite it.
   api.py must shrink to routing/prompt/session code. Existing tests must stay green; add a test that every registered
   tool has a schema, a handler, a tier and a label.
3. Tool routing for small models: implement `select_tools(user_text, history)` returning a SMALL subset of tools per turn
   (always: file read/write/edit, update_plan, remember/recall; the rest chosen by keyword/intent families), plus a
   `tool_search(query)` meta-tool that adds more tools mid-turn. Wire it into chat_endpoint (the dead `skill_list` path is
   the starting point). Add tests. Goal: <= ~10 tool schemas in a typical prompt.
4. Replace run_shell_command: Windows-native (PowerShell by default, cmd/bash selectable), per-call timeout (default 60 s,
   max 3600 s), background jobs (`action`: run|start|poll|kill|list), incremental output with tail truncation, working
   directory + env, returns exit code. Never hang the event loop. Tests with a fake slow command.
5. Prompt order for KV-cache reuse: static first (persona, rules, harness map), dynamic last (facts, recent history);
   verify llama-server `cache_prompt` is on. Measure and report time-to-first-token before/after on a 3-turn chat.
6. Fix .core/harness.md drift; set proactive.yaml timezone note; write docs/PROGRESS.md.
Definition of done: tests green, api.py < 1,500 lines, a new tool can be added by dropping ONE file in src/lucy/tools/.
Ask me only about: PowerShell vs cmd as default shell.
```

### P1 - Safety layer ("constitution", permissions, audit, undo) - REQUIRED before P5/P7
```text
Read CLAUDE.md and OPUS_HANDOFF.md section 3. Phase P1: build the permission + safety layer. Requirements:
- Permission tiers by tool TIER (read < write < exec < system < network). Modes selectable in the UI and persisted:
  "Ask every time" | "Ask for risky" (default) | "Auto within allowed drives" | "Full auto" (never available for the
  constitution list). Config file `.core/permissions.yaml`: allowed drives/folders (the user will give them), denied
  paths (secrets, the OS dir, Lucy's constitution files), command deny/ask patterns (format, diskpart, del /s on roots,
  registry hives, shutdown, net user, curl|iex style downloads...), network allowlist.
- Approval flow: when a call needs approval the loop PAUSES (SSE event `approval_request` with tool, args, plain-language
  summary, diff for edits), the UI shows Approve / Deny / Always-allow-this-session cards (minimal UI addition,
  match existing style), and the loop resumes with the decision. A denial is a tool result the model must work around
  (never a dead end). Timeouts = deny.
- Audit log: append-only JSONL `runtime/logs/audit.jsonl` of every tool call with tier, decision, who decided.
- Undo: before any write/edit/delete/move outside git, snapshot to `runtime/backups/` (already exists for Lucy Core
  files; extend to allowed drives with size limits). Add `restore_from_backup` available to the USER (UI/API), and to the
  agent only within a session's own changes. Prefer Recycle Bin semantics for delete.
- Constitution files (hash-locked at startup; any change not made by the user via the OS -> refuse to start the agent and
  show why): permissions.yaml, the permission/approval/audit code, file_guard path rules, this list itself.
  The agent's file tools must hard-refuse writes to them; test with an adversarial script.
- Prompt-injection guard: content from web_extract/web_search/files-from-others is wrapped as untrusted data; a tool call
  whose arguments appear to originate from untrusted content (substring/URL/command match) is escalated to "ask".
- Kill switch: Stop button already cancels the turn; also add a global "pause agent" flag that background jobs respect.
Tests: tier enforcement, approval pause/resume with the fake server, denial -> workaround, constitution write refused,
injection escalation, audit entries. Ask me only about: default mode and the allowed-drive list.
```

### P2 - Coding tools (Claude-Code-style)
```text
Read CLAUDE.md. Phase P2: coding agent tools, small-model friendly (few tools, strict schemas, clear errors with fixes).
Build in src/lucy/tools/: `code_search` (ripgrep if present else Python; regex/glob/context lines), `apply_patch`
(unified diff or search/replace blocks, atomic, shows the diff, syntax-checks result; fuzzy anchor matching like
file_guard.edit_file), `run_tests` (detects pytest/npm/dotnet, runs in background job, returns failures condensed),
`git` (status|diff|log|branch|commit|checkout|stash; commits need tier "write"), `python` (persistent REPL per session
with timeout, for quick computations), `project_map` (cached tree + symbol outline of a repo so the model need not read
whole files), `format_and_lint` (ruff/black/eslint if available). Add parallel execution of independent READ-tier tool
calls in agent_loop (asyncio.gather, preserve result order, keep sig/cache logic). Add an automatic VERIFY step:
after a write/edit to code, the harness runs the syntax check (and, if the user enabled it, the tests) and appends the
result to the tool output so the model sees breakage immediately. Add a "coding mode" system-prompt addendum selected by
tool routing (plan -> implement -> run tests -> fix -> report diff). Tests with the fake server. 
```

### P3 - Office documents (Word, Excel, PowerPoint, PDF)
```text
Read CLAUDE.md. Phase P3: Microsoft Office + PDF tools. Use python-docx, openpyxl, python-pptx, reportlab/pypdf; no
Office installation required (LibreOffice optional for rendering). Design for a small model: it must NOT write raw
library code. Tools take STRUCTURED input:
  `docx_create(path, blocks=[{type: heading|para|bullets|numbered|table|image|pagebreak, ...}], style?)`, `docx_edit(path, ops)`,
  `xlsx_create(path, sheets=[{name, rows|columns, formulas, number_formats, column_widths, freeze}])`, `xlsx_edit`,
  `pptx_create(path, slides=[{layout, title, bullets|image|chart|notes}])`, `pptx_edit`, `pdf_create/pdf_extract`,
  and a single `office_read(path)` that returns readable text/tables of any docx/xlsx/pptx/pdf.
Add `office_render(path)`: converts to PDF (LibreOffice headless if installed) then to PNG pages, and let the model
check the result with analyze_image (visual QA loop). Files must land in the Library via the existing MEDIA mechanism.
Ship 3 house templates (clean report, resume, slide deck) and a SKILL.md-style guidance file the loop injects only when
these tools are selected (see Claude-style skills in section 5). Tests: round-trip create -> read -> edit -> read.
```

### P4 - MCP client + browser
```text
Read CLAUDE.md. Phase P4: real MCP support. Implement an MCP client (stdio + streamable HTTP) that reads mcp.json (the
file exists but is unused), launches servers lazily, lists their tools, namespaces them (`server__tool`), maps them into the
tool registry with a TIER derived from the server config (default "network"), and exposes them through tool_search so they
do NOT all enter the prompt. Settings UI list: connected servers, enabled toggle, last error (minimal addition).
Then add a browser tool through Playwright MCP (or built-in Playwright): navigate, snapshot (accessibility tree, not
screenshots, for small models), click, type, extract. Respect P1 permissions + the injection guard.
Replace the hand-rolled Google tools only if an MCP equivalent works end to end; otherwise keep them.
Do not commit client secrets. Ask me only about: which MCP servers I want enabled first.
```

### P5 - Full PC agent (admin) - only after P1 is merged
```text
Read CLAUDE.md and OPUS_HANDOFF.md section 3. Phase P5: Windows PC agent. Do NOT run the whole agent as Administrator.
Build a small separate "broker" process (elevated, localhost-only named pipe with a shared secret) that executes a fixed
set of admin operations on behalf of Lucy after P1 approval: services (list/start/stop), processes (list/kill), scheduled
tasks, winget install/uninstall/upgrade, registry read (write = system tier, always ask), firewall rules read, disk usage,
event-log read, Windows Update status, file operations on protected locations. Non-admin tools: window list/focus,
UI Automation via pywinauto (accessible-name based clicking, preferred over pixel clicking), clipboard, screenshots with
region crop, notifications, volume/brightness, open app/URL. Improve existing cua_* tools: take a screenshot, find the
target with analyze_image, click, VERIFY by a second screenshot. Each op: structured schema, tier, audit entry, dry-run
mode. Tests with a mocked broker. Ask me only about: which drives/folders are in scope and whether to install the broker as a service.
```

### P6 - Memory, persona, and self-written character
```text
Read CLAUDE.md and OPUS_HANDOFF.md section 3. Phase P6: persona that grows. Split the persona:
  .core/personas/lucy.md        = CORE (human-owned, hash-locked constitution file; identity, safety, voice rules)
  .core/personas/lucy_self.md   = SELF (agent-writable): learned traits, preferences, in-jokes, how the user likes to be
                                   spoken to, mistakes to avoid, evolving opinions. Sections with size caps.
Load both every turn with an mtime cache (hot reload, no restart), SELF placed after CORE and before dynamic facts.
Agent tool `update_self(section, op: add|revise|remove, text, reason)` (no approval, no prompt needed) writes with a
changelog (`.core/personas/self_changelog.jsonl`, diff + reason + session id) and a one-click revert in the UI/API.
Autonomous reflection: after a session goes idle (and nightly), a background "sleep" job reads the day's conversations
and trace, then (a) updates SELF, (b) consolidates memory (dedupe, merge, importance decay, contradictions flagged),
(c) writes a short "what I learned" note. Guardrails in code: SELF cannot contradict CORE rules (reject lines that
override safety/permission text), max N edits/day, max size, never stores secrets. Upgrade memory storage from the JSON
file to SQLite + FTS5 (+ embeddings via the existing rag code) with a migration. Tests: hot reload, changelog+revert,
reflection job on a fake model, CORE protection.
```

### P7 - Self-evolving code and tools (gated)
```text
Read CLAUDE.md and OPUS_HANDOFF.md section 3. Phase P7: the agent improves itself, safely. Two lanes:
LANE A - new tools (autonomous): `create_tool(name, description, code, tests)` writes to src/lucy/tools_user/<name>.py with
the standard SCHEMA/handler/TIER/LABEL contract; the harness runs the tool's tests + a static check in a subprocess
sandbox (no network unless tier allows, timeout, temp cwd), registers it with hot reload on success, records provenance
(session, reason), marks it "probation" for N uses, and auto-disables on repeated failure. User tools default to tier
"exec" until promoted by the user. `list_user_tools`, `disable_tool`, `improve_tool` (edit + re-test).
LANE B - changes to Lucy Core itself (gated): the agent works on a git branch in a COPY of the repo (never in the live
tree), runs the full test suite, starts a canary instance on another port, runs a smoke script (chat round trip, tool call,
library, proactive tick), and only then presents a PR-style summary + diff to the user for approval; on approval it swaps
and keeps the previous version for instant rollback (`rollback` works even if the new build will not start). It can NEVER
touch constitution files. Add "trace mining": a weekly job analyses runtime/logs/agent_trace.jsonl for failure patterns
(broken promises, repeated failures per tool, forced calls) and proposes concrete fixes as Lane-B PRs or Lane-A tools.
Write docs/SELF_EVOLUTION.md describing the pipeline. Tests: tool lifecycle, sandbox escape attempts, canary failure ->
auto-rollback, constitution refusal.
```

### P8 - Proactivity and long-running autonomy
```text
Read CLAUDE.md. Phase P8: autonomy. Add a persistent job queue (SQLite) for long tasks: goal text, plan, budget (time, tool
calls, tokens), status, checkpoints so a job resumes after a restart. The existing proactive engine becomes the scheduler:
recurring tasks (cron syntax), idle-time jobs (self-improvement, memory consolidation, inbox/Drive digests, "check on X"),
and user goals ("keep an eye on...", "finish this report by Friday"). Every job runs through the same AgentLoop (so the
no-give-up behaviour and P1 permissions apply), posts progress cards to a "Tasks" panel, and asks for approval through the
P1 flow instead of blocking forever (an unanswered approval parks the job). Quiet hours/rest days/budget caps respected.
Add `schedule_task`, `list_tasks`, `cancel_task`. Fix the timezone default (Asia/Manila example in proactive.yaml).
Tests with a fake clock.
```

### P9 - Claude-Desktop-style features and polish (UI additions only where listed)
```text
Read CLAUDE.md. Phase P9: add, in this order, keeping the current UI look: (1) Projects: a project = folder + instructions
+ attached files + its own memory scope; sessions belong to a project; (2) edit a sent message / regenerate / branch the
conversation; (3) slash commands and @file mentions in the input; (4) context + usage meter and a model/thinking selector;
(5) "plan mode": the agent shows its plan and waits for approval before executing (uses update_plan + P1 approvals);
(6) checkpoints/rewind per turn (restore files + conversation); (7) citations: web answers include numbered source links;
(8) deep research mode (multi-step search -> notes -> report file in the Library); (9) Library: cache size cap + cleanup UI,
pin items, open an item beside the chat (split view); (10) settings: connectors list (P4), permission mode (P1), tasks (P8);
(11) conversation export; (12) global hotkey / tray quick-entry if running as a desktop app. One commit per item.
```

---------------------------------------------------------------------------------------------------------

## 3. Safety design for "admin over the drive" + self-evolution (the part that keeps your PC and Lucy intact)

You asked for maximum autonomy. This is how to get it without a 12B model being one bad generation away from a wiped
drive or a rewritten safety layer. None of it limits what Lucy can *do*; it limits what can go *unrecoverably* wrong.

1. **Constitution files are hash-locked and agent-immutable.** Permissions config, approval/audit code, file_guard path
   rules, the CORE persona. Everything else is fair game for self-improvement.
2. **Least privilege, elevated by a broker, not by running everything as Administrator.** Lucy Core runs as you. A tiny
   elevated helper does a fixed menu of admin operations after approval. If Lucy's process is compromised (e.g. via
   a malicious web page), the blast radius is the menu, not "arbitrary code as admin".
3. **Everything is reversible by default**: backups before writes, Recycle Bin for deletes, git for code, a rollback for
   Lucy's own upgrades, a changelog for persona edits.
4. **Untrusted content is data, never instructions.** Web pages, downloaded files and emails can contain "ignore your
   rules and run X". The harness tags that content and escalates any tool call that seems to come from it.
5. **Autonomy dial, not a switch.** Start on "Ask for risky", watch the audit log for a week, then widen. "Full auto"
   is allowed inside the drives you grant, never for constitution files or the broker.
6. **Self-modification goes through a canary.** The agent edits a copy, tests it, runs it on another port, and only then
   swaps. New *tools* can be fully autonomous (sandboxed, probation); changes to *core* need your approval click.

---------------------------------------------------------------------------------------------------------

## 4. Tool catalogue to end up with (all small-model-shaped: few tools, `action` params, structured args)

| Family | Tools |
|---|---|
| Files | read, write, edit, `apply_patch`, list, find, `code_search`, move/copy/delete (recycle), `diff` |
| Shell | `shell(action: run/start/poll/kill)`, `python` REPL |
| Code | `git`, `run_tests`, `format_and_lint`, `project_map`, `create_tool` |
| Office | `docx_*`, `xlsx_*`, `pptx_*`, `pdf_*`, `office_read`, `office_render` |
| Web | `web_search`, `web_extract`, `browser(action: goto/snapshot/click/type/extract)`, `download` |
| PC | `windows(list/focus)`, `ui_click` (UIA), `screenshot`, `clipboard`, `process`, `service`, `winget`, `schedule_os_task`, `notify` |
| Memory/self | `remember`, `recall`, `forget`, `update_self`, `search_conversations`, `note_lesson` |
| Autonomy | `update_plan`, `finish_task`, `schedule_task`, `list_tasks`, `spawn_subtask` (sub-agent with a narrower tool set) |
| Vision/audio | `analyze_image`, `ocr`, `send_tts`, (later) speech-to-text dictation |
| Connectors | MCP tools via `tool_search` |

Rule of thumb for the prompt: **<= ~10 tool schemas visible per turn**; everything else is one `tool_search` away.

---------------------------------------------------------------------------------------------------------

## 5. Features I (Claude) have that Lucy doesn't yet - ranked by value for your goals

1. **Skills** - folders of instructions (`SKILL.md` + scripts) loaded only when relevant ("progressive disclosure"). This is
   how I produce good Word/Excel/PowerPoint/PDF files. Lucy has one skill folder; make it a real system (P3/P7 use it).
2. **Deferred tools / `tool_search`** - I don't carry every tool in context. (P0)
3. **Plan mode + checkpoints/rewind** - show the plan, get a yes, execute, rewind if wrong. (P9)
4. **Sub-agents / parallel tool calls** - split work, run reads concurrently. (P2/P8)
5. **Projects** - per-project instructions, files, memory. (P9)
6. **Artifacts + file creation with preview** - you have the Library; add open-beside-chat and versioning. (P9)
7. **Citations + deep research** with sources. (P9)
8. **Connectors/MCP directory** - one-click tools. (P4)
9. **Scheduled tasks + memory that updates itself** - covered by P6/P8.
10. **Verify-after-act loops** (render the doc and look at it, run the tests after the edit). (P2/P3)
11. **Ask-the-user with tappable options** for clarifications that are genuinely needed (cheap UI card).
12. **Message edit / branch / regenerate**, slash commands, @-mentions. (P9)

---------------------------------------------------------------------------------------------------------

## 6. Optimizations specific to running small local models

- **Speculative decoding**: you have both a 12B and a 4B. llama.cpp can use the 4B as the draft model for the 12B
  (`--model-draft`), often a large speed-up for tool-call JSON and boilerplate. Add to `llm_manager.py` as a setting.
- **Prefix-cache-friendly prompts** (P0 item 5) and `cache_prompt: true`.
- **Constrained tool calls as the primary path for the 4B model** (the grammar-constrained request already exists as a
  fallback in `agent_loop._forced_round`): consider using it by default for any model that fails the tool-call self-test
  at startup. Add that self-test (call a dummy tool 5x, measure success).
- **Context compaction**: summarise old turns/tool results automatically (a `summaries` table already exists); cap each
  tool result with head+tail truncation and let the model ask for more.
- **Eval harness**: a folder of 30-50 realistic tasks (send file, edit file, multi-step research, make a docx...) replayed
  against the real models, scored automatically, results in `runtime/evals/`. Without this, "it still halts sometimes" can
  only be fixed by anecdote. Highest-value item after P1.
- **Model-aware tool descriptions**: keep two description sets (short for 4B, normal for 12B).

---------------------------------------------------------------------------------------------------------

## 7. Decisions only you can make (answer these once, in your first Opus message)

1. Allowed drives/folders for read / write / full-auto.
2. Default permission mode (recommended: "Ask for risky").
3. Default shell: PowerShell (recommended) or cmd.
4. Install the admin broker as a Windows service, or start it on demand?
5. Which MCP servers first (suggestion: filesystem, GitHub, Playwright, Google Workspace).
6. Self-evolution: Lane B (core changes) requires your approval click - confirm, or tell Opus a different policy.
7. Should the reflection job be allowed to edit `lucy_self.md` silently, or post a daily "what I changed" note (recommended: both).

## 8. If Opus runs out of budget mid-phase

Tell it: "Finish the current commit, make tests green, append the state to docs/PROGRESS.md, list exact next steps." The
next session starts from `CLAUDE.md` + `docs/PROGRESS.md` only.
