#!/usr/bin/env python
"""Wire the proactive system into an existing Lucy Core checkout.  Idempotent, backs up, verifies.

    python tools/install_proactive.py [path/to/Lucy_Core]

Edits (each skipped if already applied):  api.py, agent_loop.py, multistep.py, harness_info.py,
.core/harness.md, .core/personas/lucy.md.  Line endings (CRLF/LF) are preserved. Every edited Python file is
compiled before it is written; if any anchor text is missing (api.py changed a lot?) nothing is written.
"""
from __future__ import annotations
import py_compile, shutil, sys, tempfile, time
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]

API_TAIL = '''

# =====================================================================================================
# === Proactive system: reminders, follow-up tasks, greetings, check-ins, alerts (lucy/proactive/) ===
# Lucy can message FIRST. Everything it needs from this file is handed over in one ProactiveHost below.
# =====================================================================================================
def _proactive_pick_session() -> str:
    """Where unprompted messages go: the most recently used chat (never the CLI sessions)."""
    with _db_lock:
        row = _conn.execute("SELECT session_id FROM sessions WHERE session_id NOT LIKE 'cli-%' "
                            "ORDER BY updated_at DESC LIMIT 1").fetchone()
    sid = row["session_id"] if row else "default"
    _ensure_session(sid)
    return sid


def _proactive_is_busy() -> bool:
    return any(not ev.is_set() for ev in list(_session_inflight.values()))


async def _proactive_llm_ready() -> bool:
    """The model is already up (we never start or wait for it just to say hello)."""
    return llm.phase not in ("loading", "stopping") and await llm._healthy(llm.active["port"])


async def _proactive_llm_complete(messages: list, opts: dict) -> str:
    payload = {"model": llm.model_name, "messages": messages, "temperature": opts.get("temperature", 0.7),
               "max_tokens": opts.get("max_tokens", 200), "stream": False, **llm.request_extras()}
    async with httpx.AsyncClient(timeout=opts.get("timeout", 45.0)) as client:
        resp = await client.post(f"{llm.api_url}/chat/completions", json=payload)
    if resp.status_code != 200:
        raise RuntimeError(f"model returned HTTP {resp.status_code}")
    return resp.json()["choices"][0]["message"].get("content") or ""


async def _proactive_run_agent(messages: list, allowed: list, timeout: float, max_rounds: int) -> str:
    """A scheduled follow-up task: the normal agent loop, but with ONLY the allowed (read-only) tools."""
    allowed_set = set(allowed)
    tools = [TOOL_REGISTRY[n] for n in allowed if n in TOOL_REGISTRY]
    if not tools:
        raise RuntimeError("none of the allowed task tools exist")

    async def guarded(name: str, args: dict) -> str:
        if name not in allowed_set:
            return f"Error: {name} is not available in a background task."
        return await execute_tool_call(name, args)

    agent = AgentLoop(
        brain=_BRAIN, tools=tools, execute=guarded, activity_for=_tool_to_activity, count_tokens=_count_tokens,
        temperature=min(llm.temperature, 0.5),
        cfg=LoopConfig(max_rounds=max_rounds, max_tokens=LUCY_MAX_TOKENS, tool_timeout=LUCY_TOOL_TIMEOUT,
                       ctx_tokens=int(llm.cfg.get("context_length", 100000)), http_timeout=LUCY_TIMEOUT,
                       plan_tool=False),
        trace=_agent_trace, session_id="proactive-task")
    text = ""

    async def consume():
        nonlocal text
        async for ev in agent.run(messages):
            if ev.kind == "content":
                text += ev.data
            elif ev.kind == "error":
                raise RuntimeError(str((ev.data or {}).get("detail") or (ev.data or {}).get("error")))

    _current_session.set(None)
    await asyncio.wait_for(consume(), timeout=timeout)
    return text


def _proactive_sticker(emotion: str):
    try:
        res = _send_sticker({"emotion": emotion, "theme": "Dark"}, _STICKERS)
    except Exception:                                     # noqa: BLE001
        return None
    return res.split(" ", 1)[0].replace("MEDIA:", "") if isinstance(res, str) and res.startswith("MEDIA:") else None


async def _proactive_speak(text: str) -> None:
    """Speak through Lucy Audio on this PC - only when the DirectSound player is available."""
    if _get_native_player() is None:
        return
    clean = _clean_text_for_tts(text)
    if clean:
        async for _ in _stream_tts_to_sse(clean, "frieren"):
            pass


def _proactive_app_timezone() -> str:
    with _db_lock:
        row = _conn.execute("SELECT value FROM tz_config WHERE key = 'timezone'").fetchone()
    return row["value"] if row else "system"


def _proactive_health() -> dict:
    return {"llm_phase": llm.phase, "llm_error": llm.message if llm.phase == "error" else None,
            "llm_label": llm.active["label"]}


@app.on_event("startup")
async def _proactive_start():
    try:
        _proactive.init(_proactive.ProactiveHost(
            root=_paths.ROOT, runtime=_paths.RUNTIME, append_message=_append_message,
            pick_session=_proactive_pick_session, recent_messages=lambda sid, n: _get_messages(sid)[-n:],
            is_busy=_proactive_is_busy, health=_proactive_health, app_timezone=_proactive_app_timezone,
            persona=lambda: _persona_prompt, llm_ready=_proactive_llm_ready, llm_complete=_proactive_llm_complete,
            run_agent=_proactive_run_agent, memory_context=_memory_manager.get_contextual_memory,
            sticker=_proactive_sticker, speak=_proactive_speak))
        await _proactive.start()
    except Exception as e:                                # noqa: BLE001 - Lucy must start even if this cannot
        logger.error(f"Proactive system failed to start: {e}", exc_info=True)


@app.on_event("shutdown")
async def _proactive_stop():
    await _proactive.stop()


app.include_router(_proactive.router, dependencies=[Depends(verify_api_key)])
app.include_router(_proactive.public_router)
'''

