from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse
from pydantic import BaseModel
from typing import List, Dict, Optional, Any, Iterator, Iterable
import httpx
import asyncio
import logging
import time
import json
import os
import subprocess
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
MAX_TOOL_ROUNDS = int(os.environ.get("LUCY_MAX_TOOL_ROUNDS", "8"))    # tool rounds per turn
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

_db_lock = threading.Lock()
_conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row


def _get_tz_modifier():
    """Return the SQLite timezone modifier string for the configured timezone."""
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


def _append_message(session_id: str, role: str, content: str):
    _ensure_session(session_id)
    with _db_lock:
        _conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
            (session_id, role, content),
        )
        _conn.execute(
            f"UPDATE sessions SET updated_at = strftime('%Y-%m-%d %H:%M:%S', 'now', '{_get_tz_modifier()}') WHERE session_id = ?",
            (session_id,),
        )
        _conn.commit()


def _list_sessions_db() -> dict:
    with _db_lock:
        rows = _conn.execute(
            "SELECT session_id, title, message_count, updated_at FROM (SELECT s.session_id, s.title, COUNT(m.rowid) AS message_count, s.updated_at FROM sessions s LEFT JOIN messages m ON s.session_id = m.session_id GROUP BY s.session_id ORDER BY s.updated_at DESC)"
        ).fetchall()
    return {
        r["session_id"]: {
            "title": r["title"],
            "message_count": r["message_count"],
            "updated_at": r["updated_at"],
            "messages": _get_messages(r["session_id"]),
        }
        for r in rows
    }


