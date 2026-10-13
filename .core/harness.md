# Lucy Core harness - reference

This file describes the program Lucy runs inside. It is read with `read_local_file`; Lucy may improve it
with `edit_local_file` (keep it accurate - it is what future sessions will rely on).

## Where things are
Lucy Core root = the folder containing `src/`, `ui/`, `runtime/`, `assets/`, `.core/` (shown in the prompt).

| Path | What it is |
|---|---|
| `src/lucy/server/api.py` | FastAPI app on **port 8090**: chat endpoints, session storage, **every tool's implementation** (`execute_tool_call`), tool definitions (`TOOLS`), prompt building (`_build_messages`), voice/TTS streaming |
| `src/lucy/server/agent_loop.py` | The tool-calling loop: calls the model, validates and runs tools, retries, nudges, context budget |
| `src/lucy/server/llm_manager.py` | Starts/stops the llama-servers. **Lucy 12B on 8080, Lucy 4B on 8081**, one at a time. Settings tab > Model drives it |
| `src/lucy/server/tts_filter.py` | Removes tool calls, `Action:` lines, `MEDIA:` markers, file paths, URLs, markdown from what is spoken |
| `src/lucy/server/stickers_tool.py` | The `send_sticker` tool and sticker emotion lookup |
| `src/lucy/server/file_guard.py` | Path resolution, tolerant edits, syntax checks, backups for the file tools |
| `src/lucy/memory/`, `src/lucy/rag/conductor.py` | Long-term memory store + search of past conversations |
| `ui/` | `chat.html`, `chat.js`, `chat.css` - the web chat and Settings |
| `lucy_audio/` | Lucy Audio TTS server (**port 8091**), `lucy_player.py` (DirectSound playback), `stream_lucy_audio.py` (CLI) |
| `.core/personas/lucy.md` | Lucy's persona; becomes the system prompt. Read once at startup |
| `runtime/sessions.sqlite` | Every chat (tables: sessions, messages, skills, summaries) |
| `runtime/data/knowledge_store.json` | Long-term memories (id, text, category, importance, dates) |
| `runtime/data/llm_state.json` | Selected model, temperature, reasoning, KV cache |
| `runtime/logs/` | `lucy_core.log`, `agent_trace.jsonl` (every tool call), `deltastream_chunks.log` (TTS chunks), `llama_*.log` |
| `runtime/backups/` | Automatic backups of every file the file tools changed inside Lucy Core |
| `assets/stickers/Dark`, `Light` | Lucy's stickers, named by emotion (`Happy.png`). `User/` is the user's own picker folder |

## How a message flows
1. The UI posts to `/api/chat` (or the voice endpoint). The user message is stored in `sessions.sqlite`.
2. `_build_messages` assembles the prompt: persona -> `[USER FACTS]` (pinned preferences + relevant memories) ->
   memory rules -> harness map -> skills -> tool rules -> recent history.
3. `AgentLoop.run` calls llama-server. If the model asks for tools they are validated, executed by
   `execute_tool_call`, and the results go back to the model until it answers (max 8 tool rounds).
4. Text streams to the UI as SSE. In voice mode finished sentences also go through `tts_filter` to Lucy Audio.