REMINDER_RULE = ('        "(4) REMINDERS & FOLLOW-UPS: when the user asks to be reminded of something, or you say you will "\n'
                 '        "remind them or check back later, CALL set_reminder (plain reminder) or schedule_task (you run a "\n'
                 '        "read-only task later and report) - never just promise, and only say it is set after the tool "\n'
                 '        "confirms. Messages you send on your own initiative (reminders, greetings, check-ins) appear in the "\n'
                 '        "chat as your own earlier messages. If the user wants fewer of them, call proactive_control."\n')

# (file, [(old, new, count)])  count = how many occurrences are expected / replaced
EDITS = {
    "src/lucy/server/api.py": [
        ('from lucy.server.harness_info import build_harness_digest   # accurate map of Lucy Core for the agent\n',
         'from lucy.server.harness_info import build_harness_digest   # accurate map of Lucy Core for the agent\n'
         'from lucy import proactive as _proactive                    # reminders, follow-ups, greetings (Lucy can speak first)\n', 1),
        ('_MEMORY_TOOLS = ("remember", "recall_memory", "forget", "search_conversations")\n',
         '_MEMORY_TOOLS = ("remember", "recall_memory", "forget", "search_conversations")\n'
         'TOOLS.extend(_proactive.TOOL_DEFS)       # set_reminder, schedule_task, manage_reminders, proactive_control\n', 1),
        ('    tool_names.update(_MEMORY_TOOLS)          # memory + past-chat search are always available\n',
         '    tool_names.update(_MEMORY_TOOLS)          # memory + past-chat search are always available\n'
         '    tool_names.update(_proactive.TOOL_NAMES)  # reminders / follow-ups are always available\n', 1),
        ('    logger.info(f"Executing tool: {tool_name} with args: {args}")\n',
         '    logger.info(f"Executing tool: {tool_name} with args: {args}")\n\n'
         '    if tool_name in _proactive.TOOL_NAMES:\n'
         '        return await _proactive.run_tool(tool_name, args, _current_session.get())\n', 1),
        ('    messages.extend(_get_messages(session_id)[-MAX_CONTEXT_MESSAGES:])\n',
         '    # Lucy can now speak first, so stored history may hold two assistant messages in a row: repair for the template\n'
         '    _hist = _get_messages(session_id)\n'
         '    messages.extend(_proactive.normalize_history(_hist[-MAX_CONTEXT_MESSAGES:], truncated=len(_hist) > MAX_CONTEXT_MESSAGES))\n'
         '    # The clock goes on the LAST message only, and only when the message is about time (keeps the KV cache valid, avoids noise)\n'
         '    _note = _proactive.now_note() if _proactive.wants_clock(user_message) else ""\n'
         '    if _note:\n'
         '        user_message = f"{user_message}\\n\\n[Harness note - internal, do not mention or quote it] {_note}"\n', 1,
         '    # Lucy can now speak first, so stored history may hold two assistant messages in a row: repair for the template\n    _hist = _get_messages(session_id)\n    messages.extend(_proactive.normalize_history(_hist[-MAX_CONTEXT_MESSAGES:], truncated=len(_hist) > MAX_CONTEXT_MESSAGES))\n    # The clock goes on the LAST message only (changing text earlier in the prompt would invalidate the KV cache)\n    _note = _proactive.now_note()\n    if _note:\n        user_message = f"{user_message}\\n\\n[Harness note - internal, do not mention or quote it] {_note}"\n'),
        ('        "then use edit_local_file with the correct old_string."\n    )})\n',
         '        "then use edit_local_file with the correct old_string. "\n' + REMINDER_RULE + '    )})\n', 1),
        ('    _append_message(session_id, "user", message)\n',
         '    _append_message(session_id, "user", message)\n    _proactive.touch(session_id)\n', 2),
        ('            reply_done.set()\n', '            reply_done.set()\n            _proactive.touch(session_id)\n', 1),
        ('    html = html.replace("__JS_VERSION__", _asset_version("chat.js"))\n    return HTMLResponse(content=html)\n',
         '    html = html.replace("__JS_VERSION__", _asset_version("chat.js"))\n'
         '    html = _proactive.inject_client(html, DEV_API_KEY)      # shows what Lucy says first (toast / notification)\n'
         '    return HTMLResponse(content=html)\n', 1),
        ('    "search_conversations": "history",\n}\n',
         '    "search_conversations": "history",\n'
         '    "set_reminder": "remember", "schedule_task": "remember", "manage_reminders": "recall",\n'
         '    "proactive_control": "remember",       # reuse existing UI labels: no chat.html change needed\n}\n', 1),
        ('    # No usable detail in args — fall back to a plain category label.\n',
         '    if tool_name == "set_reminder" and args.get("message"):\n'
         '        return f"remember:reminder: {_truncate(args[\'message\'], 32)}"\n'
         '    if tool_name == "schedule_task" and args.get("task"):\n'
         '        return f"remember:follow-up: {_truncate(args[\'task\'], 32)}"\n\n'
         '    # No usable detail in args — fall back to a plain category label.\n', 1),
    ],
    "src/lucy/server/agent_loop.py": [
        ('     r"web|online|internet|google|results", {"web_search", "web_extract"}),\n]',
         '     r"web|online|internet|google|results", {"web_search", "web_extract"}),\n'
         '    ("set or changed a reminder", r"set|scheduled|created|added|saved|updated|changed|cancell?ed|removed|deleted",\n'
         '     r"reminder|alarm|timer|follow-?up", {"set_reminder", "schedule_task", "manage_reminders"}),\n]', 1),
        ('            promise = None                                     # "I\'ll remember that" after remember() already ran\n',
         '            promise = None                                     # "I\'ll remember that" after remember() already ran\n'
         '        if promise and ({"set_reminder", "schedule_task"} & st.ok_tools) and re.search(r"remind|schedul|follow", text, re.IGNORECASE):\n'
         '            promise = None                                     # "I\'ll remind you" after set_reminder() already ran\n', 1),
    ],
    "src/lucy/server/multistep.py": [
        # "I'll start by reading the file" / "I'm going to pull the contents" were not recognised as promises.
        # 4th element = what an earlier release of this installer wrote (upgrade in place).
        ('r"investigate|examine|inspect|analy[sz]e|send|compare|gather|collect|grab|query|try")',
         'r"investigate|examine|inspect|analy[sz]e|send|compare|gather|collect|grab|query|try|remind|schedule|follow\\s*up|"\n'
         '           r"pull|load|retrieve|reading|checking|running|executing|opening|writing|editing|pulling|loading|searching")', 1,
         'r"investigate|examine|inspect|analy[sz]e|send|compare|gather|collect|grab|query|try|remind|schedule|follow\\s*up")'),
        # "I am currently executing the command to read X" is a claim of action in progress
        ('r"i[\'’]m gonna|i shall|time to|now i|next,? i|first,? i|then i|i[\'’]ll now|i[\'’]m now")',
         'r"i[\'’]m gonna|i shall|time to|now i|next,? i|first,? i|then i|i[\'’]ll now|i[\'’]m now|"\n'
         '         r"i am currently|i[\'’]m currently|i[\'’]m in the process of")', 1),
        ('def find_promise(text: str, max_chars: int = 900)', 'def find_promise(text: str, max_chars: int = 1600)', 1),
    ],
    "src/lucy/server/harness_info.py": [
        ('    ("src/lucy/memory/ + src/lucy/rag/conductor.py",',
         '    ("src/lucy/proactive/", "reminders, scheduled follow-up tasks, greetings/check-ins/alerts: you can message first"),\n'
         '    (".core/proactive.yaml", "settings of the proactive system (quiet hours, rest days, limits); edit and restart"),\n'
         '    ("src/lucy/memory/ + src/lucy/rag/conductor.py",', 1),
    ],
}
APPEND = {"src/lucy/server/api.py": API_TAIL}

