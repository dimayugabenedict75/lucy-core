"""Self-knowledge for the agent: an always-accurate map of the harness she is running inside.

build_harness_digest() produces a compact system message that is sent with every conversation. Facts that
can change (tool list, sticker folders, paths) are read from the live system, never hand-written.
The longer hand-written reference is <project>/.core/harness.md (she can read and improve it).
"""

from __future__ import annotations

from pathlib import Path

from lucy.server.stickers_tool import StickerLibrary

# file -> what it does. Only files that exist are listed.
KEY_FILES = [
    ("src/lucy/server/api.py", "HTTP API on port 8090, every tool's implementation, prompt building, session storage"),
    ("src/lucy/server/agent_loop.py", "the tool-calling loop (retries, tool validation, timeouts, nudges)"),
    ("src/lucy/server/llm_manager.py", "starts/stops the Lucy 12B (8080) and Lucy 4B (8081) llama-servers; temperature, reasoning, KV cache"),
    ("src/lucy/server/tts_filter.py", "keeps tool calls, paths and markup out of the voice"),
    ("src/lucy/server/stickers_tool.py", "send_sticker: emotion -> sticker file"),
    ("src/lucy/server/file_guard.py", "path resolution, safe edits, syntax checks and backups for the file tools"),
    ("src/lucy/proactive/", "reminders, scheduled follow-up tasks, greetings/check-ins/alerts: you can message first"),
    (".core/proactive.yaml", "settings of the proactive system (quiet hours, rest days, limits); edit and restart"),
    ("src/lucy/memory/ + src/lucy/rag/conductor.py", "long-term memory (runtime/data/knowledge.sqlite) and past-chat search"),
    ("ui/chat.html, chat.js, chat.css", "the web chat UI and Settings"),
    ("lucy_audio/", "Lucy Audio text-to-speech server (port 8091), DirectSound player"),
    (".core/personas/lucy.md", "your CORE persona (the user's; locked: you cannot edit it)"),
    (".core/personas/lucy_self.md", "your SELF notes: change them only with the update_self tool (re-read every message)"),
    (".core/harness.md", "longer reference about this harness"),
    ("runtime/", "sessions.sqlite, data/, logs/ (lucy_core.log, agent_trace.jsonl, llama_*.log), backups/"),
    ("assets/stickers/", "your stickers, one PNG per emotion (Dark/, Light/); User/ is the user's own picker folder"),
]


def _reach(safe_root: Path) -> str:
    try:
        from lucy.tools import _common
        return _common.allowed_roots_text()
    except Exception:                                    # noqa: BLE001
        return safe_root.as_posix()


def _hint() -> str:
    try:
        from lucy.tools import _common
        return _common.safety_hint()
    except Exception:                                    # noqa: BLE001
        return ""


def build_harness_digest(tools: list[dict], project_root: Path, safe_root: Path) -> str:
    root = project_root.as_posix()
    files = []
    for rel, what in KEY_FILES:
        first = rel.split(",")[0].split(" + ")[0].strip().rstrip("/")
        if (project_root / first).exists():
            files.append(f"  - {rel}: {what}")
    tool_names = ", ".join(t["function"]["name"] for t in tools if t.get("function", {}).get("name"))
    try:
        stickers = StickerLibrary(project_root, safe_root).summary()
    except Exception:
        stickers = ""
    lines = [
        "YOUR HARNESS (Lucy Core) - this is accurate and current; use it instead of guessing or asking the user:",
        f"- You run inside Lucy Core, located at {root}. Your file tools work anywhere under {_reach(safe_root)}; "
        f"relative paths (e.g. src/lucy/server/api.py) resolve from the Lucy Core folder. {_hint()}",
        "- Layout:",
        *files,
        f"- Your tools: {tool_names}.",
    ]
    if stickers:
        lines.append(f"- You DO have a sticker library ({stickers}). Send one with the send_sticker tool "
                     "(emotion, theme; both themes have the same emotions). Never say you have no stickers, and never write sticker file paths or "
                     "MEDIA: lines yourself.")
    lines += [
        "- You CAN read and edit your own code and persona with read_local_file / edit_local_file (small "
        "old_string -> new_string edits; read the file first). Python/JSON edits are syntax-checked before saving "
        "and every changed file is backed up to runtime/backups/. Code and persona changes only take effect "
        "after Lucy Core restarts - tell the user that. Never claim a change was made unless the tool result says so.",
        "- Memory persists only through the `remember` tool (and what is already in [USER FACTS]); a past "
        "conversation is only visible through `search_conversations`. 'I'll remember' without calling the tool means nothing.",
        "- If asked to 'familiarize yourself' with the harness: this list is the map; read specific files with "
        f"read_local_file as needed (start with {root}/.core/harness.md). Don't just promise to - do it or answer from this map.",
    ]
    return "\n".join(lines)
