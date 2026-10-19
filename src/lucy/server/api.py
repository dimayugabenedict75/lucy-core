from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse, JSONResponse, Response
from pydantic import BaseModel
from typing import List, Dict, Optional, Any, Iterator, Iterable
import httpx
import asyncio
import logging
import time
import json
import os
import subprocess
import zlib
from pathlib import Path

# --- Lucy Core Imports ---
import re
import sys
# Put THIS project's src/ first so another install of `lucy` (e.g. the Hermes
# venv's editable agents-harness) can't shadow it. Derived from this file's
# location rather than a hardcoded user path.
_SRC_DIR = str(Path(__file__).resolve().parents[2])
if _SRC_DIR in sys.path:
    sys.path.remove(_SRC_DIR)
sys.path.insert(0, _SRC_DIR)

from lucy.memory.manager import MemoryManager
from lucy.skills.memory.hook import detect_memory_candidate, render_confirmation
from lucy.skills.skills_manager import SkillsManager
from lucy.server.voice_control import router as voice_router
from lucy import paths as _paths
from contextvars import ContextVar
from lucy.server.llm_manager import llm                      # model switching, temperature, reasoning, KV cache
from lucy.server.agent_loop import AgentLoop, LoopConfig     # reliable tool-calling loop (retries, validation, ...)
from lucy.server.tts_filter import TTSStreamFilter, clean_for_speech   # only speakable words reach the voice
from lucy.server import file_guard                           # path resolution, tolerant edits, syntax checks, backups
from lucy.server.stickers_tool import StickerLibrary, send_sticker as _send_sticker
from lucy.server import library as _library_mod            # persistent file/image cache + Library view
from lucy.server.harness_info import build_harness_digest   # accurate map of Lucy Core for the agent
from lucy import proactive as _proactive                    # reminders, follow-ups, greetings (Lucy can speak first)
from lucy import safety as _safety                          # P1: permissions, approvals, audit, undo, constitution lock
from lucy import mcp as _mcp                                # P4: MCP servers (tools found with tool_search, never all in the prompt)
import base64

# Persona is loaded from file
PERSONA_PATH = str(_paths.PERSONA_PATH)

app = FastAPI(title="Lucy Core API")

logger = logging.getLogger("lucy.core.api")
logging.basicConfig(level=logging.INFO)

# --- Log file (read by GET /api/logs and the LOGS button in the chat UI) ---
# Previously nothing wrote this file, so /api/logs always returned an empty list.
LOG_PATH = str(_paths.RUNTIME / "lucy_core.log")


def _setup_file_logging():
    from logging.handlers import RotatingFileHandler
    root = logging.getLogger()
    # Idempotent: uvicorn --reload re-imports this module on every restart.
    if any(getattr(h, "_lucy_log_file", False) for h in root.handlers):
        return
    try:
        Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler._lucy_log_file = True
        root.addHandler(handler)
        # uvicorn's loggers don't propagate to root, so attach there too
        # (request lines, startup/shutdown messages). "uvicorn.error" is
        # deliberately omitted: it propagates into "uvicorn", so adding it
        # as well would write every line twice.
        for name in ("uvicorn", "uvicorn.access"):
            logging.getLogger(name).addHandler(handler)
    except Exception as e:  # never let logging setup stop the server from starting
        logging.getLogger("lucy.core.api").warning(f"File logging disabled: {e}")


_setup_file_logging()


# --- API Key Dependency ---
from fastapi import Depends, Security


async def verify_api_key(request: Request):
    """Verify API key via X-API-Key header or ?api_key= query param."""
    # Allow /api/health to be public (it has its own handler, but this is a backup)
    if request.url.path == "/api/health":
        return True
    # Check header first
    header_key = request.headers.get("X-API-Key")
    if header_key and secrets.compare_digest(header_key, DEV_API_KEY):
        return True
    # Check query param
    query_key = request.query_params.get(API_KEY_QUERY_PARAM)
    if query_key and secrets.compare_digest(query_key, DEV_API_KEY):
        return True
    raise HTTPException(status_code=401, detail="Invalid or missing API key")

# --- Core Configuration ---
# LLM endpoint / model / temperature are live values owned by lucy.server.llm_manager (`llm`):
#   Lucy 12B -> :8080, Lucy 4B -> :8081, chosen in Settings > Model.
LUCY_AUDIO_TTS_URL = "http://127.0.0.1:8091/v1/audio/speech"
LUCY_AUDIO_MODEL = "cielvox26"
# Playback of the TTS delta stream:
#   "directsound" - the server plays the PCM itself through ONE persistent Windows DirectSound
#                   stream (jitter buffer + prebuffer, no gaps between sentences). Plays on the
#                   PC running Lucy Core. Falls back to "browser" if sounddevice is missing.
#   "browser"     - old path: PCM chunk files are sent to the web UI and played with Web Audio
#                   (use this when you chat from a phone/another device).
LUCY_AUDIO_PLAYBACK = os.environ.get("LUCY_AUDIO_PLAYBACK", "directsound").lower()
LUCY_AUDIO_PREBUFFER_MS = int(os.environ.get("LUCY_AUDIO_PREBUFFER_MS", "300"))
LUCY_AUDIO_SAMPLE_RATE = 24000
MAX_CONTEXT_TOKENS = 8192      # token budget for session history + system prompts
MAX_CONTEXT_MESSAGES = 50      # hard ceiling on messages loaded (safety)
COMPRESSION_THRESHOLD = 0.75   # start summarizing when 75% of budget is used
SUMMARY_RESERVE_TOKENS = 2048  # keep this many tokens free for the response
DEFAULT_TEMPERATURE = 0.5
MAX_TOOL_ROUNDS = int(os.environ.get("LUCY_MAX_TOOL_ROUNDS", "0"))    # safety ceiling on tool rounds per turn; 0 = none (a task runs until it is done or you stop it)
LUCY_MAX_TOKENS = int(os.environ.get("LUCY_MAX_TOKENS", "4096"))      # per model call (big tool args need room)
LUCY_TOOL_TIMEOUT = float(os.environ.get("LUCY_TOOL_TIMEOUT", "120")) # seconds before a hung tool is abandoned
# Timeout for llama-server streaming requests (seconds)
# Bumps the default 120s to 600s for training/code-generation tasks.
LUCY_TIMEOUT = float(os.environ.get("LUCY_TIMEOUT", "600.0"))

# Sandbox root for file operations
SAFE_ROOT = _paths.SAFE_ROOT
_HOME = SAFE_ROOT.as_posix()              # shown to the model in tool descriptions
_ROOT_POSIX = _paths.ROOT.as_posix()

# --- API Key Auth ---
# DEV_API_KEY defaults to 'dev-harness' if not set. The /api/health endpoint
# is public; all other /api/* routes require X-API-Key header or ?api_key= query param.
import secrets
import tiktoken

# Use the gpt2 tokenizer as a lightweight, dependency-light BPE approximation.
# The exact token count will differ from the model's native tokenizer, but it's
# accurate enough for budgeting decisions. Calibration constant below is tuned
# for typical English chat text on Qwen/Llama 2-style tokenizers.
_TOKEN_ENCODER = tiktoken.get_encoding("gpt2")
# Empirical multiplier to adjust gpt2 tokens → model-native tokens for
# Qwen-style tokenizers (roughly 1:1.3). Tweak if budget feels off.
_TOKEN_SCALE = 1.0

def _count_tokens(text: str) -> int:
    """Estimate token count for a string using the gpt2 BPE tokenizer.

    Returns model-native token estimate (not raw BPE count).
    """
    try:
        return max(1, int(len(_TOKEN_ENCODER.encode(text)) * _TOKEN_SCALE))
    except Exception:
        # Fallback: ~4 chars per token (very rough)
        return max(1, len(text) // 4)

def _count_message_tokens(messages: list) -> int:
    """Rough token estimate for a list of OpenAI-format messages."""
    total = 0
    for msg in messages:
        # Every message has role + content
        total += 4  # overhead per message
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, str):
            total += _count_tokens(role) + _count_tokens(content)
        elif isinstance(content, list):
            # Multimodal content — estimate text tokens only
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += _count_tokens(part.get("text", ""))
    return total

DEV_API_KEY = os.environ.get("DEV_API_KEY", "dev-harness")
# Allow API key via query param for browser GET requests (e.g. fetch from static HTML)
API_KEY_QUERY_PARAM = "api_key"

# --- CUA Backend (Win32 API) ---
# Import lucy_cua for direct desktop control — real OS cursor/mouse/keyboard
from lucy.cua.lucy_cua import (
    get_cursor_pos,
    set_cursor_pos,
    mouse_click,
    mouse_drag,
    key_combo,
    type_text,
    minimize_all,
)

# Voice control (Lucy Audio Server) — /api/voice/start, /stop, /status.
# Protected the same way as the other /api/* routes.
app.include_router(voice_router, dependencies=[Depends(verify_api_key)])

# Speech-to-text for the chat UI's microphone button: POST /api/stt (see stt.py).
from lucy.server.stt import router as stt_router
app.include_router(stt_router, dependencies=[Depends(verify_api_key)])

# Emoji/sticker picker: lists and serves PNGs from assets/stickers/User (see stickers.py)
try:
    from lucy.server.stickers import router as stickers_router
    app.include_router(stickers_router, dependencies=[Depends(verify_api_key)])
except Exception as _e:  # a missing/broken stickers.py must not stop the whole server
    logger.error(f"Sticker routes not loaded: {_e}")

import sqlite3
import threading

# --- Session Storage (SQLite persisted) ---
_DB_PATH = str(_paths.RUNTIME / "sessions.sqlite")
_PATH = Path(_DB_PATH)
_PATH.parent.mkdir(parents=True, exist_ok=True)

_db_lock = threading.RLock()
_conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row


def _get_tz_modifier():
    """Return the SQLite timezone modifier string for the configured timezone."""
    with _db_lock:
        row = _conn.execute("SELECT value FROM tz_config WHERE key = 'timezone'").fetchone()
    tz = row["value"] if row else "system"
    if tz == "system":
        return "localtime"
    if tz == "utc":
        return "utc"
    # For explicit offsets like "+08:00" or "-05:00"
    return tz


# Create tz_config first so _get_tz_modifier() can query it
_conn.execute("""
    CREATE TABLE IF NOT EXISTS tz_config (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
""")
_conn.execute("INSERT OR IGNORE INTO tz_config (key, value) VALUES ('timezone', 'system')")
_conn.commit()

# Now create sessions and messages with the dynamic timezone default
_tz_mod = _get_tz_modifier()
_conn.execute(f"""
    CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        title      TEXT NOT NULL DEFAULT 'New Session',
        updated_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', '{_tz_mod}'))
    )
""")
_conn.execute(f"""
    CREATE TABLE IF NOT EXISTS messages (
        session_id  TEXT NOT NULL,
        role        TEXT NOT NULL,
        content     TEXT NOT NULL,
        created_at  TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', '{_tz_mod}'))
    )
""")
_conn.execute(f"""
    CREATE TABLE IF NOT EXISTS summaries (
        session_id  TEXT NOT NULL,
        summary     TEXT NOT NULL,
        msg_count   INTEGER NOT NULL,
        token_count INTEGER NOT NULL,
        created_at  TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', '{_tz_mod}'))
    )
""")
_conn.execute("""
    CREATE TABLE IF NOT EXISTS connectors (
        connector_id  INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT NOT NULL UNIQUE,
        token         TEXT,
        created_at    TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
        updated_at    TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
    )
""")
_conn.commit()

# --- Attachments + Library (see lucy/server/library.py) ---
try:
    _conn.execute("ALTER TABLE messages ADD COLUMN attachments TEXT")     # JSON list of files/images on a message
except sqlite3.OperationalError:
    pass                                                                  # column already exists
_conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id)")
_conn.commit()


def _now_str() -> str:
    with _db_lock:
        row = _conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%S', 'now', ?) AS now", (_get_tz_modifier(),)).fetchone()
    return row["now"] if row else time.strftime("%Y-%m-%d %H:%M:%S")


_library = _library_mod.Library(_paths.CACHE_DIR, _conn, _db_lock, _paths.ROOT / "assets" / "stickers", _now_str)
try:
    # Sessions that were created but never used (old behaviour created one per click / per refresh)
    with _db_lock:
        _conn.execute(
            "DELETE FROM sessions WHERE session_id NOT IN (SELECT DISTINCT session_id FROM messages) "
            "AND updated_at < strftime('%Y-%m-%d %H:%M:%S', 'now', ?, '-1 hour')", (_get_tz_modifier(),))
        _conn.commit()
    _library.reconcile()
except Exception as _e:                                                   # housekeeping must never block startup
    logger.warning(f"Library housekeeping skipped: {_e}")
app.include_router(_library_mod.build_router(lambda: _library), dependencies=[Depends(verify_api_key)])


def _mask_token(token: str) -> str:
    """Mask a token for display: show last 4 chars, rest as asterisks."""
    if not token or len(token) < 4:
        return "****"
    return "*" * (len(token) - 4) + token[-4:]


def _session_exists(session_id: str) -> bool:
    with _db_lock:
        row = _conn.execute("SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    return row is not None


def _ensure_session(session_id: str):
    with _db_lock:
        _conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, title) VALUES (?, 'New Session')",
            (session_id,),
        )
        _conn.commit()


def _fallback_session_title(content: str) -> str:
    """Fast fallback: summarize the first message to <=10 words.

    Takes the first sentence or first ~10 words, whichever is shorter.
    This is a heuristic summary, not a raw prompt dump.
    """
    text = content.strip()
    # Try to find first sentence boundary (period, exclamation, question mark)
    truncated = text
    for sep in ['. ', '! ', '? ']:
        idx = text.find(sep)
        if idx > 0:
            truncated = text[:idx]
            break
    # Then trim to 10 words
    words = truncated.strip().split()[:10]
    return ' '.join(words).rstrip('.,;:!?')