HARNESS_SECTION = '''
## Proactive system (`src/lucy/proactive/`) - Lucy can message first
- Engine loop (every 10 s) started from api.py on startup. State: `runtime/data/proactive.sqlite` (reminders, events log, kv). Settings: `.core/proactive.yaml` (+ runtime overrides via `proactive_control`).
- **Reminders / follow-ups** (explicit, always fire on time, even in quiet hours): tools `set_reminder(message, when, repeat?, every_minutes?, replaces_id?)`, `schedule_task(task, when)` (you run it later with READ-ONLY tools: web_search, web_extract, read/list/find files, recall_memory, search_conversations - never shell/edit), `manage_reminders(list|cancel|cancel_all)`. `when` is parsed by the harness ("in 20 minutes", "tomorrow 9am", "friday 5pm", "oct 20 8:30", ISO). A missed one-off is still delivered, labelled "was due ...".
- **Initiative** (your own idea, gated by policy): greeting when the chat opens after 6 h+; check-in after 3 h idle (daytime only; you may answer SKIP); alerts (model failed to load, disk < 5 GB, log > 50 MB). Held back by: pause, mute, quiet hours (23:00-07:00), rest days (Thursday: no check-ins), no open window, a reply in progress, caps (3/h, 8/day), back-off after ignored messages (4 ignored in a row = silent until the user writes).
- `proactive_control(status|pause|resume|mute|unmute|quiet_hours)` - call it when the user wants less. `pause scope=all` also holds reminders.
- Messages are stored as ordinary assistant messages in the most recently used chat and pushed to open windows over SSE (`/api/proactive/stream`; client `proactive.js` is injected into the chat page). API: `/api/proactive/{status,reminders,control,test,feedback,pending}`.
- History sent to the model is normalized (consecutive assistant messages merged) because the templates need strict alternation. The prompt's last user message carries a `Now:` clock note.
- Reminders/alerts are templates (no model call). Greetings/check-ins use the model only if it is already running, 1 short call; the llama slot is single, so that call costs the next chat turn a prompt reload.
- Logs: `lucy_core.log` lines starting `proactive:`. Tests: `tests/test_proactive*.py`.
'''
PERSONA_BULLET = ("- **Reminders & follow-ups:** If the user asks to be reminded, or you say you'll check back later, call "
                  "`set_reminder` / `schedule_task` - never just promise. You may message first (greetings, check-ins); if the user "
                  "wants less, call `proactive_control`. Rest day (Thursday): no nudging.")