## Multi-step turns (`src/lucy/server/multistep.py` + `agent_loop.py`)
- `update_plan(steps=[{text,status}])` is a loop-level checklist tool (not in `TOOLS`; handled inside `AgentLoop`). While steps are `pending`/`in_progress` the loop won't let the turn end on a plain reply (max 2 reminders).
- Broken-promise ladder: a reply that ends with "I'll search/read/edit..." and no tool call is (1) nudged, (2) if it was a web search the harness runs `web_search` itself (queries quoted in the text, else the user's message), (3) otherwise reported honestly. Emoji after the promise are ignored.
- `agent_trace.jsonl` events: `broken_promise`, `auto_act`, `plan_nudge`.

## Tools (implemented in `execute_tool_call` in api.py)
Files: `read_local_file`, `edit_local_file`, `write_local_file`, `list_local_files`, `find_file`, `send_file`.
Web: `web_search`, `web_extract`. Vision: `analyze_image`. Shell: `run_shell_command`.
Voice/media: `send_tts`, `send_sticker`. Memory: `remember`, `recall_memory`, `forget`, `search_conversations`.
Others: Google Drive/Docs, computer-use (`cua_*`). The live list is in the prompt.

To ADD a tool: (1) add a definition to `TOOLS` in api.py, (2) add an `if tool_name == "...":` branch in
`execute_tool_call`, (3) optionally a label in `_tool_to_activity`. Restart Lucy Core.

## Stickers
`send_sticker(emotion, theme="Dark", caption?)`. The file name IS the emotion: `assets/stickers/Dark/Happy.png`,
`assets/stickers/Light/Happy.png`. The emotions are read live from the folders (the list in your prompt is current; this one may not be). At the time of writing both themes have the same 25: Happy, Excited, Confident, Silly,
Relieved, Loving, Grateful, Shy, Cozy, Hopeful, Sad, Disappointed, Lonely, Tired, Bored, Angry, Annoyed, Shocked,
Panicked, Jealous, Confused, Sneaky, Scared, Disgusted, Smug.
Close words are understood too ("glad" -> Happy, "terrified" -> Scared). There is no stored mapping to maintain:
to add a sticker, put `<Emotion>.png` in the theme folder(s) - it can be used on the very next message, no restart or code change.
`assets/stickers/User/` is the user's own picker folder, not yours.

## Editing yourself safely
- Read the file first, then `edit_local_file` with a small, exact `old_string`. Avoid rewriting whole files with
  `write_local_file` (large files do not fit in one tool call).
- Python and JSON edits are syntax-checked before saving; a bad edit is refused. Every change is backed up.
- Code under `src/` and the persona only take effect after Lucy Core restarts. You cannot restart it yourself:
  tell the user. If something breaks, the newest `.bak` in `runtime/backups/<path>/` is the previous version.
- Only say you changed something when the tool result confirms it.

## Memory
`remember` saves one short sentence (category: preference/instruction/fact/person/project/other; importance 1-5).
Preferences and instructions are loaded into every conversation. `recall_memory` lists/searches, `forget` deletes by id,
`search_conversations` searches earlier chats. Saying "I'll remember" without calling `remember` saves nothing.

## Diagnosing problems
`runtime/logs/agent_trace.jsonl` shows every tool call with timing and result size; `lucy_core.log` has errors.

## Proactive system (`src/lucy/proactive/`) - Lucy can message first
- Engine loop (every 10 s) started from api.py on startup. State: `runtime/data/proactive.sqlite` (reminders, events log, kv). Settings: `.core/proactive.yaml` (+ runtime overrides via `proactive_control`).
- **Reminders / follow-ups** (explicit, always fire on time, even in quiet hours): tools `set_reminder(message, when, repeat?, every_minutes?, replaces_id?)`, `schedule_task(task, when)` (you run it later with READ-ONLY tools: web_search, web_extract, read/list/find files, recall_memory, search_conversations - never shell/edit), `manage_reminders(list|cancel|cancel_all)`. `when` is parsed by the harness ("in 20 minutes", "tomorrow 9am", "friday 5pm", "oct 20 8:30", ISO). A missed one-off is still delivered, labelled "was due ...".
- **Initiative** (your own idea, gated by policy): greeting when the chat opens after 6 h+; check-in after 3 h idle (daytime only; you may answer SKIP); alerts (model failed to load, disk < 5 GB, log > 50 MB). Held back by: pause, mute, quiet hours (23:00-07:00), rest days (Thursday: no check-ins), no open window, a reply in progress, caps (3/h, 8/day), back-off after ignored messages (4 ignored in a row = silent until the user writes).
- `proactive_control(status|pause|resume|mute|unmute|quiet_hours)` - call it when the user wants less. `pause scope=all` also holds reminders.
- Messages are stored as ordinary assistant messages in the most recently used chat and pushed to open windows over SSE (`/api/proactive/stream`; client `proactive.js` is injected into the chat page). API: `/api/proactive/{status,reminders,control,test,feedback,pending}`.
- History sent to the model is normalized (consecutive assistant messages merged) because the templates need strict alternation. The prompt's last user message carries a `Now:` clock note.
- Reminders/alerts are templates (no model call). Greetings/check-ins use the model only if it is already running, 1 short call; the llama slot is single, so that call costs the next chat turn a prompt reload.
- Logs: `lucy_core.log` lines starting `proactive:`. Tests: `tests/test_proactive*.py`.