def _delete_session_db(session_id: str) -> bool:
    with _db_lock:
        cur = _conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        _conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        _conn.commit()
        return cur.rowcount > 0

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
    {
        "type": "function",
        "function": {
            "name": "read_local_file",
            "description": f"Read a file from the local filesystem. Only safe paths under {_HOME} are allowed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": f"Absolute path to the file to read (e.g. {_HOME}/Desktop/Hello.txt)"
                    }
                },
                "required": ["file_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_local_file",
            "description": "Edit a file on the local filesystem. Reads the existing content, makes string replacements, and writes the updated result back to the same path. Use this for partial edits (adding/removing/modifying specific text sections) rather than overwriting the entire file. For each replacement, old_string must match exactly and uniquely (or use replace_all=true to replace all matches). Parent directories must already exist. Use \\\"replace_all\\\": true to replace all occurrences of a pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": f"Absolute path to the file to edit (e.g. {_HOME}/Desktop/Hello.txt)"
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact text to find in the file content"
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Replacement text to substitute for old_string"
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "If true, replace all occurrences of old_string. If false (default), only the first occurrence is replaced",
                        "default": False
                    }
                },
                "required": ["file_path", "old_string", "new_string"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_local_file",
            "description": f"Write content to a file on the local filesystem. Overwrites if the file exists. Only safe paths under {_HOME} are allowed. Parent directories are created automatically. Use this to write/create a brand new file. For editing an existing file, prefer edit_local_file to avoid accidental data loss.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": f"Absolute path to the file to write (e.g. {_HOME}/Desktop/Hello.txt)"
                    },
                    "content": {
                        "type": "string",
                        "description": "The text content to write to the file"
                    }
                },
                "required": ["file_path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_local_files",
            "description": f"List files in a local directory. Only safe paths under {_HOME} are allowed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dir_path": {
                        "type": "string",
                        "description": "Absolute path to the directory to list"
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern for filtering files (default: *)"
                    }
                },
                "required": ["dir_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell_command",
            "description": "Run a shell command on the local machine. Use bash syntax. Timeout is 10 seconds. Use this ONLY for system inspection (listing files, checking processes, etc.). Do NOT use this for web fetching — use web_search and web_extract tools instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash command to execute"
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_file",
            "description": f"Find a file by name within a specific directory and subdirectories. Only safe paths under {_HOME} are allowed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_name": {
                        "type": "string",
                        "description": "Name of the file to search for"
                    },
                    "dir_path": {
                        "type": "string",
                        "description": f"Root directory to search in (default: {_HOME})"
                    }
                },
                "required": ["file_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_file",
            "description": f"Signal to the client that a file is ready for the user to view or download. The file must already exist on disk under {_HOME}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": f"Absolute path to the file to send (e.g. {_HOME}/data/uploads/screenshot.png)"
                    },
                    "caption": {
                        "type": "string",
                        "description": "Optional caption for the file"
                    }
                },
                "required": ["file_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_image",
            "description": "Analyze an image file that was uploaded in this conversation. The image must have been sent as a file upload. Returns a detailed description of what is seen in the image, including objects, text, colors, layout, and any notable details. Use this whenever the user attaches an image and asks you to describe, explain, read, or analyze it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": f"Path to the uploaded image file on disk (e.g. {_ROOT_POSIX}/runtime/tmp/screenshot.png)"
                    },
                    "question": {
                        "type": "string",
                        "description": "What to analyze or ask about in the image (e.g. 'describe this code', 'read the text', 'what colors are used')"
                    }
                },
                "required": ["image_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for information. Returns top results with titles, URLs, and snippets. Use this to discover current information, research topics, find articles, and gather background context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query (e.g. 'health benefits of mandarin oranges')"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of results to return (default: 5, max: 10)",
                        "default": 5
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_extract",
            "description": "Extract clean, readable text content from one or more web pages. Converts HTML to markdown. Use after web_search to read the full content of the most relevant result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of URLs to extract content from. Only extract from the most relevant 1-2 URLs, never all 5 search results."
                    },
                    "char_limit": {
                        "type": "integer",
                        "description": "Maximum characters to return per page (default: 15000). Full text is saved to file for large pages.",
                        "default": 15000
                    }
                },
                "required": ["urls"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_tts",
            "description": "Synthesize speech from text using the CielVox 2.6 TTS model (via the Lucy Audio server on port 8091). Returns a MEDIA: path to a WAV file that the client plays automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The text to synthesize. Keep under ~500 characters per call for best quality and streaming behavior."
                    },
                    "voice": {
                        "type": "string",
                        "description": "Voice preset to use (e.g. 'frieren'). Defaults to the model's configured default.",
                        "default": "frieren"
                    }
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cua_cursor",
            "description": "Get or set the real OS cursor position via Win32 API. Pass action 'get' to read current position, or 'move' with x/y to move the cursor. Returns {'x': X, 'y': Y}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["get", "move"],
                        "description": "'get' returns current cursor position; 'move' requires x and y"
                    },
                    "x": {
                        "type": "integer",
                        "description": "Target X coordinate (required for action='move')"
                    },
                    "y": {
                        "type": "integer",
                        "description": "Target Y coordinate (required for action='move')"
                    }
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cua_mouse",
            "description": "Control mouse: click, drag, or move. action='click' requires x,y and optional button ('left'/'right'/'middle'). action='drag' requires x1,y1,x2,y2. action='move' requires x,y.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["click", "drag", "move"],
                        "description": "The mouse action to perform"
                    },
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "x1": {"type": "integer", "description": "Drag start X (for drag action)"},
                    "y1": {"type": "integer", "description": "Drag start Y (for drag action)"},
                    "x2": {"type": "integer", "description": "Drag end X (for drag action)"},
                    "y2": {"type": "integer", "description": "Drag end Y (for drag action)"},
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                        "default": "left"
                    }
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cua_keyboard",
            "description": "Send keyboard input. action='hotkey' requires keys list (e.g. ['win','d']). action='type' requires text string. action='press' requires a single key name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["hotkey", "type", "press", "minimize", "enter", "escape"],
                        "description": "The keyboard action to perform"
                    },
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Key combo components (for hotkey action, e.g. ['win','d'])"
                    },
                    "text": {
                        "type": "string",
                        "description": "Text to type (for type action)"
                    }
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_drive_files",
            "description": "List the most recent files in your Google Drive.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_google_doc",
            "description": "Create a new Google Doc with the given title and return its name and ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "The title for the new Google Doc"
                    }
                },
                "required": ["title"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_doc_content",
            "description": "Retrieve the full text content of a Google Doc by its title.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "The title of the Google Doc to retrieve"
                    }
                },
                "required": ["title"]
            }
        }
    },
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
    if tool_names:
        return [TOOL_REGISTRY[name] for name in sorted(tool_names)]
    return TOOLS  # fallback: all tools available


# --- Web Search Helpers ---