def read(p: Path):
    raw = p.read_bytes().decode("utf-8")
    crlf = "\r\n" in raw
    return raw.replace("\r\n", "\n"), crlf


def main() -> int:
    plan: dict[Path, tuple[str, bool]] = {}
    problems = []
    for rel, edits in EDITS.items():
        p = ROOT / rel
        if not p.exists():
            problems.append(f"missing file {rel}"); continue
        text, crlf = read(p)
        before = text
        for edit in edits:
            old, new, count = edit[:3]
            if new in text:                           # this edit is already applied
                continue
            if len(edit) > 3 and edit[3] in text:     # an older release of this edit is installed: upgrade it
                text = text.replace(edit[3], new)
                continue
            n = text.count(old)
            if n != count:
                problems.append(f"{rel}: expected {count} match(es) for {old[:70]!r}, found {n}")
            else:
                text = text.replace(old, new)
        if rel in APPEND and "def _proactive_pick_session" not in text:
            text = text.rstrip("\n") + "\n" + APPEND[rel]
        if text == before:
            print(f"= {rel}: already up to date"); continue
        plan[p] = (text, crlf)
    # docs: harness.md + persona
    h = ROOT / ".core" / "harness.md"
    if h.exists():
        text, crlf = read(h)
        if "## Proactive system" not in text:
            plan[h] = (text.rstrip("\n") + "\n" + HARNESS_SECTION, crlf)
    pp = ROOT / ".core" / "personas" / "lucy.md"
    if pp.exists():
        text, crlf = read(pp)
        if "Reminders & follow-ups" not in text:
            plan[pp] = (text.rstrip("\n") + "\n" + PERSONA_BULLET, crlf)
    if problems:
        print("NOTHING WRITTEN. Problems:\n  " + "\n  ".join(problems)); return 1
    # verify python compiles before touching anything
    for p, (text, crlf) in plan.items():
        if p.suffix == ".py":
            with tempfile.TemporaryDirectory() as d:
                t = Path(d) / p.name; t.write_text(text, encoding="utf-8")
                try:
                    py_compile.compile(str(t), doraise=True)
                except py_compile.PyCompileError as e:
                    print(f"NOTHING WRITTEN. {p.name} would not compile: {e}"); return 1
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for p, (text, crlf) in plan.items():
        bk = ROOT / "runtime" / "backups" / "proactive_install" / stamp / p.relative_to(ROOT)
        bk.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(p, bk)
        out = text.replace("\n", "\r\n") if crlf else text
        p.write_bytes(out.encode("utf-8"))
        print(f"+ {p.relative_to(ROOT)} updated (backup: {bk.relative_to(ROOT)})")
    print("Done. Restart Lucy Core." if plan else "Nothing to do.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