async def _generate_session_title(session_id: str):
    """Generate a session title from the first user message if title is 'New Session'.

    Uses the local llama.cpp model to produce a concise Claude-style summary
    (e.g. 'Build a web scraper with Python') instead of raw truncation.
    Falls back to truncation if the LLM is unavailable. Never blocks the chat.
    """
    with _db_lock:
        row = _conn.execute(
            "SELECT title FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not row or row["title"] != "New Session":
            return

        first_msg = _conn.execute(
            "SELECT content FROM messages WHERE session_id = ? AND role = 'user' ORDER BY rowid ASC LIMIT 1",
            (session_id,),
        ).fetchone()
        if not first_msg:
            return

        content = first_msg["content"]

    # Try LLM-based title generation (fast, non-blocking)
    title = await _llm_title_from_query(content)

    if not title:
        title = _fallback_session_title(content)

    with _db_lock:
        _conn.execute(
            "UPDATE sessions SET title = ? WHERE session_id = ?",
            (title, session_id),
        )
        _conn.commit()


async def _llm_title_from_query(content: str) -> Optional[str]:
    """Ask the local llama.cpp model for a concise session title.

    Returns the title string, or None if the request fails.
    Uses a short timeout so it never blocks the chat pipeline.
    Maximum 10 words — Claude-style concise summary, not a prompt dump.
    """
    prompt = (
        "You are a title-generation assistant. Summarize the user's message "
        "as a concise session title: lowercase, 4-7 words max, "
        "no quotes, no punctuation. Just the title.\n\n"
        f"User message: {content.strip()}\n"
        "Title:"
    )
    payload = {
        "model": llm.model_name,
        "messages": [
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 32,
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{llm.api_url}/chat/completions", json=payload
            )
            if resp.status_code != 200:
                logger.warning(
                    f"Title generation failed (HTTP {resp.status_code})"
                )
                return None
            data = resp.json()
            title = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            title = title.strip()
            # Clean up any stray punctuation/quotes the model added
            title = title.strip('"\'').strip()
            title = title.rstrip(".,;:!?")
            # Enforce max 10 words — reject if over
            if not title or len(title.split()) > 10 or len(title) > 80:
                return None
            return title
    except Exception as e:
        logger.debug(f"Title generation error: {e}")
        return None


def _get_messages(session_id: str) -> list[dict]:
    _ensure_session(session_id)
    with _db_lock:
        rows = _conn.execute(
            "SELECT role, content, created_at FROM messages WHERE session_id = ? ORDER BY rowid ASC",
            (session_id,),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"], "timestamp": r["created_at"]} for r in rows]


def _get_messages_full(session_id: str) -> list[dict]:
    """Messages for the web UI: like _get_messages, plus each message's attachments (files, images, stickers)."""
    with _db_lock:
        rows = _conn.execute(
            "SELECT role, content, created_at, attachments FROM messages WHERE session_id = ? ORDER BY rowid ASC",
            (session_id,),
        ).fetchall()
    out, all_shas = [], set()
    for r in rows:
        atts = []
        if r["attachments"]:
            try:
                atts = json.loads(r["attachments"])
            except (ValueError, TypeError):
                atts = []
        all_shas.update(a.get("sha") for a in atts if a.get("sha"))
        out.append({"role": r["role"], "content": r["content"], "timestamp": r["created_at"], "attachments": atts})
    alive = _library.existing_shas(all_shas)
    for m in out:
        for a in m["attachments"]:
            if a.get("sha"):
                a["missing"] = a["sha"] not in alive
    return out


def _append_message(session_id: str, role: str, content: str, attachments: list | None = None) -> int:
    """Store one message. `attachments` = records from the Library (or sticker references). Returns its rowid."""
    _ensure_session(session_id)
    atts = [a for a in (attachments or []) if a]
    with _db_lock:
        cur = _conn.execute(
            "INSERT INTO messages (session_id, role, content, attachments) VALUES (?, ?, ?, ?)",
            (session_id, role, content, json.dumps(atts, ensure_ascii=False) if atts else None),
        )
        rowid = cur.lastrowid
        _conn.execute(
            f"UPDATE sessions SET updated_at = strftime('%Y-%m-%d %H:%M:%S', 'now', '{_get_tz_modifier()}') WHERE session_id = ?",
            (session_id,),
        )
        _conn.commit()
    if atts:
        _library.add_refs(session_id, rowid, "user" if role == "user" else "assistant", atts)
    return rowid


def _list_sessions_db(include_messages: bool = False, hide_empty: bool = False) -> dict:
    """Session list for the sidebar. Messages are NOT loaded unless asked (the UI fetches them per session)."""
    with _db_lock:
        rows = _conn.execute(
            "SELECT s.session_id, s.title, COUNT(m.rowid) AS message_count, s.updated_at FROM sessions s "
            "LEFT JOIN messages m ON s.session_id = m.session_id GROUP BY s.session_id ORDER BY s.updated_at DESC"
        ).fetchall()
    out = {}
    for r in rows:
        if hide_empty and not r["message_count"]:
            continue                      # an unused "New Session" is not a conversation yet
        entry = {"title": r["title"], "message_count": r["message_count"], "updated_at": r["updated_at"]}
        if include_messages:
            entry["messages"] = _get_messages(r["session_id"])
        out[r["session_id"]] = entry
    return out


def _delete_session_db(session_id: str) -> bool:
    with _db_lock:
        cur = _conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        _conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        _conn.commit()
        deleted = cur.rowcount > 0
    _library.delete_session(session_id)       # drop cached files that only this session used
    return deleted

# --- Instantiating Core Systems ---
_memory_manager = MemoryManager()
_skills_manager = SkillsManager()

# --- Load Persona ---
try:
    with open(PERSONA_PATH, "r", encoding="utf-8") as f:
        _persona_prompt = f.read()
except FileNotFoundError:
    logger.warning(f"Persona file not found at {PERSONA_PATH}. Using default.")
    _persona_prompt = "You are Lucy, a helpful and witty AI companion."

# --- Tool Definitions ---
# These match the function names from agents-harness/tools.py so the model
# can use the same tool vocabulary whether running in the SDK harness or
# via this API server.
TOOLS = [
    # --- Persistent memory (the agent decides what to keep) ---
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "Save something to your persistent long-term memory so you still know it in future "
                "conversations. Call this on your own initiative, without being asked, when the user "
                "reveals a lasting preference, a fact about themselves / their setup / projects, a "
                "decision, a correction, or a standing instruction ('from now on', 'always', 'never'). "
                "Write ONE short, self-contained sentence (e.g. 'Benny prefers concise answers'). "
                "Do NOT save one-off requests, small talk, temporary details, or secrets (passwords, "
                "tokens, API keys). If an existing memory is outdated or contradicted, pass its id in "
                "replaces_id instead of adding a second, conflicting one."),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The memory: one short self-contained sentence."},
                    "category": {"type": "string",
                                 "enum": ["preference", "instruction", "fact", "person", "project", "other"],
                                 "description": "preference/instruction are always loaded into every conversation."},
                    "importance": {"type": "integer",
                                   "description": "1-5. 5 = must never forget, 3 = normal, 1 = minor detail."},
                    "replaces_id": {"type": "integer",
                                    "description": "Id of an outdated memory this one replaces (see recall_memory)."}
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": (
                "Look up your persistent memory. Pass a query to find memories about a topic, or leave "
                "it empty to list everything you remember. Each result has an id (needed by forget / "
                "remember's replaces_id). The most relevant memories are already shown to you at the "
                "start of each conversation; use this to dig deeper."),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to look for (empty = list all)."},
                    "limit": {"type": "integer", "description": "Max results (default 8)."}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "forget",
            "description": ("Delete one memory by id (get ids from recall_memory). Use when the user asks "
                            "you to forget something or a memory is wrong."),
            "parameters": {
                "type": "object",
                "properties": {"memory_id": {"type": "integer", "description": "Id of the memory to delete."}},
                "required": ["memory_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_conversations",
            "description": (
                "Search the user's PAST conversations (other sessions, not the current one). Use it "
                "whenever the user refers to an earlier chat ('last time', 'we talked about', 'what did I "
                "say about'), or when you need background you don't have, BEFORE answering from guesswork. "
                "Returns matching message snippets with dates and session ids. To read more of one "
                "conversation, call again with that session_id and an empty query."),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Words to look for (empty only with session_id)."},
                    "session_id": {"type": "string", "description": "Limit to / read one session."},
                    "days": {"type": "integer", "description": "Only look at the last N days."},
                    "limit": {"type": "integer", "description": "Max results (default 5, max 15)."}
                },
                "required": []
            }
        }
    }
]

_MEMORY_TOOLS = ("remember", "recall_memory", "forget", "search_conversations")
TOOLS.extend(_proactive.TOOL_DEFS)       # set_reminder, schedule_task, manage_reminders, proactive_control

# Tools living in src/lucy/tools/ (one file per tool, auto-discovered). A registry tool replaces an old inline
# schema of the same name in place; new ones are appended.
from lucy.tools import registry as _tool_registry
from lucy.tools import routing as _routing
_tool_registry.default.discover()
for _spec in _tool_registry.default.all_specs():
    _idx = next((i for i, t in enumerate(TOOLS) if t["function"]["name"] == _spec.name), None)
    if _idx is None:
        TOOLS.append(_spec.schema)
    else:
        TOOLS[_idx] = _spec.schema

# --- Tool Registry (lookup by name for dynamic skill-tool mapping) ---
TOOL_REGISTRY: Dict[str, dict] = {
    tool_def["function"]["name"]: tool_def for tool_def in TOOLS
}

def _resolve_skills_to_tools(skill_list: list[dict]) -> list[dict]:
    """Aggregate unique tool definitions from a list of skills.
    
    Each skill has a 'tools' field (list of tool name strings).
    Returns the full OpenAI-format tool definitions from TOOL_REGISTRY.
    Falls back to ALL tools if no skills specify tools (backward compat).

    Note: write_local_file and edit_local_file and read_local_file are ALWAYS 
    included (never skill-gated) so the model can reliably edit files without 
    skill-matching confusion.
    """
    tool_names = set()
    for skill in skill_list:
        tools = skill.get("tools", [])
        if isinstance(tools, list):
            for t in tools:
                if isinstance(t, str) and t in TOOL_REGISTRY:
                    tool_names.add(t)
    # Always include file I/O tools (Fix 1: stop file I/O from being skill-gated)
    tool_names.add("write_local_file")
    tool_names.add("edit_local_file")
    tool_names.add("read_local_file")
    tool_names.update(_MEMORY_TOOLS)          # memory + past-chat search are always available
    tool_names.update(_proactive.TOOL_NAMES)  # reminders / follow-ups are always available
    tool_names.add("send_sticker")
    if tool_names:
        return [TOOL_REGISTRY[name] for name in sorted(tool_names)]
    return TOOLS  # fallback: all tools available


# --- Tool Execution ---

def _resolve_path(file_path: str) -> Path:
    """Resolve what the model typed to a real path under SAFE_ROOT (None if outside).

    Relative paths resolve from the Lucy Core folder first; "Lucy_Core/src/..." style guesses are understood.
    See lucy.server.file_guard.resolve_path."""
    return file_guard.resolve_path(file_path, SAFE_ROOT, _paths.ROOT)


def _outside_root(file_path: str) -> str:
    return file_guard.outside_root_message(file_path, SAFE_ROOT, _paths.ROOT)


_STICKERS = StickerLibrary(_paths.ROOT, SAFE_ROOT)

# The chat UI sends a sticker as the text "[sticker: 7.png]" plus the PNG itself (so the model can see it).
_STICKER_TEXT_RE = re.compile(r"^\s*\[sticker:\s*[^\]\n]+?\.png\s*\]\s*$", re.IGNORECASE)


def _ingest_generated_media(media_path: str, voice_mode: bool = False) -> dict | None:
    """A file Lucy delivered to the chat (MEDIA: event) -> attachment record for the message history.

    * stickers are NOT cached (they live in assets/stickers): a light path reference is stored instead
    * streaming-TTS chunks (.pcm) and voice-mode speech are not files the user wants in the Library
    * everything else is snapshotted into cache/ as it is right now
    """
    if not media_path or media_path.lower().endswith(".pcm"):
        return None
    target = _resolve_path(media_path)
    if target is None or not target.is_file():
        return None
    if _library.is_sticker_path(target):
        return {"kind": "sticker", "name": target.name, "path": media_path}
    if voice_mode:
        try:
            target.resolve().relative_to((_paths.HERMES_HOME / "cache" / "audio").resolve())
            return None
        except ValueError:
            pass
    return _library.ingest_path(target, "assistant")


def _refresh_sticker_tool() -> None:
    """Re-read assets/stickers so a newly added <Emotion>.png can be used right away (no restart)."""
    try:
        emotions = _STICKERS.all_emotions()
        for t in TOOLS:
            if t["function"]["name"] == "send_sticker":
                t["function"]["parameters"]["properties"]["emotion"]["enum"] = emotions
    except Exception as e:                       # noqa: BLE001 - never break a chat over this
        logger.warning(f"Could not refresh sticker emotions: {e}")


# -- Lucy Audio streaming TTS client (SSE delta stream) --
# This replaces the batch send_tts approach: instead of waiting for the full
# WAV file, we get s16LE PCM chunks as they're generated by the model.
# Each chunk is yielded as a MEDIA: event for immediate playback — true streaming TTS.

_NATIVE_PLAYER = None
_NATIVE_PLAYER_TRIED = False
_CHUNK_LOG_PATH = _paths.RUNTIME / "logs" / "deltastream_chunks.log"