async def _web_search(query: str, limit: int = 5) -> str:
    """Search Bing and return top results as formatted text."""
    import re
    import html as html_module
    import base64
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(
                "https://www.bing.com/search",
                params={"q": query, "count": limit, "setlang": "en-US"},
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.5",
                },
            )
            if resp.status_code != 200:
                return f"Search failed with status {resp.status_code}"
            html_text = resp.text
    except Exception as e:
        return f"Search error: {e}"

    results = []
    links = re.findall(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html_text, re.DOTALL)
    cites = re.findall(r'<cite[^>]*>(.*?)</cite>', html_text, re.DOTALL)
    snippets = re.findall(r'class="b_caption"[^>]*>(.*?)</div>', html_text, re.DOTALL)

    for i in range(min(limit, len(links))):
        href, title_raw = links[i]
        title = html_module.unescape(re.sub(r'<[^>]+>', '', title_raw).strip())
        result_url = ""
        # Try to decode Bing redirect URL
        match = re.search(r'u=a1([A-Za-z0-9+/=]+)', href)
        if match:
            try:
                decoded = base64.b64decode(match.group(1)).decode('utf-8', errors='replace')
                result_url = decoded
            except Exception:
                pass
        # Fall back to cite for URL
        if not result_url and i < len(cites):
            cite_text = html_module.unescape(re.sub(r'<[^>]+>', '', cites[i]).strip())
            url_match = re.search(r'https?://[^\s]+', cite_text)
            if url_match:
                result_url = url_match.group(0).replace('&nbsp;', '').replace(' › ', '/').strip()
        # Also try extracting URL from the href itself if it's a direct link
        if not result_url:
            direct_url = re.search(r'https?://[^\s&]+', href)
            if direct_url:
                result_url = direct_url.group(0)
        snippet = ""
        if i < len(snippets):
            snippet = html_module.unescape(re.sub(r'<[^>]+>', '', snippets[i]).strip())
            snippet = snippet.replace('\n', ' ')[:200]
        results.append(f"[{i+1}] {title}\n    URL: {result_url}\n    {snippet}")

    if not results:
        return f"No results found for '{query}'"
    return "\n\n".join(results)


async def _web_extract(urls: list, char_limit: int = 15000) -> str:
    """Extract clean content from web pages using Jina Reader API, with fallback to direct scraping."""
    if isinstance(urls, str):
        urls = [urls]
    results = []
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            for url in urls:
                jina_url = f"https://r.jina.ai/{url}"
                try:
                    resp = await client.get(jina_url, timeout=30.0)
                    if resp.status_code == 200:
                        content = resp.text
                        if len(content) > char_limit:
                            content = content[:char_limit] + "\n\n[... truncated, full page was longer ...]"
                        results.append(f"=== {url} ===\n{content}")
                    else:
                        # Fallback: try Wikipedia API for Wikipedia URLs
                        if "wikipedia.org" in url:
                            api_url = url.replace("/wiki/", "/w/api.php").replace("/w/api.php", "/w/api.php")
                            # Better: use the REST API summary endpoint
                            title = url.split("/wiki/")[-1] if "/wiki/" in url else ""
                            if title:
                                rest_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
                                resp2 = await client.get(rest_url, headers={"User-Agent": "LucyCore/1.0"})
                                if resp2.status_code == 200:
                                    import json
                                    data = json.loads(resp2.text)
                                    extract = data.get("extract", "")
                                    content = f"# {data.get('title', title)}\n\n{extract}"
                                    if len(content) > char_limit:
                                        content = content[:char_limit] + "\n\n[... truncated ...]"
                                    results.append(f"=== {url} ===\n{content}")
                                else:
                                    results.append(f"=== {url} ===\n[Failed to extract Wikipedia page: status {resp2.status_code}]")
                            else:
                                results.append(f"=== {url} ===\n[Could not parse Wikipedia URL]")
                        else:
                            results.append(f"=== {url} ===\n[Failed to extract — Jina status {resp.status_code}]")
                except Exception as e:
                    results.append(f"=== {url} ===\n[Error extracting: {e}]")
    except Exception as e:
        return f"Extraction error: {e}"

    return "\n\n".join(results) if results else "No content extracted"


async def _analyze_image(image_path: str, question: str = "") -> str:
    """Analyze an image using the local llama-server multimodal model."""
    import base64

    # Read the image and encode as base64 data URL
    target = Path(image_path)
    if not target.exists():
        return f"Image not found: {image_path}"

    # Determine MIME type from extension
    mime_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
    }
    mime_type = mime_map.get(target.suffix.lower(), "image/png")

    image_b64 = base64.b64encode(target.read_bytes()).decode("utf-8")
    data_url = f"data:{mime_type};base64,{image_b64}"

    # Build the prompt for image analysis
    prompt = question if question else "Describe this image in detail. Include all visible text, objects, colors, layout, and any notable details."

    # Send to llama-server multimodal endpoint
    payload = {
        "model": llm.model_name,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "max_tokens": 2048,
        "temperature": 0.3,
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{llm.api_url}/chat/completions",
                json=payload,
            )
            if resp.status_code == 200:
                result = resp.json()
                if "choices" in result and result["choices"]:
                    return result["choices"][0]["message"]["content"]
                return "[No content returned from image analysis]"
            else:
                return f"[Image analysis failed: status {resp.status_code} — {resp.text[:500]}]"
    except Exception as e:
        return f"[Image analysis error: {e}]"