def _get_native_player():
    """Process-wide DirectSound player (created once, kept open across sentences), or None."""
    global _NATIVE_PLAYER, _NATIVE_PLAYER_TRIED
    if LUCY_AUDIO_PLAYBACK != "directsound":
        return None
    if _NATIVE_PLAYER is not None or _NATIVE_PLAYER_TRIED:
        return _NATIVE_PLAYER
    _NATIVE_PLAYER_TRIED = True
    try:
        _audio_dir = str(_paths.ROOT / "lucy_audio")
        if _audio_dir not in sys.path:
            sys.path.insert(0, _audio_dir)
        from lucy_player import DirectSoundPlayer, directsound_available
        if not directsound_available():
            logger.warning("DirectSound playback unavailable (needs Windows + 'pip install sounddevice'); "
                           "falling back to browser playback")
            return None
        _NATIVE_PLAYER = DirectSoundPlayer(LUCY_AUDIO_SAMPLE_RATE, 1, prebuffer_ms=LUCY_AUDIO_PREBUFFER_MS)
        _NATIVE_PLAYER.on_event = lambda ev: logger.info(f"DirectSound: {ev}")
    except Exception as e:
        logger.warning(f"DirectSound player init failed ({e}); falling back to browser playback")
    return _NATIVE_PLAYER


async def _stream_tts_to_sse(text: str, voice: str = "frieren") -> Iterator[str]:
    """Connect to Lucy Audio's SSE streaming endpoint and play / yield the PCM deltas.

    DirectSound mode: every delta goes straight into the persistent DirectSound player and this
    generator yields nothing. Browser mode: each delta is written to a .pcm file and yielded as
    MEDIA:/path so the web UI plays it. Both modes log every delta to
    runtime/logs/deltastream_chunks.log (arrival gap, lead over real time, starvation, TTFB, RTF).
    """
    import uuid as _uuid
    import httpx
    from pathlib import Path

    player = _get_native_player()
    try:
        from lucy_player import ChunkLog
    except ImportError:
        _audio_dir = str(_paths.ROOT / "lucy_audio")
        if _audio_dir not in sys.path:
            sys.path.insert(0, _audio_dir)
        from lucy_player import ChunkLog
    clog = ChunkLog(text, LUCY_AUDIO_SAMPLE_RATE, source="server", path=_CHUNK_LOG_PATH, player=player,
                    playback="directsound" if player else "browser", voice=voice)
    if player is not None:
        player.on_event = clog.player_event      # underruns etc. land in the same log
        # Opening the device takes tens of ms: do it off the event loop.
        await asyncio.get_running_loop().run_in_executor(None, player.open)
    else:
        chunk_dir = _paths.HERMES_HOME / "cache" / "audio" / "stream"
        chunk_dir.mkdir(parents=True, exist_ok=True)

    request_body = {
        "model": LUCY_AUDIO_MODEL,
        "input": text,
        "response_format": "pcm",
        "stream_format": "sse",
        "sample_rate": LUCY_AUDIO_SAMPLE_RATE,
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST", LUCY_AUDIO_TTS_URL,
                json=request_body,
                headers={"Accept": "text/event-stream"},
            ) as resp:
                if resp.status_code != 200:
                    err = await resp.aread()
                    logger.error(f"Lucy Audio stream error: HTTP {resp.status_code}")
                    clog.error(f"HTTP {resp.status_code}: {err[:200]!r}")
                    return

                buffer = ""
                async for chunk in resp.aiter_text():
                    buffer += chunk.replace("\r\n", "\n")
                    # SSE: split on blank lines (event boundaries)
                    while "\n\n" in buffer:
                        event_data, buffer = buffer.split("\n\n", 1)
                        for line in event_data.split("\n"):
                            if line.startswith("data: "):
                                payload = line[6:]
                                if payload == "[DONE]":
                                    clog.done()
                                    return
                                try:
                                    ev = json.loads(payload)
                                except json.JSONDecodeError:
                                    continue
                                if ev.get("type") == "speech.audio.delta":
                                    audio_b64 = ev.get("audio", "")
                                    if audio_b64:
                                        pcm_data = base64.b64decode(audio_b64)
                                        clog.chunk(len(pcm_data))
                                        if player is not None:
                                            player.feed(pcm_data)      # non-blocking
                                        else:
                                            chunk_path = chunk_dir / f"pcm_{_uuid.uuid4().hex[:8]}.pcm"
                                            chunk_path.write_bytes(pcm_data)
                                            yield f"MEDIA:{chunk_path.as_posix()}"
                                elif ev.get("type") == "speech.audio.done":
                                    logger.debug("Lucy Audio stream done")
                                    clog.done(ev.get("timing"))
                                    return
                                elif ev.get("type") == "error":
                                    err_msg = ev.get("error", {})
                                    logger.error(f"Lucy Audio stream error: {err_msg}")
                                    clog.error(str(err_msg))
                                    return
    except httpx.ConnectError:
        logger.warning("Lucy Audio server not reachable on 8091 — is voice mode on?")
        clog.error("connect error: Lucy Audio server not reachable on 8091")
    except Exception as e:
        logger.error(f"Lucy Audio stream exception: {e}")
        clog.error(repr(e))
    finally:
        clog.done()                    # no-op if a summary/error was already written
        if player is not None:
            player.end_of_stream()     # play out whatever is queued, even if short or truncated


# Which chat session the running reply belongs to (so search_conversations can skip it).
_current_session: ContextVar[str | None] = ContextVar("lucy_current_session", default=None)
# Sessions where the agent saved a memory during the current turn (suppresses the click-to-save prompt).
_agent_saved_turn: set = set()

_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9\-]{10,}"
    r"|AIza[0-9A-Za-z_\-]{30,}|(?:password|passwd|pwd|secret|api[_ -]?key|token)\s*(?:is|=|:)\s*\S{4,})",
    re.IGNORECASE)


def _run_memory_tool(tool_name: str, args: dict) -> str:
    """remember / recall_memory / forget / search_conversations (blocking; run in a worker thread)."""
    from lucy.memory import history as _history
    if tool_name == "remember":
        content = str(args.get("content", "")).strip()
        if not content:
            return json.dumps({"status": "error", "error": "content is required"})
        if _SECRET_RE.search(content):
            return json.dumps({"status": "refused",
                               "error": "That looks like a password/token/API key. Secrets are never stored in memory."})
        rid = args.get("replaces_id")
        result = _memory_manager.save_memory(
            content, category=str(args.get("category") or "fact"), importance=args.get("importance", 3),
            source="agent", replaces_id=rid if rid not in ("", None, 0) else None)
        if result.get("status") in ("saved", "updated"):
            sid = _current_session.get()
            if sid:
                _agent_saved_turn.add(sid)
            logger.info(f"Memory {result['status']} (#{result['id']}): {result['text']}")
        return json.dumps(result, ensure_ascii=False)

    if tool_name == "recall_memory":
        query = str(args.get("query", "")).strip()
        try:
            limit = max(1, min(int(args.get("limit", 8)), 30))
        except (TypeError, ValueError):
            limit = 8
        docs = _memory_manager.recall(query, limit) if query else _memory_manager.list_all()[-limit:]
        if not docs:
            return "No matching memories." if query else "Your memory is empty."
        return "\n".join(f"#{d['id']} [{d['category']}, importance {d['importance']}] {d['text']}" for d in docs)

    if tool_name == "forget":
        ok = _memory_manager.forget(args.get("memory_id"))
        return json.dumps({"status": "forgotten" if ok else "not_found", "id": args.get("memory_id")})

    if tool_name == "search_conversations":
        query = str(args.get("query", "")).strip()
        sid = str(args.get("session_id") or "").strip() or None
        if not query and not sid:
            return "Give a query, or a session_id to read that conversation."
        if not query:
            msgs = _history.read_session(_conn, _db_lock, sid)
            return _history.format_results(msgs, f"Latest messages of session {sid}:") or "No messages found for that session."
        res = _history.search_history(
            _conn, _db_lock, query, session_id=sid,
            exclude_session=None if sid else _current_session.get(),
            days=args.get("days"), limit=args.get("limit", 5))
        return (_history.format_results(res, f"Found {len(res)} match(es) in past conversations:")
                or "Nothing found in past conversations for that query.")
    return f"Unknown memory tool: {tool_name}"


# Memory + reminder tools keep their state in this module (memory manager, sessions DB, current session) or in the
# proactive package, so they are registered as thin adapters instead of moving the code. They still get a tier and a
# label like every other tool, and go through the same registry dispatch (one place for permissions later).
def _register_adapter_tools() -> None:
    def memory_handler(name):
        return lambda args, ctx: _run_memory_tool(name, args)          # sync -> registry runs it in a worker thread

    def proactive_handler(name):
        async def handler(args, ctx):
            return await _proactive.run_tool(name, args, ctx.session_id)
        return handler

    memory = (("remember", "write", "remember"), ("recall_memory", "read", "recall"),
              ("forget", "write", "forget"), ("search_conversations", "read", "history"))
    proactive = {"set_reminder": ("write", "remember"), "schedule_task": ("write", "remember"),
                 "manage_reminders": ("write", "recall"), "proactive_control": ("write", "remember")}   # UI labels that already exist
    items = [(n, memory_handler(n), t, l) for n, t, l in memory]
    for n in _proactive.TOOL_NAMES:
        t, l = proactive.get(n, ("write", "remember"))
        items.append((n, proactive_handler(n), t, l))
    for name, handler, tier, label in items:
        _tool_registry.default.unregister(name)
        _tool_registry.default.register(_tool_registry.ToolSpec(name, TOOL_REGISTRY[name], handler, tier, label, "lucy.server.api"))


_register_adapter_tools()


async def execute_tool_call(tool_name: str, args: dict) -> str:
    """Execute a single tool call and return its string result."""
    logger.info(f"Executing tool: {tool_name} with args: {args}")

    _reg_spec = _tool_registry.default.get(tool_name)
    if _reg_spec is not None:
        return await _reg_spec.run(args, _tool_registry.ToolContext(session_id=_current_session.get(), tool_names=_tool_names()))

    return f"Unknown tool: {tool_name}"


# --- Request Models ---
class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"
    stream: bool = True


class ChatResponse(BaseModel):
    message: str
    session_id: str
    metrics: Optional[Dict] = None


@app.get("/api/health")
async def health_check():
    """Health check endpoint for both the API and the llama-server."""
    llama_online = False
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{llm.api_url}/models")
            if resp.status_code == 200:
                llama_online = True
    except Exception:
        llama_online = False

    api_status = "online" if llama_online else "degraded"
    return {"status": api_status, "llama_server": "online" if llama_online else "offline"}


def _prompt_order() -> str:
    order = os.environ.get("LUCY_PROMPT_ORDER", "static_first").strip().lower()
    return order if order in ("static_first", "late", "legacy") else "static_first"


def _build_messages(session_id: str, user_message: str, image_paths: List[str] = None) -> List[Dict[str, Any]]:
    """Build the full message list for the llama-server request."""
    persona = {"role": "system", "content": _persona_prompt}
    facts = digest = skills = None        # facts + skills change with every message: see the assembly below

    # Inject long-term memory
    try:
        memory_context = _memory_manager.get_contextual_memory(user_message)
        if memory_context:
            facts = {"role": "system", "content": f"[USER FACTS]: {memory_context}"}
    except Exception as e:
        logger.warning(f"Failed to retrieve memory context: {e}")

    memory_rules = {"role": "system", "content": (
        "MEMORY: You have persistent memory that survives across conversations; [USER FACTS] (if shown) "
        "is what you already remember. On your own initiative - never ask permission and keep any "
        "mention of it to a few words - call `remember` when the user shares a lasting preference, a "
        "fact about themselves or their setup, a project detail, a decision, a correction, or a standing "
        "instruction ('from now on', 'always', 'never'). One short self-contained sentence per memory. "
        "If something you remember changed, pass replaces_id (find it with `recall_memory`) instead of "
        "adding a conflicting memory. Never store passwords, tokens or API keys, or temporary one-off "
        "details. When the user refers to an earlier conversation ('last time', 'we talked about', "
        "'what did I say about') or you lack background you need, call `search_conversations` (past "
        "chats) or `recall_memory` BEFORE answering instead of guessing. Only say you saved or "
        "remembered something if the tool call succeeded."
    )}

    try:                                   # accurate map of Lucy Core: paths, files, tools, stickers, how to edit
        digest = {"role": "system", "content": build_harness_digest(TOOLS, _paths.ROOT, SAFE_ROOT)}
    except Exception as e:
        logger.warning(f"Failed to build harness digest: {e}")

    # Inject relevant skills
    try:
        relevant_skills = _skills_manager.get_relevant_skills(user_message)
        if relevant_skills:
            skill_instructions = "\n".join([f"[{s['name']}]: {s['instructions']}" for s in relevant_skills])
            skills = {"role": "system", "content": f"[ACTIVE SKILLS]: {skill_instructions}"}
    except Exception as e:
        logger.warning(f"Failed to retrieve skills: {e}")

    # --- Tool usage guidance (Fix 2 reinforcement) ---
    # Reinforce that edit/update/patch all map to edit_local_file,
    # and edit should always be called (not just narrated) when the user
    # asks to modify an existing file.
    tool_rules = {"role": "system", "content": (
        "TOOL USAGE RULES: (1) write_local_file — for creating NEW files only. "
        "(2) edit_local_file — for modifying EXISTING files. "
        "When the user says 'edit', 'update', 'patch', 'modify', or 'change' a file, "
        "ALWAYS call edit_local_file. Do not narrate edits without calling the tool. "
        "edit_local_file does partial string replacement — use it to change specific "
        "sections without rewriting the whole file. "
        "(3) read_local_file — for viewing file contents. "
        "When you need to know what's in a file, call read_local_file first, "
        "then use edit_local_file with the correct old_string. "
        "(4) REMINDERS & FOLLOW-UPS: when the user asks to be reminded of something, or you say you will "
        "remind them or check back later, CALL set_reminder (plain reminder) or schedule_task (you run a "
        "read-only task later and report) - never just promise, and only say it is set after the tool "
        "confirms. Messages you send on your own initiative (reminders, greetings, check-ins) appear in the "
        "chat as your own earlier messages. If the user wants fewer of them, call proactive_control. "
        "(5) FINISH WHAT YOU START: work on the user's request until it is completely done. Never end a reply by "
        "saying what you are about to do ('I'll send the file', 'Now I will edit...') - do it, by calling the tool "
        "in that same turn. If a step needs a file sent, send it with send_file; if it needs an edit, call "
        "edit_local_file. If a tool fails or something blocks you, do not stop and do not give up: read the error, "
        "and try another way (a corrected path, a smaller edit, write_local_file, run_shell_command, a different "
        "search). Say you are blocked only after trying at least two different approaches, and then still finish "
        "everything else. Never repeat a sentence you already wrote. Only give your final answer once the whole "
        "task is done."
    )}

    # Prompt order = KV-cache reuse. llama-server only re-reads the part of the prompt that changed since the last
    # request, counted from the START. So the parts that never change come first and what changes every message
    # (recalled facts, matching skills) comes after them. LUCY_PROMPT_ORDER: static_first (default) | late (facts and
    # skills ride on the last user message, so even the history stays cached) | legacy (the old order).
    order = _prompt_order()
    if order == "legacy":
        messages = [m for m in (persona, facts, memory_rules, digest, skills, tool_rules) if m]
    else:
        messages = [m for m in (persona, memory_rules, digest, tool_rules) if m]
        if order != "late":
            messages += [m for m in (facts, skills) if m]
    late_context = [m["content"] for m in (facts, skills) if m] if order == "late" else []

    # Session history (limit to last N messages to avoid context overflow)
    # Lucy can now speak first, so stored history may hold two assistant messages in a row: repair for the template
    _hist = _get_messages(session_id)
    messages.extend(_proactive.normalize_history(_hist[-MAX_CONTEXT_MESSAGES:], truncated=len(_hist) > MAX_CONTEXT_MESSAGES))
    # The clock goes on the LAST message only, and only when the message is about time (keeps the KV cache valid, avoids noise)
    _note = _proactive.now_note() if _proactive.wants_clock(user_message) else ""
    if _note:
        user_message = f"{user_message}\n\n[Harness note - internal, do not mention or quote it] {_note}"

    if late_context:
        user_message = (f"{user_message}\n\n[Harness note - internal, do not mention or quote it] Context for this "
                        "message:\n" + "\n".join(late_context))

    # New user message — embed images as multimodal content if present
    image_paths = image_paths or []
    if image_paths:
        content = [{"type": "text", "text": user_message}]
        for img_path in image_paths:
            img = Path(img_path)
            if img.exists():
                mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                           ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}
                mime = mime_map.get(img.suffix.lower(), "image/png")
                img_b64 = base64.b64encode(img.read_bytes()).decode("utf-8")
                data_url = f"data:{mime};base64,{img_b64}"
                content.append({"type": "image_url", "image_url": {"url": data_url}})
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": user_message})
    return messages