# --- Tool Execution ---

def _resolve_path(file_path: str) -> Path:
    """Resolve a path and verify it's under SAFE_ROOT."""
    target = Path(file_path).resolve()
    try:
        target.relative_to(SAFE_ROOT)
    except ValueError:
        return None
    return target


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


async def execute_tool_call(tool_name: str, args: dict) -> str:
    """Execute a single tool call and return its string result."""
    logger.info(f"Executing tool: {tool_name} with args: {args}")

    if tool_name in _MEMORY_TOOLS:
        return await asyncio.to_thread(_run_memory_tool, tool_name, args)

    if tool_name == "read_local_file":
        file_path = args.get("file_path", "")
        target = _resolve_path(file_path)
        if target is None:
            return f"Error: {file_path} is outside the allowed root."
        if not target.exists():
            return f"File not found: {file_path}"
        if not target.is_file():
            return f"Not a file: {file_path}"
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
            max_chars = 100_000
            if len(content) > max_chars:
                content = content[:max_chars] + "\n\n[... truncated ...]"
            return content
        except Exception as e:
            return f"[error reading file: {e}]"

    if tool_name == "edit_local_file":
        file_path = args.get("file_path", "")
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        replace_all = args.get("replace_all", False)
        # Normalize string booleans to actual Python booleans
        # (llama-server may emit "true"/"false" strings instead of true/false)
        if isinstance(replace_all, str):
            replace_all = replace_all.lower() == "true"
        target = _resolve_path(file_path)
        if target is None:
            return f"Error: {file_path} is outside the allowed root."
        if not target.exists() or not target.is_file():
            return f"File not found: {file_path}"
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
            if old_string not in content:
                return f"Error: 'old_string' not found in file: {file_path}"
            if not replace_all and content.count(old_string) > 1:
                return f"Error: 'old_string' matches {content.count(old_string)} times in {file_path}. Use replace_all=true or provide more context."
            occurrences = content.count(old_string) if replace_all else 1
            if replace_all:
                new_content = content.replace(old_string, new_string)
            else:
                new_content = content.replace(old_string, new_string, 1)
            target.write_text(new_content, encoding="utf-8")
            return f"Replaced {occurrences} occurrence(s) in {file_path}"
        except Exception as e:
            return f"[error editing file: {e}]"

    if tool_name == "write_local_file":
        file_path = args.get("file_path", "")
        content = args.get("content", "")
        target = _resolve_path(file_path)
        if target is None:
            return f"Error: {file_path} is outside the allowed root."
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"Written {len(content)} characters to {file_path}"
        except Exception as e:
            return f"[error writing file: {e}]"

    elif tool_name == "list_local_files":
        dir_path = args.get("dir_path", "")
        pattern = args.get("pattern", "*")
        target = _resolve_path(dir_path)
        if target is None:
            return f"Error: {dir_path} is outside the allowed root."
        if not target.exists():
            return f"Directory not found: {dir_path}"
        if not target.is_dir():
            return f"Not a directory: {dir_path}"
        try:
            entries = sorted(target.glob(pattern))
            if not entries:
                return f"No files matching '{pattern}' in {dir_path}"
            lines = [f"{e.name}{'/' if e.is_dir() else ''}" for e in entries]
            return "\n".join(lines)
        except Exception as e:
            return f"[error listing files: {e}]"

    elif tool_name == "run_shell_command":
        command = args.get("command", "")
        try:
            result = subprocess.run(
                ["bash", "-c", command],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(SAFE_ROOT),
            )
            output = result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}"
            if len(output) > 100_000:
                output = output[:100_000] + "\n\n[... truncated ...]"
            return output if output else "[command produced no output]"
        except subprocess.TimeoutExpired:
            return "[command timed out after 10s]"
        except Exception as e:
            return f"[error running command: {e}]"

    elif tool_name == "find_file":
        file_name = args.get("file_name", "")
        dir_path = args.get("dir_path", str(SAFE_ROOT))
        target = _resolve_path(dir_path)
        if target is None:
            return f"Error: {dir_path} is outside the allowed root."
        # Validate file_name — reject shell-dangerous characters to prevent
        # command injection in the find command below
        _dangerous = [';', '|', '&', '$', '`', '"', "'"]
        if not file_name or any(c in file_name for c in _dangerous):
            return "Error: file_name contains invalid characters"
        try:
            result = subprocess.run(
                ["bash", "-c", f'find "{target}" -name "{file_name}" -maxdepth 4 2>/dev/null | head -20'],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(SAFE_ROOT),
            )
            output = result.stdout.strip()
            if output:
                return output
            return f"No files matching '{file_name}' found in {dir_path}"
        except subprocess.TimeoutExpired:
            return f"[find timed out after 10s in {dir_path}]"
        except Exception as e:
            return f"[error: {e}]"

    elif tool_name == "send_file":
        file_path = args.get("file_path", "")
        caption = args.get("caption")
        target = _resolve_path(file_path)
        if target is None:
            return f"Error: {file_path} is outside the allowed root."
        if not target.exists() or not target.is_file():
            return f"File not found: {file_path}"
        # Return MEDIA: marker so Hermes/Discord sends it as an attachment
        # Use forward slashes in the path for cross-platform frontend compatibility
        # Calculate path relative to SAFE_ROOT
        try:
            relative_path = target.relative_to(SAFE_ROOT)
            media_path_str = relative_path.as_posix()
        except ValueError:
            # Fallback: use the absolute path if relative fails
            media_path_str = target.as_posix()
            
        # Ensure we have a clean string for the MEDIA marker
        result = f"MEDIA:{media_path_str}"
        if caption:
            result += f" {caption}"
        return result

    elif tool_name == "web_search":
        query = args.get("query", "")
        limit = args.get("limit", 5)
        return await _web_search(query, limit)

    elif tool_name == "web_extract":
        urls = args.get("urls", [])
        char_limit = args.get("char_limit", 15000)
        return await _web_extract(urls, char_limit)

    elif tool_name == "analyze_image":
        image_path = args.get("image_path", "")
        question = args.get("question", "")
        target = _resolve_path(image_path)
        if target is None or not target.exists():
            return f"Image not found or outside allowed path: {image_path}"
        return await _analyze_image(str(target), question)

    elif tool_name == "send_tts":
        text = args.get("text", "")
        voice = args.get("voice", "frieren")
        if not text:
            return "send_tts requires 'text' parameter"
        # Save the text to a temp input file for the wrapper to read
        import uuid as _uuid
        _ts = time.strftime("%Y%m%d_%H%M%S")
        input_file = _paths.HERMES_HOME / "cache" / "scratch" / f"tts_input_{_uuid.uuid4().hex[:8]}.txt"
        input_file.parent.mkdir(parents=True, exist_ok=True)
        input_file.write_text(text, encoding="utf-8")
        # Call the wrapper which proxies to lucy audio server on 8091
        # Use the baked-in wrapper inside Lucy_Core/lucy_audio/ first;
        # fall back to the Hermes scripts dir for backwards compat.
        baked_wrapper = _paths.ROOT / "lucy_audio" / "build" / "windows-vulkan-release" / "bin" / "tts-wrapper-lucy-cielvox26.bat"
        hermes_wrapper = _paths.HERMES_HOME / "scripts" / "tts-wrapper-lucy-cielvox26.bat"
        wrapper = baked_wrapper if baked_wrapper.exists() else hermes_wrapper
        output_file = _paths.HERMES_HOME / "cache" / "audio" / f"tts_lucy_{_uuid.uuid4().hex[:8]}.wav"
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # --- Delta stream log ---
        log_path = _paths.RUNTIME / "logs" / "deltastream_tts.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        def _log_deltastream(status: str, msg: str = "", media_path: str = ""):
            entry = {
                "timestamp": _ts,
                "session": "current",
                "tool": "send_tts",
                "status": status,
                "voice": voice,
                "text_preview": text[:80] + ("..." if len(text) > 80 else ""),
                "message": msg,
                "media": media_path,
            }
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        _log_deltastream("started", text)
        try:
            result = subprocess.run(
                ["cmd", "/c", str(wrapper), str(input_file), str(output_file)],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                _log_deltastream("error", f"wrapper exit {result.returncode}: {result.stderr[:200]}")
                logger.error(f"TTS wrapper failed: {result.stderr}")
                return f"TTS failed: {result.stderr[:200]}"
            if output_file.exists() and output_file.stat().st_size > 1000:
                _log_deltastream("success", f"WAV {output_file.stat().st_size} bytes", str(output_file).replace("\\", "/"))
                # Return MEDIA: path with forward slashes for frontend compatibility
                return f"MEDIA:{str(output_file).replace(chr(92), '/')}"
            _log_deltastream("error", f"output missing/too small (size={output_file.stat().st_size if output_file.exists() else 0})")
            return f"TTS error: output file missing or too small"
        except subprocess.TimeoutExpired:
            _log_deltastream("timeout", "wrapper exceeded 120s")
            logger.error("TTS wrapper timed out after 120s")
            return f"TTS error: lucy audio server timeout (120s)"
        except Exception as e:
            _log_deltastream("error", str(e))
            logger.error(f"TTS call error: {e}")
            return f"TTS error: {e}"

    elif tool_name in ("cua_cursor", "cua_mouse", "cua_keyboard"):
        # Dispatch CUA tools to the lucy_cua module (Win32 API backend)
        from lucy.cua.lucy_cua import (
            get_cursor_pos, set_cursor_pos, mouse_click,
            mouse_drag, key_combo, type_text, minimize_all,
        )
        try:
            if tool_name == "cua_cursor":
                action = args.get("action", "get")
                if action == "get":
                    return json.dumps(get_cursor_pos())
                elif action == "move":
                    x, y = args["x"], args["y"]
                    return json.dumps(set_cursor_pos(x, y))
                return json.dumps({"error": f"Unknown cursor action: {action}"})

            elif tool_name == "cua_mouse":
                action = args.get("action", "click")
                if action == "click":
                    x, y = args["x"], args["y"]
                    button = args.get("button", "left")
                    return json.dumps(mouse_click(x, y, button))
                elif action == "move":
                    x, y = args["x"], args["y"]
                    return json.dumps(set_cursor_pos(x, y))
                elif action == "drag":
                    x1, y1, x2, y2 = args["x1"], args["y1"], args["x2"], args["y2"]
                    return json.dumps(mouse_drag(x1, y1, x2, y2))
                return json.dumps({"error": f"Unknown mouse action: {action}"})

            elif tool_name == "cua_keyboard":
                action = args.get("action", "press")
                if action == "hotkey":
                    keys = args.get("keys", [])
                    return json.dumps(key_combo(keys))
                elif action == "type":
                    text_content = args.get("text", "")
                    return json.dumps(type_text(text_content))
                elif action in ("minimize", "enter", "escape"):
                    key_map = {"minimize": ["win", "d"], "enter": ["enter"], "escape": ["escape"]}
                    return json.dumps(key_combo(key_map[action]))
                return json.dumps({"error": f"Unknown keyboard action: {action}"})

        except Exception as e:
            logger.error(f"CUA tool error ({tool_name}): {e}")
            return json.dumps({"error": str(e)})

    if tool_name == "list_drive_files":
        from lucy.tools import Toolset
        toolset = Toolset()
        return toolset.list_drive_files()

    if tool_name == "create_google_doc":
        from lucy.tools import Toolset
        toolset = Toolset()
        title = args.get("title", "")
        return toolset.create_google_doc(title)

    if tool_name == "get_doc_content":
        from lucy.tools import Toolset
        toolset = Toolset()
        title = args.get("title", "")
        return toolset.get_doc_content(title)

    else:
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


def _build_messages(session_id: str, user_message: str, image_paths: List[str] = None) -> List[Dict[str, Any]]:
    """Build the full message list for the llama-server request."""
    messages = [{"role": "system", "content": _persona_prompt}]

    # Inject long-term memory
    try:
        memory_context = _memory_manager.get_contextual_memory(user_message)
        if memory_context:
            messages.append({"role": "system", "content": f"[USER FACTS]: {memory_context}"})
    except Exception as e:
        logger.warning(f"Failed to retrieve memory context: {e}")

    messages.append({"role": "system", "content": (
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
    )})

    # Inject relevant skills
    try:
        relevant_skills = _skills_manager.get_relevant_skills(user_message)
        if relevant_skills:
            skill_instructions = "\n".join([f"[{s['name']}]: {s['instructions']}" for s in relevant_skills])
            messages.append({"role": "system", "content": f"[ACTIVE SKILLS]: {skill_instructions}"})
    except Exception as e:
        logger.warning(f"Failed to retrieve skills: {e}")

    # --- Tool usage guidance (Fix 2 reinforcement) ---
    # Reinforce that edit/update/patch all map to edit_local_file,
    # and edit should always be called (not just narrated) when the user
    # asks to modify an existing file.
    messages.append({"role": "system", "content": (
        "TOOL USAGE RULES: (1) write_local_file — for creating NEW files only. "
        "(2) edit_local_file — for modifying EXISTING files. "
        "When the user says 'edit', 'update', 'patch', 'modify', or 'change' a file, "
        "ALWAYS call edit_local_file. Do not narrate edits without calling the tool. "
        "edit_local_file does partial string replacement — use it to change specific "
        "sections without rewriting the whole file. "
        "(3) read_local_file — for viewing file contents. "
        "When you need to know what's in a file, call read_local_file first, "
        "then use edit_local_file with the correct old_string."
    )})

    # Session history (limit to last N messages to avoid context overflow)
    messages.extend(_get_messages(session_id)[-MAX_CONTEXT_MESSAGES:])

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
# Maps tool names to a plain fallback activity label (no detail available).
_TOOL_ACTIVITY_MAP = {
    "web_search": "search",
    "web_extract": "read",
    "list_local_files": "list",
    "find_file": "search",
    "run_shell_command": "run",
    "send_file": "send",
    "analyze_image": "analyze",
    "send_tts": "speak",
    "cua_cursor": "control",
    "cua_mouse": "control",
    "cua_keyboard": "control",
    "list_drive_files": "drive",
    "create_google_doc": "docs",
    "get_doc_content": "docs",
    "remember": "remember",
    "recall_memory": "recall",
    "forget": "forget",
    "search_conversations": "history",
}

_ACTIVITY_DETAIL_MAX = 60  # truncate long detail (queries/commands) so the UI stays one line


def _truncate(s: str, limit: int = _ACTIVITY_DETAIL_MAX) -> str:
    s = s.strip()
    return s if len(s) <= limit else s[:limit - 1] + "\u2026"


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
    if tool_name == "web_search" and args.get("query"):
        return f"search:{_truncate(args['query'])}"
    if tool_name == "web_extract" and args.get("urls"):
        return f"read:{_truncate(', '.join(args['urls'][:2]))}"
    if tool_name == "analyze_image" and args.get("image_path"):
        return f"analyze:{args['image_path']}"
    if tool_name == "send_tts" and args.get("text"):
        return f"speak:{_truncate(args['text'], 40)}"
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

    # No usable detail in args — fall back to a plain category label.
    if tool_name in _TOOL_ACTIVITY_MAP:
        return _TOOL_ACTIVITY_MAP[tool_name]
    return tool_name  # last resort: raw tool name


# --- Stop / queued-message support ---
# The UI can abort a reply mid-stream and immediately send the next message.
# This marks "the previous reply for this session is still running / cleaning
# up" so the next request doesn't build its history before the interrupted
# reply has been saved.
_session_inflight: Dict[str, asyncio.Event] = {}

import re

def _clean_text_for_tts(text: str) -> str:
    """Remove Action: prefixes, markdown bold/italic markers, and emojis
    from text before it is sent to the TTS engine.

    This prevents the voice from reading out 'Action: send_tts' or stuttering
    on **bold** markers like 'star star Hello star star'.
    """
    import re
    # Remove ReAct-style "Action:" / "Action Input:" prefixes (multiline)
    text = re.sub(r'^(Action|Action Input):\s*', '', text, flags=re.MULTILINE | re.IGNORECASE)
    # Strip markdown bold and italic markers (** and __ or * and _)
    text = text.replace('**', '').replace('__', '')
    text = text.replace('*', '').replace('_', '')
    # Remove emojis and other non-speech Unicode symbols
    # Using a simple approach: remove anything not a word char, space, or basic punctuation.
    text = re.sub(r'[^\w\s.,!?;:\'"()\n-]', '', text, flags=re.UNICODE)
    # Collapse multiple spaces/tabs into one
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()



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
    if temperature is None:
        temperature = llm.temperature          # Settings > Model value (preset per model)
    available_tools = _resolve_skills_to_tools(skill_list) if skill_list else TOOLS
    agent = AgentLoop(
        brain=_BRAIN, tools=available_tools,
        execute=lambda name, args: execute_tool_call(name, args),   # looked up at call time
        activity_for=_tool_to_activity, count_tokens=_count_tokens, temperature=temperature,
        cfg=LoopConfig(max_rounds=MAX_TOOL_ROUNDS, max_tokens=LUCY_MAX_TOKENS, tool_timeout=LUCY_TOOL_TIMEOUT,
                       ctx_tokens=int(llm.cfg.get("context_length", 100000)), http_timeout=LUCY_TIMEOUT),
        trace=_agent_trace, session_id=session_id)

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
            _tts_buffer += ev.data
            yield f"data: {json.dumps({'content': ev.data})}\n\n"
            # Incremental TTS: speak each finished sentence while the model is still generating.
            if voice_mode and re.search(r'[。．！？！？.!?…\n]\s*$', _tts_buffer):
                chunk, _tts_buffer = _tts_buffer.strip(), ""
                if chunk:
                    async for line in _speak(chunk):
                        yield line
        elif ev.kind == "activity":
            yield f"data: {json.dumps({'activity': ev.data})}\n\n"
        elif ev.kind == "media":
            yield f"data: {json.dumps({'media': ev.data})}\n\n"
        elif ev.kind == "error":
            yield f"data: {json.dumps(ev.data)}\n\n"
            return
        elif ev.kind == "metrics":
            _stats = ev.data

    # --- Final TTS for any remaining buffered text (voice_mode) ---
    # Catches the last sentence fragment that didn't end with punctuation.
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

    # Allow empty message when files are attached (the model can reason about the image)
    if not message and not files and not file_paths:
        raise HTTPException(status_code=400, detail="No message provided")

    # Handle uploaded files — save them to a temp upload dir
    uploaded_file_paths = []
    image_paths = []
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
    # Store user message in session
    _append_message(session_id, "user", message)
    # Check for memory-worthy fact in the user message
    memory_fact = detect_memory_candidate(message)
    _agent_saved_turn.discard(session_id)
    # Generate session title from first message if it's still "New Session"
    asyncio.create_task(_generate_session_title(session_id))
    reply_done = asyncio.Event()
    _session_inflight[session_id] = reply_done

    async def event_stream():
        full_response = ""
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
                    except json.JSONDecodeError:
                        pass
                yield event_data

            # Store assistant response in session history (including any media markers)
            if full_response.strip():
                stored_response = full_response
                # Check for media references in the tool results
                for tool_result in messages:
                    if tool_result.get("role") == "tool" and tool_result.get("content", "").startswith("MEDIA:"):
                        media_path = tool_result["content"].split(" ", 1)[0].replace("MEDIA:", "")
                        stored_response += f"\nMEDIA:{media_path}"
                _append_message(session_id, "assistant", stored_response)
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
                _append_message(session_id, "assistant", f"{partial}\n\n{note}" if partial else note)
            except Exception as e:
                logger.error(f"Failed to save interrupted reply: {e}")
            raise
        except Exception as e:
            logger.error(f"Streaming error: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            reply_done.set()
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


@app.get("/api/file/{path:path}")
async def serve_file(path: str, _: bool = Depends(verify_api_key)):
    """Serve a file from the safe root for the frontend to download/view."""
    file_path = (SAFE_ROOT / path).resolve()
    try:
        file_path.relative_to(SAFE_ROOT)
    except ValueError:
        raise HTTPException(status_code=403, detail="Path outside allowed root")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)


@app.get("/api/skills")
async def list_skills(_ = Depends(verify_api_key)):
    """Lists all available skills for the frontend.

    Returns skills with: id, name, trigger, instructions, tools, category, source.
    """
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
    sessions = _list_sessions_db()
    return {
        "active_sessions": len(sessions),
        "total_messages": sum(s["message_count"] for s in sessions.values()),
        "sessions": {sid: s["message_count"] for sid, s in sessions.items()},
    }


@app.get("/api/sessions")
async def list_sessions(_: bool = Depends(verify_api_key)):
    """Returns all sessions with metadata for the sidebar."""
    return {"sessions": _list_sessions_db()}


@app.post("/api/sessions")
async def create_session(_: bool = Depends(verify_api_key)):
    """Creates a new empty session and returns its ID."""
    import uuid
    session_id = str(uuid.uuid4())
    _ensure_session(session_id)
    # Set updated_at immediately so the session sorts to the top of the list
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
async def get_session(session_id: str, _: bool = Depends(verify_api_key)):
    """Returns full message history for a specific session."""
    if not _session_exists(session_id):
        return {"session_id": session_id, "messages": []}
    return {"session_id": session_id, "messages": _get_messages(session_id)}


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