async def _generate_once(messages: List[Dict[str, Any]], stream: bool) -> Any:
    """Send one request to llama-server. Returns either a streaming response or full JSON.
    
    Retries once on 503 (llama-server busy starting up or handling another request).
    """
    payload = {
        "model": llm.model_name,
        "messages": messages,
        "temperature": llm.temperature,
        "max_tokens": 2048,
        "tools": TOOLS,
        "tool_choice": "auto",
        **llm.request_extras(),
    }
    if stream:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    else:
        payload["stream"] = False

    max_retries = 1
    for attempt in range(max_retries + 1):
        async with httpx.AsyncClient(timeout=60.0) as client:
            if stream:
                resp = await client.stream("POST", f"{llm.api_url}/chat/completions", json=payload)
                # Check if the stream response started with an error
                # (client.stream returns 200, but the first SSE line may contain an error)
                return resp
            else:
                resp = await client.post(f"{llm.api_url}/chat/completions", json=payload)
                if resp.status_code == 503 and attempt < max_retries:
                    logger.warning(f"llama-server returned 503 (busy), retrying... (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(1.0)
                    continue
                return resp
    return resp


async def _collect_full_response(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Non-streaming request that returns the full JSON response."""
    resp = await _generate_once(messages, stream=False)
    if resp.status_code != 200:
        return {"error": "Failed to connect to brain", "status": resp.status_code}
    return resp.json()


# --- Activity Indicator Helpers ---
# The plain fallback label for a tool (when its args give no detail) is the LABEL its registry entry declares.

_ACTIVITY_DETAIL_MAX = 60  # truncate long detail (queries/commands) so the UI stays one line


def _truncate(s: str, limit: int = _ACTIVITY_DETAIL_MAX) -> str:
    s = s.strip()
    return s if len(s) <= limit else s[:limit - 1] + "\u2026"


_OFFICE_ACTIVITY = {"docx_create": "write", "xlsx_create": "write", "pptx_create": "write", "pdf_create": "write",
                    "docx_edit": "edit", "xlsx_edit": "edit", "pptx_edit": "edit", "office_read": "read", "office_render": "analyze"}


def _tool_to_activity(tool_name: str, args: dict) -> str:
    """Map a tool name + its args to a 'prefix:detail' activity label for the
    frontend (e.g. 'write:reset.py', 'search:latest iPhone'). The frontend
    splits on the first ':' and looks up a human label for the prefix, so any
    tool added here with a new prefix should get a matching entry added to
    the `labels` map in chat.html."""
    if tool_name == "edit_local_file" and args.get("file_path"):
        return f"edit:{args['file_path']}"
    if tool_name == "write_local_file" and args.get("file_path"):
        return f"write:{args['file_path']}"
    if tool_name == "read_local_file" and args.get("file_path"):
        return f"read:{args['file_path']}"
    if tool_name == "list_local_files" and args.get("dir_path"):
        return f"list:{args['dir_path']}"
    if tool_name == "find_file" and args.get("file_name"):
        return f"search:{args['file_name']}"
    if tool_name == "run_shell_command" and args.get("command"):
        return f"run:{_truncate(args['command'])}"
    if tool_name == "send_file" and args.get("file_path"):
        return f"send:{args['file_path']}"
    if tool_name in _OFFICE_ACTIVITY and args.get("path"):
        return f"{_OFFICE_ACTIVITY[tool_name]}:{args['path']}"
    if tool_name == "web_search" and args.get("query"):
        return f"search:{_truncate(args['query'])}"
    if tool_name == "web_extract" and args.get("urls"):
        return f"read:{_truncate(', '.join(args['urls'][:2]))}"
    if tool_name == "analyze_image" and args.get("image_path"):
        return f"analyze:{args['image_path']}"
    if tool_name == "send_tts" and args.get("text"):
        return f"speak:{_truncate(args['text'], 40)}"
    if tool_name == "send_sticker" and args.get("emotion"):
        return f"send:{args['emotion']} sticker"
    if tool_name == "remember" and args.get("content"):
        return f"remember:{_truncate(args['content'], 40)}"
    if tool_name == "recall_memory" and args.get("query"):
        return f"recall:{_truncate(args['query'])}"
    if tool_name == "search_conversations" and args.get("query"):
        return f"history:{_truncate(args['query'])}"
    if tool_name == "cua_cursor" and args.get("action"):
        if args["action"] == "move":
            return f"cursor:move({args.get('x','?')},{args.get('y','?')})"
        return f"cursor:{args['action']}"
    if tool_name == "cua_mouse" and args.get("action"):
        if args["action"] == "click":
            return f"mouse:click({args.get('x','?')},{args.get('y','?')})"
        elif args["action"] == "drag":
            return f"mouse:drag({args.get('x1','?')},{args.get('y1','?')}"
        return f"mouse:{args['action']}"
    if tool_name == "cua_keyboard" and args.get("action"):
        if args["action"] == "hotkey":
            return f"keys:{','.join(args.get('keys', []))}"
        elif args["action"] == "type":
            return f"type:{_truncate(args.get('text',''), 40)}"
        return f"keys:{args['action']}"

    if tool_name == "set_reminder" and args.get("message"):
        return f"remember:reminder: {_truncate(args['message'], 32)}"
    if tool_name == "schedule_task" and args.get("task"):
        return f"remember:follow-up: {_truncate(args['task'], 32)}"

    # No usable detail in args — fall back to a plain category label.
    _reg_spec = _tool_registry.default.get(tool_name)
    if _reg_spec is not None:
        return _reg_spec.label
    return tool_name  # last resort: raw tool name


# --- Stop / queued-message support ---
# The UI can abort a reply mid-stream and immediately send the next message.
# This marks "the previous reply for this session is still running / cleaning
# up" so the next request doesn't build its history before the interrupted
# reply has been saved.
_session_inflight: Dict[str, asyncio.Event] = {}

import re

def _tool_names() -> tuple:
    return tuple(t["function"]["name"] for t in TOOLS)


def _clean_text_for_tts(text: str) -> str:
    """Final cleanup of text before it is spoken: tool-call chatter ("Action: ..."), MEDIA: markers,
    file paths, URLs, code blocks, markdown and emoji are removed. See lucy.server.tts_filter."""
    return clean_for_speech(text, _tool_names())



async def _wait_for_previous_reply(session_id: str, timeout: float = 5.0):
    prev = _session_inflight.get(session_id)
    if prev is not None and not prev.is_set():
        try:
            await asyncio.wait_for(prev.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"Previous reply for session {session_id} did not finish within {timeout}s; continuing.")


class _Brain:
    """Adapter that lets the agent loop see the live llama-server (llm_manager)."""
    @property
    def base_url(self) -> str:
        return llm.api_url

    @property
    def model(self) -> str:
        return llm.model_name

    @property
    def label(self) -> str:
        return llm.active["label"]

    def extras(self) -> dict:
        return llm.request_extras()

    def loading(self) -> bool:
        return llm.phase in ("loading", "stopping")

    def error(self):
        return llm.message if llm.phase == "error" else None

    async def ready(self) -> bool:
        return await llm._healthy(llm.active["port"])

    async def ensure(self) -> None:
        """The model isn't running (crashed / never started): start it again."""
        if not self.loading():
            llm.phase, llm.message = "loading", f"Loading {llm.active['label']}..."
            asyncio.get_running_loop().create_task(llm.ensure_active())


_BRAIN = _Brain()
_AGENT_TRACE_PATH = _paths.RUNTIME / "logs" / "agent_trace.jsonl"
_trace_lock = threading.Lock()


def _agent_trace(entry: dict) -> None:
    """One JSON line per tool call / retry / turn summary: runtime/logs/agent_trace.jsonl."""
    try:
        _AGENT_TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _trace_lock:
            if _AGENT_TRACE_PATH.exists() and _AGENT_TRACE_PATH.stat().st_size > 5_000_000:
                _AGENT_TRACE_PATH.replace(_AGENT_TRACE_PATH.with_suffix(".jsonl.1"))
            with open(_AGENT_TRACE_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


# The tool list is part of the prompt (the chat template puts it at the very start), so a list that flips every turn throws
# the whole KV cache away. Keep it stable: fixed order, and tools picked in the last 3 turns of a chat stay in the list.
_RECENT_TOOLS: Dict[str, list] = {}
_STICKY_TURNS = 3


def _sticky_tool_names(session_id: str | None, names: list) -> set:
    hist = _RECENT_TOOLS.setdefault(session_id or "_", [])
    hist.append(list(names))
    del hist[:-_STICKY_TURNS]
    while len(_RECENT_TOOLS) > 200:
        _RECENT_TOOLS.pop(next(iter(_RECENT_TOOLS)))
    return set().union(*hist)


def _note_added_tools(session_id: str | None, names: list) -> None:
    """Tools the model pulled in mid-turn (tool_search / by name) stay available for the next turns."""
    hist = _RECENT_TOOLS.setdefault(session_id or "_", [[]])
    hist[-1] = sorted(set(hist[-1]) | set(names))


def _mcp_for_turn():
    """(schemas of the enabled MCP tools, search keywords, core names, more-hint fn, hint text). They only reach the
    catalog (tool_search / auto-add); a tool enters the prompt when the model asked for it. Never breaks a chat."""
    try:
        m = _mcp.get()
        tools = m.catalog_schemas()
        return tools, m.keywords(), m.core_names(), m.more_hint, m.hint()
    except Exception as e:                                   # noqa: BLE001
        logger.warning(f"MCP tools unavailable this turn: {e}")
        return [], {}, set(), None, ""


def _tools_for_turn(messages: List[Dict[str, Any]], skill_list: list[dict] | None = None, session_id: str | None = None):
    """(tools shown to the model this turn, catalog of every tool for tool_search / auto-add).

    Small models get worse as the tool list grows, so only a few schemas are shown (see lucy.tools.routing); the rest are one
    tool_search away. LUCY_TOOL_ROUTING=0 turns this off (all tools every turn, like before)."""
    if os.environ.get("LUCY_TOOL_ROUTING", "1").strip().lower() in ("0", "false", "off", "no"):
        return (_resolve_skills_to_tools(skill_list) if skill_list else TOOLS), None
    last_user = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"), None)
    text = _routing._text_of(messages[last_user].get("content")) if last_user is not None else ""
    history = messages[:last_user] if last_user is not None else []
    names = _routing.select_tools(text, history, available=TOOL_REGISTRY)
    if skill_list:                                           # skills can still pull their tools in
        names += [t["function"]["name"] for t in _resolve_skills_to_tools(skill_list) if t["function"]["name"] not in names]
    keep = _sticky_tool_names(session_id, names)
    mcp_tools, mcp_kw, mcp_core, mcp_more, mcp_hint = _mcp_for_turn()
    shown = ([t for t in TOOLS if t["function"]["name"] in keep]                 # TOOLS order = stable prompt
             + [t for t in mcp_tools if t["function"]["name"] in keep]           # MCP tools the model pulled in recently
             + [_routing.tool_search_schema(mcp_hint)])
    return shown, _routing.ToolCatalog(TOOLS + mcp_tools, keywords=mcp_kw, core=mcp_core, more=mcp_more)


async def _stream_response(messages: List[Dict[str, Any]], temperature: float | None = None, skill_list: list[dict] = None, voice_mode: bool = False, session_id: str | None = None):
    """Stream a response from llama-server, handling tool calls in a loop.

    The loop itself lives in lucy.server.agent_loop (retries, tool validation, timeouts, loop and
    empty-reply protection, context budget...). This wrapper turns its events into the SSE stream
    and does the incremental voice (TTS) work.

    Emits SSE lines: {content}, {activity}, {media}, {error}, and finally [METRICS]{json}.
    """
    import time as _time_mod
    _start_time = _time_mod.time()
    _full_response_text = ""
    _tts_voice = "frieren"
    _tts_buffer = ""  # accumulates sentence fragments during streaming
    _stats: dict = {}

    _current_session.set(session_id)          # lets search_conversations skip the live conversation
    _refresh_sticker_tool()                   # new sticker files are picked up without a restart
    if temperature is None:
        temperature = llm.temperature          # Settings > Model value (preset per model)
    _halt = _safety.get().halt_reason()       # constitution files changed without the user sealing them
    if _halt:
        yield f"data: {json.dumps({'error': _halt})}\n\n"
        return
    available_tools, _catalog = _tools_for_turn(messages, skill_list, session_id)
    if _catalog is not None and _routing.coding_mode(t["function"]["name"] for t in available_tools):
        messages = list(messages)             # coding turn: add the short work-method note after the stable system blocks
        _at = next((i for i, m in enumerate(messages) if m.get("role") != "system"), len(messages))
        messages.insert(_at, {"role": "system", "content": _routing.CODING_ADDENDUM})
    if _catalog is not None and _routing.office_mode(t["function"]["name"] for t in available_tools):
        messages = list(messages)             # Word / Excel / PowerPoint / PDF turn: add the office guidance (.core/skills/definitions/office/SKILL.md)
        _at = next((i for i, m in enumerate(messages) if m.get("role") != "system"), len(messages))
        messages.insert(_at, {"role": "system", "content": _routing.office_guidance()})
    _tts_filter = TTSStreamFilter(_tool_names())    # strips tool calls, paths, MEDIA: ... (all tools, not just the ones shown)
    agent = AgentLoop(
        brain=_BRAIN, tools=available_tools,
        execute=lambda name, args: execute_tool_call(name, args),   # looked up at call time
        activity_for=_tool_to_activity, count_tokens=_count_tokens, temperature=temperature,
        cfg=LoopConfig(max_rounds=MAX_TOOL_ROUNDS, max_tokens=LUCY_MAX_TOKENS, tool_timeout=LUCY_TOOL_TIMEOUT,
                       ctx_tokens=int(llm.cfg.get("context_length", 100000)), http_timeout=LUCY_TIMEOUT),
        trace=_agent_trace, session_id=session_id, tool_catalog=_catalog,
        on_tools_added=lambda names: _note_added_tools(session_id, names),
        gate=_safety.get().gate_for(session_id))    # may pause the turn to show an approval card

    async def _speak(text: str):
        """Yield SSE lines that speak `text` through Lucy Audio (voice mode)."""
        clean = _clean_text_for_tts(text)
        if clean:
            yield f"data: {json.dumps({'activity': 'speak'})}\n\n"
            async for media_event in _stream_tts_to_sse(clean, _tts_voice):
                if media_event.startswith("MEDIA:"):
                    media_path = media_event.split(" ", 1)[0].replace("MEDIA:", "")
                    yield f"data: {json.dumps({'media': media_path})}\n\n"
        yield f"data: {json.dumps({'activity': None})}\n\n"

    async for ev in agent.run(messages):
        if ev.kind == "content":
            _full_response_text += ev.data
            if voice_mode:
                _tts_buffer += _tts_filter.feed(ev.data)      # the chat shows everything; the voice only speech
            yield f"data: {json.dumps({'content': ev.data})}\n\n"
            # Incremental TTS: speak each finished sentence while the model is still generating.
            if voice_mode and re.search(r'[。．！？！？.!?…\n]\s*$', _tts_buffer):
                chunk, _tts_buffer = _tts_buffer.strip(), ""
                if chunk:
                    async for line in _speak(chunk):
                        yield line
        elif ev.kind == "activity":
            if voice_mode:                    # a round just ended (tools are about to run): speak what's pending
                _tts_buffer += _tts_filter.flush()
                if _tts_buffer.strip():
                    chunk, _tts_buffer = _tts_buffer.strip(), ""
                    async for line in _speak(chunk):
                        yield line
            yield f"data: {json.dumps({'activity': ev.data})}\n\n"
        elif ev.kind == "media":
            yield f"data: {json.dumps({'media': ev.data})}\n\n"
        elif ev.kind in ("approval_request", "approval_resolved"):
            yield f"data: {json.dumps({ev.kind: ev.data})}\n\n"
        elif ev.kind == "error":
            yield f"data: {json.dumps(ev.data)}\n\n"
            return
        elif ev.kind == "metrics":
            _stats = ev.data

    # --- Final TTS for any remaining buffered text (voice_mode) ---
    # Catches the last sentence fragment that didn't end with punctuation.
    if voice_mode:
        _tts_buffer += _tts_filter.flush()
    if voice_mode and _tts_buffer.strip():
        async for line in _speak(_tts_buffer.strip()):
            yield line

    # Emit final metrics line per SSE protocol: [METRICS]{json}
    _elapsed = max(_time_mod.time() - _start_time, 0.001)
    _token_count = _count_tokens(_full_response_text)
    _metrics_json = json.dumps({
        'tokens_generated': _token_count,
        'time_ms': int(_elapsed * 1000),
        'tokens_per_second': round(_token_count / _elapsed, 1),
        'tool_calls': _stats.get('tool_calls', 0),
        'rounds': _stats.get('rounds', 0),
        'tool_errors': _stats.get('tool_errors', 0),
        'retries': _stats.get('retries', 0),
    })
    yield f"data: [METRICS]{_metrics_json}\n\n"


@app.post("/api/chat")
async def chat_endpoint(
    request: Request,
    message: Optional[str] = Form(None),
    session_id: Optional[str] = Form("default"),
    stream: Optional[bool] = Form(True),
    voice_mode: Optional[str] = Form(None),
    files: Optional[List[UploadFile]] = File(None),
    _: bool = Depends(verify_api_key),
):
    """Main chat endpoint — streams responses with tool-call support.

    Accepts multipart/form-data for file uploads, or JSON for plain text.
    """
    # Try JSON first (backward compat with plain text requests)
    file_paths: list[str] = []
    if not files and not message:
        try:
            body = await request.json()
            message = body.get("message", "")
            session_id = body.get("session_id", "default")
            stream = body.get("stream", True)
            voice_mode = body.get("voice_mode", voice_mode)
            # Accept file paths as strings (for Discord gateway relay)
            file_paths = body.get("file_paths", []) or body.get("files", []) or []
            if isinstance(file_paths, str):
                file_paths = [file_paths]
        except Exception:
            pass

    # If still no message, try reading from form data (already parsed by FastAPI)
    if not message:
        try:
            form = await request.form()
            message = form.get("message", "")
            session_id = form.get("session_id") or session_id or "default"
            stream_str = form.get("stream")
            if stream_str is not None:
                stream = stream_str == "true" or stream_str is True
            # Re-extract files from form if not already parsed
            if files is None:
                files = form.getlist("files") if "files" in form else None
            # Also accept file_paths as a form field (JSON string or comma-separated)
            fp_field = form.get("file_paths")
            if fp_field:
                try:
                    file_paths = json.loads(fp_field) if isinstance(fp_field, str) else fp_field
                    if isinstance(file_paths, str):
                        file_paths = [file_paths]
                except Exception:
                    file_paths = [fp_field]
        except Exception:
            pass

    if message:
        # Browsers encode line breaks in form data as CRLF (Shift+Enter in the chat box): store plain \n
        message = message.replace("\r\n", "\n").replace("\r", "\n")

    # Allow empty message when files are attached (the model can reason about the image)
    if not message and not files and not file_paths:
        raise HTTPException(status_code=400, detail="No message provided")

    # Handle uploaded files — save them to a temp upload dir
    uploaded_file_paths = []
    image_paths = []
    upload_records: list[tuple[bytes, str]] = []     # (bytes, name) of what the user attached -> Library
    relay_paths: list[Path] = []                      # files passed by path (Discord relay) -> Library
    if files:
        upload_dir = _paths.RUNTIME / "tmp"
        upload_dir.mkdir(parents=True, exist_ok=True)
        for f in files:
            # Robust path traversal protection: extract basename only,
            # reject any path separators or parent-dir references
            safe_name = os.path.basename(f.filename.replace("\\", "/").strip())
            if not safe_name or safe_name in (".", ".."):
                safe_name = "uploaded_file"
            fpath = upload_dir / safe_name
            content = await f.read()
            fpath.write_bytes(content)
            upload_records.append((content, safe_name))
            uploaded_file_paths.append(str(fpath))
            # If this is an image, pass it for multimodal embedding
            if str(fpath).lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp')):
                image_paths.append(str(fpath))
            await f.seek(0)

    # Handle file paths passed as strings (e.g. from Discord gateway relay)
    if file_paths:
        for fp in file_paths:
            if not isinstance(fp, str):
                continue
            target = _resolve_path(fp)
            if target is None or not target.exists():
                logger.warning(f"File path from relay not found or unsafe: {fp}")
                continue
            uploaded_file_paths.append(str(target))
            relay_paths.append(target)
            if str(target).lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp')):
                image_paths.append(str(target))

    # If the user just stopped a reply in this session, let it finish saving first
    await _wait_for_previous_reply(session_id)

    # Build messages (includes system persona, memory, skills, history, and new user message)
    # Image paths are embedded directly in the user message as multimodal content
    messages = _build_messages(session_id, message, image_paths=image_paths if image_paths else None)
    # If non-image files were uploaded, add a system note about them
    non_image_files = [p for p in uploaded_file_paths if p not in image_paths]
    if non_image_files:
        file_info = f"User uploaded {len(non_image_files)} file(s): " + ", ".join(non_image_files)
        messages.append({"role": "system", "content": file_info})
    elif image_paths:
        # Let the model know images were received (redundant but harmless)
        pass
    # Store user message in session, with its files/images cached so they survive a page refresh.
    # (Stickers - "[sticker: N.png]" - are skipped: they already live in assets/stickers.)
    user_atts: list[dict] = []
    if not _STICKER_TEXT_RE.match(message or ""):
        for _data, _nm in upload_records:
            _a = await asyncio.to_thread(_library.ingest_bytes, _data, _nm, "user")
            if _a:
                user_atts.append(_a)
        for _rp in relay_paths:
            _a = await asyncio.to_thread(_library.ingest_path, _rp, "user")
            if _a:
                user_atts.append(_a)
    _append_message(session_id, "user", message or "", user_atts)
    _proactive.touch(session_id)
    # Check for memory-worthy fact in the user message
    memory_fact = detect_memory_candidate(message)
    _agent_saved_turn.discard(session_id)
    # Generate session title from first message if it's still "New Session"
    asyncio.create_task(_generate_session_title(session_id))
    reply_done = asyncio.Event()
    _session_inflight[session_id] = reply_done

    async def event_stream():
        full_response = ""
        media_atts: list[dict] = []          # files Lucy delivered this turn (kept with her reply)
        # Parse voice_mode from form field (string "true" or "false")
        voice_mode_bool = voice_mode and voice_mode.lower() == "true"
        try:
            async for event_data in _stream_response(messages, voice_mode=voice_mode_bool, session_id=session_id):
                if event_data.startswith("data: "):
                    chunk_data = event_data[6:]
                    try:
                        parsed = json.loads(chunk_data)
                        if "content" in parsed:
                            full_response += parsed["content"]
                        elif parsed.get("media"):
                            att = await asyncio.to_thread(_ingest_generated_media, parsed["media"], bool(voice_mode_bool))
                            if att and not any((att.get("sha") and att.get("sha") == m.get("sha")) or
                                               (att.get("path") and att.get("path") == m.get("path")) for m in media_atts):
                                media_atts.append(att)
                    except json.JSONDecodeError:
                        pass
                yield event_data

            # Store assistant response in session history (including any media markers)
            if full_response.strip() or media_atts:
                _append_message(session_id, "assistant", full_response, media_atts)
                # Append memory confirmation UI to the streamed response if a fact was detected
                if memory_fact and session_id not in _agent_saved_turn:
                    confirmation = render_confirmation(memory_fact, session_id)
                    yield f"data: {json.dumps({'content': confirmation})}\n\n"

        except asyncio.CancelledError:
            # Client disconnected (e.g. the user pressed Stop) — must re-raise;
            # CancelledError is BaseException on 3.8+ and will NOT be caught by
            # except Exception, causing uvicorn worker crash.
            logger.info("Client disconnected (CancelledError in event_stream)")
            # Save what was generated so far, marked as interrupted. Otherwise the
            # history ends with a user message that has no reply, and the next
            # message creates two user turns in a row, which some chat templates
            # (e.g. Mistral-style) reject.
            try:
                partial = full_response.strip()
                note = "[Response interrupted by the user.]"
                _append_message(session_id, "assistant", f"{partial}\n\n{note}" if partial else note, media_atts)
            except Exception as e:
                logger.error(f"Failed to save interrupted reply: {e}")
            raise
        except Exception as e:
            logger.error(f"Streaming error: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            reply_done.set()
            _proactive.touch(session_id)
            if _session_inflight.get(session_id) is reply_done:
                _session_inflight.pop(session_id, None)

    if stream:
        return StreamingResponse(event_stream(), media_type="text/event-stream")
    # Non-streaming: collect the full response
    full_response = ""
    media_paths: list[str] = []
    async for event in event_stream():
        stripped = event.replace("data: ", "").strip()
        try:
            parsed = json.loads(stripped)
            if "content" in parsed:
                full_response += parsed["content"]
            if "media" in parsed:
                media_paths.append(parsed["media"])
        except json.JSONDecodeError:
            continue
    result: dict = {"response": full_response, "session_id": session_id}
    if media_paths:
        result["media"] = media_paths[0] if len(media_paths) == 1 else media_paths
    return result


@app.post("/api/tts")
async def tts_endpoint(
    request: Request,
    text: str = Form(None),
    voice: str = Form("frieren"),
    _: bool = Depends(verify_api_key),
):
    """Direct TTS endpoint — bypasses chat, goes straight to lucy audio server on 8091.

    Accepts JSON or form data. Returns a MEDIA: path to the generated WAV file.
    """
    if not text:
        try:
            body = await request.json()
            text = body.get("text", "")
            voice = body.get("voice", "frieren")
        except Exception:
            pass
    if not text:
        raise HTTPException(status_code=400, detail="No text provided")
    result = await execute_tool_call("send_tts", {"text": text, "voice": voice})
    if result.startswith("MEDIA:"):
        media_path = result.split(" ", 1)[0].replace("MEDIA:", "")
        return {"media": media_path, "path": media_path}
    return {"error": result}


@app.post("/api/cua")
async def cua_endpoint(
    action: str = Form(..., description="Action to perform: cursor_get, cursor_move, mouse_click, mouse_drag, key_combo, type_text, minimize_all"),
    x: Optional[int] = Form(None),
    y: Optional[int] = Form(None),
    x1: Optional[int] = Form(None),
    y1: Optional[int] = Form(None),
    x2: Optional[int] = Form(None),
    y2: Optional[int] = Form(None),
    button: str = Form("left"),
    keys: Optional[str] = Form(None),
    text: Optional[str] = Form(None),
    _: bool = Depends(verify_api_key),
):
    """Direct CUA (Computer Use) endpoint — Win32 API backend.
    
    Controls the real OS cursor, mouse, and keyboard via direct Win32 API calls
    (ctypes → user32.dll), bypassing cua-driver overlay/synthetic-event limitations.
    
    Available actions:
    - cursor_get: Get current cursor position → {'x': X, 'y': Y}
    - cursor_move: Requires x, y → Move real cursor to (x, y)
    - mouse_click: Requires x, y, optional button → Click at (x, y)
    - mouse_drag: Requires x1, y1, x2, y2 → Drag from (x1,y1) to (x2,y2)
    - key_combo: Requires keys (comma-separated e.g. 'win,d') → Press key combination
    - type_text: Requires text → Type a string
    - minimize_all: Win+D → Minimize all windows
    """
    import json as _json
    
    try:
        if action == "cursor_get":
            result = get_cursor_pos()
        elif action == "cursor_move":
            if x is None or y is None:
                raise HTTPException(status_code=400, detail="x and y required for cursor_move")
            result = set_cursor_pos(x, y)
        elif action == "mouse_click":
            if x is None or y is None:
                raise HTTPException(status_code=400, detail="x and y required for mouse_click")
            result = mouse_click(x, y, button)
        elif action == "mouse_drag":
            if x1 is None or y1 is None or x2 is None or y2 is None:
                raise HTTPException(status_code=400, detail="x1, y1, x2, y2 required for mouse_drag")
            result = mouse_drag(x1, y1, x2, y2)
        elif action == "key_combo":
            if not keys:
                raise HTTPException(status_code=400, detail="keys required for key_combo (comma-separated, e.g. 'win,d')")
            key_list = keys.split(",")
            result = key_combo(key_list)
        elif action == "type_text":
            if not text:
                raise HTTPException(status_code=400, detail="text required for type_text")
            result = type_text(text)
        elif action == "minimize_all":
            result = minimize_all()
        else:
            raise HTTPException(status_code=400, detail=f"Unknown action: {action}. Available: cursor_get, cursor_move, mouse_click, mouse_drag, key_combo, type_text, minimize_all")
        
        return {"action": action, "result": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"CUA error on action={action}: {e}")
        return {"action": action, "error": str(e)}


@app.post("/api/server/restart")
async def server_restart(
    _: bool = Depends(verify_api_key),
):
    """Trigger a server restart via uvicorn --reload mechanism.

    Touches api.py to trigger the reload watcher (requires --reload flag on startup).
    Returns immediately; the server will reload within ~2 seconds.

    NOTE: This restarts the Lucy Core API server process only.
    It does NOT restart the llama-server (port 8080) or lucy audio server (port 8091).
    OS-level operations (reboot/shutdown/lock) are not supported by this endpoint.
    """
    import os
    api_path = str(Path(__file__).resolve())
    try:
        # Touch the file to trigger uvicorn --reload watcher
        os.utime(api_path, None)
        return {
            "status": "restart_triggered",
            "method": "file_touch (uvicorn --reload)",
            "file": api_path,
            "message": "Server reload signal sent. The server will restart within ~2 seconds.",
        }
    except Exception as e:
        logger.error(f"Server restart failed: {e}")
        return {
            "status": "error",
            "error": str(e),
            "note": "If the server was not started with --reload, file touch will not trigger a restart.",
        }


def _shutdown_lucy_core_sequence():
    """Runs in a background thread after /api/server/stop has responded.

    Stops, in order:
      1. The Lucy Audio Server, only if it is running.
      2. Lucy Core itself — this process, plus the uvicorn --reload supervisor
         that spawned it, if there is one.

    It never touches other Python processes, llama-server (8080), or the
    batch file / terminal that launched Lucy Core.
    """
    import multiprocessing
    import subprocess
    import threading

    time.sleep(0.75)  # let the HTTP response flush to the browser first

    try:
        from lucy.server import voice_control
        if voice_control.stop_all():
            logger.info("Lucy Audio Server stopped.")
    except Exception as e:
        logger.error(f"Failed to stop Lucy Audio during shutdown: {e}")

    try:
        _conn.commit()
    except Exception:
        pass

    logger.info("Lucy Core shutting down.")

    # Under `uvicorn --reload`, this process is a worker spawned by a reloader
    # supervisor; if we only exit ourselves, the supervisor would just spawn a
    # replacement. multiprocessing.parent_process() is set only in that case
    # (a plain `uvicorn.run(...)` server has no multiprocessing parent), so it
    # identifies exactly the supervisor and nothing else.
    parent = multiprocessing.parent_process()
    if parent is not None and parent.pid:
        # /T also takes down its child tree (this worker). Only that tree.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(parent.pid)],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    os._exit(0)


@app.post("/api/server/stop")
async def server_stop(
    _: bool = Depends(verify_api_key),
):
    """Stops Lucy Core, and the Lucy Audio Server if it is running.

    Does NOT kill other python.exe processes and does NOT touch llama-server
    (port 8080). Responds immediately, then shuts down ~1 second later.
    """
    import threading
    try:
        from lucy.server import voice_control
        voice_running = voice_control._is_alive() or voice_control._external_lucy_audio_running()
    except Exception:
        voice_running = False

    threading.Thread(target=_shutdown_lucy_core_sequence, daemon=True).start()
    return {"status": "stopping", "lucy_audio_was_running": voice_running}


@app.get("/api/logs")
async def get_logs(
    lines: int = 100,
    follow: bool = False,
    _: bool = Depends(verify_api_key),
):
    """Returns the last N log lines from runtime/lucy_core.log.

    If follow=true, streams new log entries as Server-Sent Events (SSE).
    """
    log_path = Path(LOG_PATH)

    if not follow:
        if not log_path.exists():
            return {"logs": []}
        from collections import deque
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            # deque(maxlen=N) keeps only the last N lines instead of loading
            # the whole file — the UI polls this every few seconds.
            recent = deque(f, maxlen=lines) if lines > 0 else list(f)
        return {"logs": [line.rstrip("\n") for line in recent]}

    def event_stream():
        if not log_path.exists():
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.touch()
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    payload = json.dumps({"log": line.rstrip()})
                    yield f"data: {payload}\n\n"
                else:
                    time.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/cli")
async def cli_chat_endpoint(
    request: Request,
    message: str = Form(None),
    session_id: str = Form("cli-dev"),
    stream: bool = Form(True),
    voice_mode: Optional[str] = Form(None),
    _: bool = Depends(verify_api_key),
):
    """CLI-focused chat endpoint — coding mode (temp=0.3, technical tone).

    Accepts JSON or form data. Mirrors /api/chat but with lower temperature
    and a dedicated CLI session namespace.
    """
    # Try JSON first (backward compat with plain text requests)
    if not message:
        try:
            body = await request.json()
            message = body.get("message", "")
            session_id = body.get("session_id", session_id)
            stream = body.get("stream", stream)
            voice_mode = body.get("voice_mode", voice_mode)
        except Exception:
            pass

    # If still no message, try reading from form data (already parsed by FastAPI)
    if not message:
        try:
            form = await request.form()
            message = form.get("message", "")
            session_id = form.get("session_id") or session_id
            stream_str = form.get("stream")
            if stream_str is not None:
                stream = stream_str == "true" or stream_str is True
            voice_mode = form.get("voice_mode", voice_mode)
        except Exception:
            pass

    if not message:
        raise HTTPException(status_code=400, detail="No message provided")

    session_id = f"cli-{session_id}" if not session_id.startswith("cli-") else session_id
    voice_mode_bool = voice_mode and voice_mode.lower() == "true"

    # Build messages (CLI mode — same as chat but with coding tone)
    messages = _build_messages(session_id, message)

    # Store user message in CLI session
    _append_message(session_id, "user", message)
    _proactive.touch(session_id)
    memory_fact = detect_memory_candidate(message)
    _agent_saved_turn.discard(session_id)
    asyncio.create_task(_generate_session_title(session_id))

    async def event_stream():
        full_response = ""
        try:
            async for event_data in _stream_response(messages, temperature=0.3, voice_mode=voice_mode_bool, session_id=session_id):
                if event_data.startswith("data: "):
                    chunk_data = event_data[6:]
                    try:
                        parsed = json.loads(chunk_data)
                        if "content" in parsed:
                            full_response += parsed["content"]
                    except json.JSONDecodeError:
                        pass
                # Pass through all SSE events (content, activity, media, [METRICS])
                yield event_data

            if full_response.strip():
                _append_message(session_id, "assistant", full_response)
                if memory_fact and session_id not in _agent_saved_turn:
                    confirmation = render_confirmation(memory_fact, session_id)
                    yield f"data: {json.dumps({'content': confirmation})}\n\n"

        except asyncio.CancelledError:
            logger.info("CLI client disconnected")
            raise
        except Exception as e:
            logger.error(f"CLI streaming error: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    if stream:
        return StreamingResponse(event_stream(), media_type="text/event-stream")
    full_response = ""
    media_paths: list[str] = []
    async for event in event_stream():
        stripped = event.replace("data: ", "").strip()
        try:
            parsed = json.loads(stripped)
            if "content" in parsed:
                full_response += parsed["content"]
            if "media" in parsed:
                media_paths.append(parsed["media"])
        except json.JSONDecodeError:
            continue
    result: dict = {"response": full_response, "session_id": session_id}
    if media_paths:
        result["media"] = media_paths[0] if len(media_paths) == 1 else media_paths
    return result


@app.get("/")
async def root(request: Request):
    """Serve the chat UI with API key injected into the DOM."""
    ui_path = _paths.UI_DIR / "chat.html"
    html = Path(ui_path).read_text(encoding="utf-8")
    # Inject the API key into the DOM so browser fetch() can read it
    # The placeholder __API_KEY_PLACEHOLDER__ is replaced with the actual key
    html = html.replace("__API_KEY_PLACEHOLDER__", DEV_API_KEY)
    html = html.replace("__SAFE_ROOT_PLACEHOLDER__", SAFE_ROOT.as_posix())
    # Cache-busting: the asset URLs get the file's modification time as ?v=, so
    # an updated chat.js / chat.css is picked up on the next page load without
    # editing the version number by hand.
    html = html.replace("__CSS_VERSION__", _asset_version("chat.css"))
    html = html.replace("__JS_VERSION__", _asset_version("chat.js"))
    html = _proactive.inject_client(html, DEV_API_KEY)      # shows what Lucy says first (toast / notification)
    return HTMLResponse(content=html)


def _asset_version(filename: str) -> str:
    try:
        return str(int((_paths.UI_DIR / filename).stat().st_mtime))
    except OSError:
        return "0"


@app.get("/chat.css")
async def serve_css():
    """Serve the external stylesheet for the chat UI."""
    css_path = _paths.UI_DIR / "chat.css"
    css = Path(css_path).read_text(encoding="utf-8")
    return HTMLResponse(content=css, media_type="text/css")


@app.get("/chat.js")
async def serve_js():
    """Serve the external script for the chat UI."""
    js_path = _paths.UI_DIR / "chat.js"
    if not js_path.exists():
        raise HTTPException(status_code=404, detail="chat.js not found")
    js = Path(js_path).read_text(encoding="utf-8")
    return HTMLResponse(content=js, media_type="application/javascript")


@app.get("/assets/logo/{name}")
async def serve_logo(name: str):
    """Serve the Lucy logo for the chat UI from Lucy_Core/assets/logo/ (public, like chat.css / chat.js)."""
    logo_dir = (_paths.ROOT / "assets" / "logo").resolve()
    path = (logo_dir / name).resolve()
    try:
        path.relative_to(logo_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="Path outside the logo folder")
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".svg"} or not path.is_file():
        raise HTTPException(status_code=404, detail="Logo not found")
    return FileResponse(path)


@app.get("/api/file/{path:path}")
async def serve_file(path: str, _: bool = Depends(verify_api_key)):
    """Serve a file from the safe root for the frontend to download/view."""
    file_path = (SAFE_ROOT / path).resolve()          # an absolute path (D:/...) replaces SAFE_ROOT here
    try:
        file_path.relative_to(SAFE_ROOT)
    except ValueError:
        # Outside SAFE_ROOT: allowed if the safety layer lets Lucy read it (folders.read, not a denied path)
        if _safety.get().decide("read_local_file", "read", {"file_path": file_path.as_posix()}, None).level != _safety.AUTO:
            raise HTTPException(status_code=403, detail="Path outside allowed root")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)


@app.get("/api/skills")
async def list_skills(_ = Depends(verify_api_key)):
    """Lists all available skills for the frontend.

    Returns skills with: id, name, trigger, instructions, tools, category, source.
    """
    with _db_lock:
        rows = _conn.execute(
            "SELECT skill_id, name, trigger, instructions, tools, category, source, "
            "created_at, updated_at FROM skills ORDER BY category, name"
        ).fetchall()
    skills = []
    for row in rows:
        tools_list = []
        if row["tools"]:
            try:
                tools_list = json.loads(row["tools"])
            except (json.JSONDecodeError, TypeError):
                tools_list = []
        skills.append({
            "id": row["skill_id"],
            "name": row["name"],
            "trigger": row["trigger"],
            "instructions": row["instructions"],
            "tools": tools_list,
            "category": row["category"],
            "source": row["source"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })
    return {"skills": skills}


@app.get("/api/connectors")
async def get_connectors(_ = Depends(verify_api_key)):
    """Returns all connectors with masked tokens."""
    rows = _conn.execute(
        "SELECT connector_id, name, token, created_at, updated_at FROM connectors ORDER BY name"
    ).fetchall()
    connectors = []
    for row in rows:
        connectors.append({
            "id": row["connector_id"],
            "name": row["name"],
            "masked_token": _mask_token(row["token"]) if row["token"] else None,
            "has_token": bool(row["token"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })
    return {"connectors": connectors}


@app.post("/api/connectors")
async def create_connector(request: Request, _ = Depends(verify_api_key)):
    """Add a new connector. Body: { name, token }"""
    body = await request.json()
    name = body.get("name", "").strip()
    token = body.get("token", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Connector name is required")
    with _db_lock:
        _conn.execute(
            "INSERT OR IGNORE INTO connectors (name, token) VALUES (?, ?)",
            (name, token),
        )
        _conn.commit()
    row = _conn.execute(
        "SELECT connector_id, name FROM connectors WHERE name = ?", (name,)
    ).fetchone()
    return {"status": "ok", "id": row["connector_id"], "name": row["name"]}


@app.patch("/api/connectors/{connector_id}")
async def update_connector(connector_id: int, request: Request, _ = Depends(verify_api_key)):
    """Update a connector's token. Body: { token }"""
    body = await request.json()
    token = body.get("token", "").strip()
    with _db_lock:
        result = _conn.execute(
            "UPDATE connectors SET token = ?, updated_at = strftime('%Y-%m-%d %H:%M:%S', 'now') "
            "WHERE connector_id = ?",
            (token, connector_id),
        )
        _conn.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Connector not found")
    return {"status": "ok", "id": connector_id}


@app.delete("/api/connectors/{connector_id}")
async def delete_connector(connector_id: int, _ = Depends(verify_api_key)):
    """Delete a connector."""
    with _db_lock:
        result = _conn.execute(
            "DELETE FROM connectors WHERE connector_id = ?", (connector_id,)
        )
        _conn.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Connector not found")
    return {"status": "deleted", "id": connector_id}


@app.post("/api/skills/learn")
async def learn_skill(
    request: Request,
    _: bool = Depends(verify_api_key),
):
    """Dynamically learn a new skill from chat.

    Body: { name, trigger, instructions, tools?, category? }
    Inserts into DB, updates cache, immediately available for matching.
    """
    body = await request.json()
    name = body.get("name", "").strip()
    trigger = body.get("trigger", "").strip()
    instructions = body.get("instructions", "").strip()
    tools = body.get("tools", [])
    category = body.get("category", "general")

    if not name or not trigger or not instructions:
        raise HTTPException(
            status_code=400,
            detail="name, trigger, and instructions are required",
        )

    skill = _skills_manager.learn_skill(
        name=name,
        trigger=trigger,
        instructions=instructions,
        tools=tools,
        category=category,
        source="learned",
    )
    return {"status": "learned", "skill": {
        "name": skill["name"],
        "trigger": skill["trigger"],
        "category": skill["category"],
        "tools": skill["tools"],
    }}


@app.post("/api/skills/import")
async def import_skills(_: bool = Depends(verify_api_key)):
    """Re-import all .md skill files from definitions/ into the DB."""
    count = _skills_manager._db.import_from_files()
    _skills_manager._db.load_all_to_cache()
    _skills_manager.skills = _skills_manager._db.list_all()
    return {"status": "imported", "count": count}


@app.post("/api/health/fast")
async def fast_health_check():
    """Fast-path health check — bypasses agent stack entirely.

    Proxies directly to llama-server /v1/chat/completions with tools=[]
    for a sub-50ms response when the user just wants a simple status ping.
    No API key required (same as /api/health).
    """
    import json as _json
    payload = {
        "model": llm.model_name,
        "messages": [{"role": "user", "content": "OK"}],
        "max_tokens": 1,
        "tools": [],
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.post(
                f"{llm.api_url}/chat/completions",
                json=payload,
            )
            if resp.status_code == 200:
                data = resp.json()
                usage = data.get("usage", {})
                return {
                    "status": "healthy",
                    "llama_server": "online",
                    "model": data.get("model", llm.model_name),
                    "tokens_generated": usage.get("completion_tokens", 0),
                    "tokens_per_second": round(
                        usage.get("completion_tokens", 0) / max(usage.get("total_duration_ms", 1) / 1000, 0.001),
                        1,
                    ) if usage.get("total_duration_ms") else 0,
                }
            else:
                return {"status": "degraded", "llama_server": "error", "http_status": resp.status_code}
    except Exception as e:
        return {"status": "offline", "llama_server": "offline", "error": str(e)}


@app.get("/api/metrics/model")
async def model_metrics(_: bool = Depends(verify_api_key)):
    """Returns model specification details from llama-server."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{llm.api_url}/models")
            if resp.status_code == 200:
                data = resp.json()
                if data.get("data"):
                    m = data["data"][0]
                    meta = m.get("meta", {})
                    return {
                        "name": m.get("id", m.get("name", "unknown")),
                        "n_params": meta.get("n_params", 0),
                        "n_ctx": meta.get("n_ctx", 0),
                        "quantization": meta.get("ftype", "unknown"),
                        "size_bytes": meta.get("size", 0),
                        "format": meta.get("format", "unknown"),
                        "backend": "llama-server",
                        "base_url": llm.api_url,
                    }
    except Exception as e:
        logger.warning(f"Failed to get model metrics: {e}")
    return {"error": "Could not retrieve model metrics", "llama_server": llm.api_url}


@app.get("/api/metrics/session")
async def session_metrics(_: bool = Depends(verify_api_key)):
    """Returns session storage statistics for diagnostics."""
    sessions = _list_sessions_db(include_messages=False)
    return {
        "active_sessions": len(sessions),
        "total_messages": sum(s["message_count"] for s in sessions.values()),
        "sessions": {sid: s["message_count"] for sid, s in sessions.items()},
    }


@app.get("/api/sessions")
async def list_sessions(messages: int = 0, _: bool = Depends(verify_api_key)):
    """Returns the sessions for the sidebar (metadata only; ?messages=1 adds every message).

    Sessions without any message are not listed: a "New Session" only exists once you write in it."""
    return {"sessions": _list_sessions_db(include_messages=bool(messages), hide_empty=True)}


@app.post("/api/sessions")
async def create_session(_: bool = Depends(verify_api_key)):
    """Creates a new empty session and returns its ID."""
    import uuid
    session_id = str(uuid.uuid4())
    _ensure_session(session_id)
    # Set updated_at immediately so the session sorts to the top of the list
    with _db_lock:
        row = _conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%S', 'now', ?) AS now", (_get_tz_modifier(),)).fetchone()
    now = row["now"] if row else time.strftime('%Y-%m-%d %H:%M:%S')
    with _db_lock:
        _conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
            (now, session_id),
        )
        _conn.commit()
    return {"session_id": session_id, "title": "New Session", "message_count": 0, "updated_at": now}


# === Timezone settings via TimeAPI.io with caching ===

import time as _time_mod  # noqa: E402

_TIMEZONE_CACHE_PATH = str(_paths.RUNTIME / "tz_cache.json")
_TIMEZONE_CACHE_MAX_AGE = 24 * 3600  # 24 hours
_timezone_cache: dict | None = None
_timezone_cache_expires: float = 0.0


def _load_timezone_cache() -> dict | None:
    """Load cached timezone list from disk, if fresh enough."""
    global _timezone_cache, _timezone_cache_expires
    if _timezone_cache is not None and _time_mod.time() < _timezone_cache_expires:
        return _timezone_cache
    cache_path = Path(_TIMEZONE_CACHE_PATH)
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            if cached.get("_expires", 0) > _time_mod.time():
                _timezone_cache = cached
                _timezone_cache_expires = cached["_expires"]
                return _timezone_cache
        except Exception:
            pass
    return None


def _save_timezone_cache(data: dict):
    """Persist timezone list to disk for future requests."""
    global _timezone_cache, _timezone_cache_expires
    data["_expires"] = _time_mod.time() + _TIMEZONE_CACHE_MAX_AGE
    _timezone_cache = data
    _timezone_cache_expires = data["_expires"]
    try:
        Path(_TIMEZONE_CACHE_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(_TIMEZONE_CACHE_PATH).write_text(json.dumps(data, ensure_ascii=False))
    except Exception as e:
        logger.debug(f"Failed to save timezone cache: {e}")


# Static fallback list (kept for offline resilience)
_STATIC_TIMEZONES = [
    {"value": "system", "label": "System Default"},
    {"value": "utc", "label": "UTC"},
    {"value": "+08:00", "label": "GMT+8 (Beijing, Singapore)"},
    {"value": "+00:00", "label": "GMT+0 (London)"},
    {"value": "-05:00", "label": "GMT-5 (New York)"},
    {"value": "-08:00", "label": "GMT-8 (Los Angeles)"},
]


def _fetch_timezone_zones_local() -> list[str]:
    """Get IANA timezone names from Python's zoneinfo (local, no network)."""
    from zoneinfo import available_timezones
    zones = sorted(available_timezones())
    # Filter out some known-invalid / internal zones
    return [z for z in zones if z and not z.lower().startswith("factory")]


def _resolve_zone_offset_local(zone: str) -> int | None:
    """Resolve the current UTC offset for a zone using zoneinfo (local)."""
    from zoneinfo import ZoneInfo
    from datetime import datetime
    try:
        tz = ZoneInfo(zone)
        now = datetime.now(tz)
        offset = now.utcoffset()
        if offset is not None:
            return int(offset.total_seconds())
    except Exception:
        pass
    return None


async def _fetch_timezone_zones() -> list[str]:
    """Fetch the list of IANA timezone names from TimeAPI.io."""
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        resp = await client.get(
            "https://timeapi.io/api/timezone/availabletimezones",
            headers={"Accept": "application/json"},
        )
        if resp.status_code == 200:
            zones = resp.json()
            if isinstance(zones, list):
                return zones
    raise RuntimeError(f"TimeAPI returned status {resp.status_code if resp else 'N/A'}")


async def _resolve_zone_offsets(client: httpx.AsyncClient, zones: list[str]) -> dict:
    """Resolve UTC offsets for IANA zone strings via TimeAPI.io.

    Returns {zone_name: offset_seconds}. Parallel with semaphore=5 and
    429-retry-with-backoff to handle TimeAPI rate limiting.
    """
    results: dict[str, int] = {}
    semaphore = asyncio.Semaphore(5)

    async def _fetch_one(zone: str):
        async with semaphore:
            for attempt in range(3):
                try:
                    resp = await client.get(
                        f"https://timeapi.io/api/TimeZone/zone?timeZone={zone}",
                        headers={"Accept": "application/json"},
                    )
                    if resp.status_code == 429:
                        await asyncio.sleep(0.5 * (2 ** attempt))
                        continue
                    if resp.status_code == 200:
                        data = resp.json()
                        secs = data.get("currentUtcOffset", {}).get("seconds")
                        if secs is not None:
                            results[zone] = secs
                    return
                except Exception:
                    if attempt < 2:
                        await asyncio.sleep(0.2 * (attempt + 1))
                        continue
                    return

    # Process in small batches to avoid overwhelming TimeAPI
    batch_size = 10
    for i in range(0, len(zones), batch_size):
        batch = zones[i:i + batch_size]
        tasks = [_fetch_one(z) for z in batch]
        await asyncio.gather(*tasks)
        await asyncio.sleep(0.3)
    return results


def _format_offset_label(seconds: int) -> str:
    """Convert offset seconds to 'UTC+8' / 'UTC-5:30' style."""
    sign = "+" if seconds >= 0 else "-"
    abs_sec = abs(seconds)
    hours = abs_sec // 3600
    minutes = (abs_sec % 3600) // 60
    if minutes:
        return f"UTC{sign}{hours}:{minutes:02d}"
    return f"UTC{sign}{hours}"


def _build_timezone_list(zones: list[str], offset_map: dict) -> list[dict]:
    """Build the label/value list for the frontend from live zone data."""
    def _sort_key(zone: str):
        return (offset_map.get(zone, 0), zone)

    items = []
    seen_values = {"system", "utc"}
    items.append({"value": "system", "label": "System Default"})
    items.append({"value": "utc", "label": "UTC"})

    for zone in sorted(zones, key=_sort_key):
        if zone in seen_values:
            continue
        seen_values.add(zone)
        offset = offset_map.get(zone)
        label_zone = zone.replace("_", " ")
        if offset is not None:
            items.append({
                "value": zone,
                "label": f"{_format_offset_label(offset)} {label_zone}",
            })
        else:
            items.append({
                "value": zone,
                "label": label_zone,
            })
    return items


async def _refresh_timezone_cache():
    """One-shot: build full zone list with offsets and cache to disk.

    Primary source: Python's zoneinfo (local, instant, DST-aware, no network).
    Fallback source: TimeAPI.io (network, ~60s for 597 zones).
    Cached for 24h. On total failure, keeps existing cache or static fallback.
    """
    # --- Primary: local zoneinfo (instant, no network, DST-aware) ---
    try:
        zones = _fetch_timezone_zones_local()
        offset_map = {}
        for zone in zones:
            secs = _resolve_zone_offset_local(zone)
            if secs is not None:
                offset_map[zone] = secs
        available = _build_timezone_list(zones, offset_map)
        _save_timezone_cache({"timezones": available, "_source": "zoneinfo_local"})
        logger.info(f"Timezone cache refreshed from zoneinfo: {len(available)} zones, "
                    f"{len(offset_map)} with resolved offsets")
        return
    except Exception as e:
        logger.warning(f"zoneinfo local timezone resolution failed: {e}; falling back to TimeAPI.io")

    # --- Fallback: TimeAPI.io (network) ---
    try:
        zones = await _fetch_timezone_zones()
        logger.info(f"TimeAPI returned {len(zones)} zones; resolving offsets...")
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            offset_map = await _resolve_zone_offsets(client, zones)
        available = _build_timezone_list(zones, offset_map)
        _save_timezone_cache({"timezones": available, "_source": "timeapi_io"})
        logger.info(f"Timezone cache refreshed from TimeAPI.io: {len(available)} zones total, "
                    f"{len(offset_map)} with resolved offsets")
    except Exception as e:
        logger.warning(f"Failed to refresh timezone cache from TimeAPI.io: {e}")


@app.on_event("startup")
async def _llm_autostart():
    """Load the model chosen in Settings > Model as soon as Lucy Core starts.

    Already running (e.g. after a code reload): left alone. Any other model is unloaded.
    Disable with "autostart": False in llm_manager.DEFAULT_MODELS_CFG.
    """
    if llm.cfg.get("autostart", True):
        logger.info(f"Autostart: loading {llm.active['label']} on port {llm.active['port']} "
                    f"(kv={llm.state['kv_cache']}, reasoning={'on' if llm.reasoning else 'off'})")
        asyncio.get_running_loop().create_task(llm.ensure_active())


# === LLM settings (Settings > Model) ===
@app.get("/api/llm/status")
async def llm_status(_ = Depends(verify_api_key)):
    """Active model, temperature, reasoning, KV cache and per-model readiness (UI polls this while loading)."""
    return await llm.status()


@app.post("/api/llm/settings")
async def llm_settings(request: Request, _ = Depends(verify_api_key)):
    """Partial update: {model, temperature, reasoning, kv_cache}.

    model     -> unloads the other llama-server, loads this one (background; poll /api/llm/status).
                 Temperature resets to the model's preset (12B 0.7 / 4B 0.5).
    kv_cache / reasoning -> restart the active model with the new launch flags.
    temperature applies to the next message with no reload.
    """
    body = await request.json()
    try:
        todo = llm.update(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    llm.schedule(todo)
    if todo:
        llm.phase, llm.message = "loading", "Switching..." if "switch" in todo else "Reloading with new settings..."
    return await llm.status()


@app.get("/api/settings/timezone")
async def get_timezone_setting(_ = Depends(verify_api_key)):
    """Returns the current timezone configuration with live IANA zone list.

    Zone list is resolved once from Python's zoneinfo (instant, local, DST-aware)
    and cached for 24h. Falls back to TimeAPI.io if zoneinfo/tzdata is unavailable,
    then to a static list if the network is unreachable.
    """
    with _db_lock:
        row = _conn.execute("SELECT value FROM tz_config WHERE key = 'timezone'").fetchone()
    tz = row["value"] if row else "system"

    # Try cache first (in-memory or on-disk)
    cached = _load_timezone_cache()
    if cached and cached.get("timezones"):
        available = cached["timezones"]
    else:
        # Cache miss — perform refresh. zoneinfo is instant; TimeAPI fallback ~60s.
        logger.info("Timezone cache cold — performing refresh...")
        await _refresh_timezone_cache()
        cached = _load_timezone_cache()
        available = cached.get("timezones", []) if cached else _STATIC_TIMEZONES

    return {"timezone": tz, "available_timezones": available}


@app.post("/api/settings/timezone")
async def set_timezone_setting(request: Request, _ = Depends(verify_api_key)):
    """Sets the timezone configuration."""
    body = await request.json()
    tz = body.get("timezone", "system")
    with _db_lock:
        _conn.execute(
            "INSERT INTO tz_config (key, value) VALUES ('timezone', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (tz,),
        )
        _conn.commit()
    return {"status": "ok", "timezone": tz}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str, request: Request, _: bool = Depends(verify_api_key)):
    """Full message history (with attachments) of one session.

    Sent with an ETag, so reopening a session you already loaded costs a tiny "304 Not Modified"."""
    if not _session_exists(session_id):
        return {"session_id": session_id, "title": "New Session", "messages": []}
    with _db_lock:
        meta = _conn.execute(
            "SELECT s.title AS title, COUNT(m.rowid) AS n, COALESCE(MAX(m.rowid), 0) AS last "
            "FROM sessions s LEFT JOIN messages m ON m.session_id = s.session_id WHERE s.session_id = ?",
            (session_id,)).fetchone()
    etag = 'W/"%d-%d-%d-%08x"' % (meta["n"], meta["last"], _library.rev, zlib.crc32((meta["title"] or "").encode("utf-8")))
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}      # no-cache = always revalidate (cheap)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse({"session_id": session_id, "title": meta["title"], "messages": _get_messages_full(session_id)},
                        headers=headers)


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, _: bool = Depends(verify_api_key)):
    """Deletes a session."""
    if _delete_session_db(session_id):
        return {"status": "deleted", "session_id": session_id}
    raise HTTPException(status_code=404, detail="Session not found")


@app.get("/api/memory")
async def list_memories(_ = Depends(verify_api_key)):
    """Everything in persistent memory (agent-written and user-confirmed)."""
    return {"memories": _memory_manager.list_all()}


@app.delete("/api/memory/{memory_id}")
async def delete_memory(memory_id: int, _ = Depends(verify_api_key)):
    if _memory_manager.forget(memory_id):
        return {"status": "forgotten", "id": memory_id}
    raise HTTPException(status_code=404, detail="Memory not found")


@app.get("/api/memory/save")
async def save_memory_get(fact: str, session: str = None):
    """Save a memory fact to the knowledge base (called via inline confirmation link)."""
    try:
        _memory_manager.save_memory(fact)
        return {"status": "saved", "fact": fact}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.post("/api/restart")
async def restart_server(_ = Depends(verify_api_key)):
    """Restart the Lucy Core server (uses a background subprocess)."""
    import subprocess
    async def _delayed_restart():
        import asyncio
        await asyncio.sleep(1)
        subprocess.Popen([sys.executable, "-c",
            "import uvicorn; from lucy.server.api import app; "
            "uvicorn.run(app, host='0.0.0.0', port=8090, log_level='info')"
        ], env={**os.environ, "PYTHONPATH": str(_paths.SRC)},
        cwd=str(_paths.ROOT))
    asyncio.create_task(_delayed_restart())
    return {"status": "restarting", "detail": "Server will restart shortly"}


@app.post("/api/shutdown")
async def shutdown_server(_ = Depends(verify_api_key)):
    """Shut down the Lucy Core server."""
    import signal
    asyncio.get_event_loop().add_signal_handler(signal.SIGINT, lambda: None)
    asyncio.create_task(_shutdown_after_delay())
    return {"status": "shutting_down", "detail": "Server is shutting down"}


async def _shutdown_after_delay():
    import asyncio
    await asyncio.sleep(1)
    os._exit(0)


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
        trace=_agent_trace, session_id="proactive-task",
        gate=lambda name, args, _emit: _safety.get().gate(name, args, None, None))   # no approver in the background
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
            sticker=_proactive_sticker, speak=_proactive_speak, paused=lambda: _safety.get().paused))
        await _proactive.start()
    except Exception as e:                                # noqa: BLE001 - Lucy must start even if this cannot
        logger.error(f"Proactive system failed to start: {e}", exc_info=True)


@app.on_event("shutdown")
async def _proactive_stop():
    await _proactive.stop()


# === Safety layer (P1): permissions, approvals, audit, undo. These routes are for the USER (the UI); the agent has no
# tool that calls them, its shell gets no API key, and any shell command mentioning them always asks first.
@app.on_event("startup")
async def _safety_start():
    layer = _safety.install()
    if layer.constitution.note:
        logger.info(f"Safety: {layer.constitution.note}")
    if layer.constitution.problems:
        logger.error(f"Safety: constitution files changed without being sealed: {layer.constitution.problems}. "
                     "The agent will not run tools until you run: python -m lucy.safety seal")
    for p in layer.cfg.problems:
        logger.warning(f"Safety config: {p}")


@app.get("/api/safety/status")
async def safety_status(session_id: Optional[str] = None, _ = Depends(verify_api_key)):
    """Mode, pause flag, halted reason, pending approvals (for this chat), config problems."""
    return _safety.get().status(session_id)


@app.post("/api/safety/approvals/{approval_id}")
async def safety_approve(approval_id: str, request: Request, _ = Depends(verify_api_key)):
    """{decision: approve | deny | always}. Resumes the waiting agent turn."""
    body = await request.json()
    try:
        ok = _safety.get().broker.resolve(approval_id, str(body.get("decision", "")), body.get("session_id"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="no such pending approval (already answered, timed out or stopped)")
    return {"ok": True}


@app.post("/api/safety/mode")
async def safety_mode(request: Request, _ = Depends(verify_api_key)):
    body = await request.json()
    try:
        _safety.get().set_mode(str(body.get("mode", "")), by="user (settings)")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _safety.get().status(body.get("session_id"))


@app.post("/api/safety/pause")
async def safety_pause(request: Request, _ = Depends(verify_api_key)):
    """{paused: bool, kill_jobs: bool=true}. Paused = no tool runs anywhere (chat, background tasks, proactive)."""
    body = await request.json()
    paused = bool(body.get("paused", True))
    _safety.get().set_paused(paused, by="user (settings)")
    killed = 0
    if paused and body.get("kill_jobs", True):
        from lucy.tools import jobs as _jobs
        for j in _jobs.default.all():
            if j.running:
                j.kill()
                killed += 1
    return {**_safety.get().status(body.get("session_id")), "killed_jobs": killed}


@app.get("/api/safety/backups")
async def safety_backups(limit: int = 30, _ = Depends(verify_api_key)):
    return {"backups": _safety.get().backups.list(limit=max(1, min(limit, 200)))}


@app.post("/api/safety/backups/{backup_id}/restore")
async def safety_restore(backup_id: str, _ = Depends(verify_api_key)):
    msg = await asyncio.to_thread(_safety.get().backups.restore, backup_id, session=None, by="user (settings)")
    _safety.get().audit.write(event="restore", by="user (settings)", backup_id=backup_id, result=msg[:200])
    return {"ok": not msg.startswith(("Error", "Refused")), "message": msg}


@app.get("/api/safety/audit")
async def safety_audit(n: int = 50, _ = Depends(verify_api_key)):
    return {"entries": _safety.get().audit.tail(max(1, min(n, 500)))}


# === MCP (P4). Servers come from .core/mcp.json (a constitution file: only the user edits it, then reseals). These routes
# only switch a server the user already declared on/off, re-read its tool list or stop it. For the USER (the UI).
@app.on_event("startup")
async def _mcp_start():
    await asyncio.to_thread(_mcp.get().start)        # reads the config + cached tool lists; slow work runs in a thread
    for p in _mcp.get().problems:
        logger.warning(f"MCP config: {p}")


@app.on_event("shutdown")
async def _mcp_stop():
    await asyncio.to_thread(_mcp.get().shutdown)


@app.get("/api/mcp/status")
async def mcp_status(_ = Depends(verify_api_key)):
    return _mcp.get().status()


@app.post("/api/mcp/{key}/enabled")
async def mcp_enabled(key: str, request: Request, _ = Depends(verify_api_key)):
    """{enabled: bool}. Switching on reads the tool list in the background when it is not cached yet."""
    body = await request.json()
    m, on = _mcp.get(), bool(body.get("enabled", True))
    try:
        m.set_enabled(key, on)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"no MCP server called {key}")
    if on and key not in m._raw_tools:
        asyncio.get_running_loop().run_in_executor(None, m._safe_refresh, key)
    return m.status()


@app.post("/api/mcp/{key}/refresh")
async def mcp_refresh(key: str, _ = Depends(verify_api_key)):
    """Start the server (if needed) and re-read its tool list now."""
    m = _mcp.get()
    if key not in m.servers:
        raise HTTPException(status_code=404, detail=f"no MCP server called {key}")
    try:
        await asyncio.to_thread(m.refresh, key)
    except _mcp.McpError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return m.status()


@app.post("/api/mcp/{key}/stop")
async def mcp_stop(key: str, _ = Depends(verify_api_key)):
    await asyncio.to_thread(_mcp.get().stop, key)
    return _mcp.get().status()


app.include_router(_proactive.router, dependencies=[Depends(verify_api_key)])
app.include_router(_proactive.public_router)
