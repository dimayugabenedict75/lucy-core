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
LLAMACPP_API_URL = "http://localhost:8080/v1"
LUCY_AUDIO_TTS_URL = "http://127.0.0.1:8091/v1/audio/speech"
LUCY_AUDIO_MODEL = "cielvox26"
MODEL_NAME = os.environ.get("LUCY_MODEL_PATH", "D:/AI/lucy/LLM/Lux-Plus-M12B.i1-QAT_Q4_K.gguf")
MAX_CONTEXT_TOKENS = 8192      # token budget for session history + system prompts
MAX_CONTEXT_MESSAGES = 50      # hard ceiling on messages loaded (safety)
COMPRESSION_THRESHOLD = 0.75   # start summarizing when 75% of budget is used
SUMMARY_RESERVE_TOKENS = 2048  # keep this many tokens free for the response
DEFAULT_TEMPERATURE = 0.5
MAX_TOOL_ROUNDS = 5
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
        "model": MODEL_NAME,
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
                f"{LLAMACPP_API_URL}/chat/completions", json=payload
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
                "type": "json",
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
                "type": "json",
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
                "type": "json",
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
                "type": "json",
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
                "type": "json",
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
                "type": "json",
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
                "type": "json",
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
                "type": "json",
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
                "type": "json",
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
                "type": "json",
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
                "type": "json",
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
                "type": "json",
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
                "type": "json",
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
                "type": "json",
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
                "type": "json",
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
                "type": "json",
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
                "type": "json",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "The title of the Google Doc to retrieve"
                    }
                },
                "required": ["title"]
            }
        }
    }
]

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
        "model": MODEL_NAME,
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
                f"{LLAMACPP_API_URL}/chat/completions",
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

async def _stream_tts_to_sse(text: str, voice: str = "frieren") -> Iterator[str]:
    """Connect to Lucy Audio's SSE streaming endpoint and yield PCM chunks.

    Yields strings in the format: MEDIA:/path/to/pcm_chunk_<uuid>
    Each chunk is a small s16LE PCM file that the frontend plays immediately.
    """
    import uuid as _uuid
    import httpx
    from pathlib import Path

    chunk_dir = _paths.HERMES_HOME / "cache" / "audio" / "stream"
    chunk_dir.mkdir(parents=True, exist_ok=True)

    request_body = {
        "model": LUCY_AUDIO_MODEL,
        "input": text,
        "response_format": "pcm",
        "stream_format": "sse",
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
                    return

                buffer = ""
                async for chunk in resp.aiter_text():
                    buffer += chunk
                    # SSE: split on blank lines (event boundaries)
                    while "\n\n" in buffer:
                        event_data, buffer = buffer.split("\n\n", 1)
                        for line in event_data.split("\n"):
                            if line.startswith("data: "):
                                payload = line[6:]
                                if payload == "[DONE]":
                                    return
                                try:
                                    ev = json.loads(payload)
                                except json.JSONDecodeError:
                                    continue
                                if ev.get("type") == "speech.audio.delta":
                                    audio_b64 = ev.get("audio", "")
                                    if audio_b64:
                                        pcm_data = base64.b64decode(audio_b64)
                                        chunk_path = chunk_dir / f"pcm_{_uuid.uuid4().hex[:8]}.pcm"
                                        chunk_path.write_bytes(pcm_data)
                                        yield f"MEDIA:{chunk_path.as_posix()}"
                                elif ev.get("type") == "speech.audio.done":
                                    logger.debug("Lucy Audio stream done")
                                    return
                                elif ev.get("type") == "error":
                                    err_msg = ev.get("error", {})
                                    logger.error(f"Lucy Audio stream error: {err_msg}")
                                    return
    except httpx.ConnectError:
        logger.warning("Lucy Audio server not reachable on 8091 — is voice mode on?")
    except Exception as e:
        logger.error(f"Lucy Audio stream exception: {e}")


async def execute_tool_call(tool_name: str, args: dict) -> str:
    """Execute a single tool call and return its string result."""
    logger.info(f"Executing tool: {tool_name} with args: {args}")

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
            resp = await client.get(f"{LLAMACPP_API_URL}/models")
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
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": DEFAULT_TEMPERATURE,
        "max_tokens": 2048,
        "tools": TOOLS,
        "tool_choice": "auto",
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
                resp = await client.stream("POST", f"{LLAMACPP_API_URL}/chat/completions", json=payload)
                # Check if the stream response started with an error
                # (client.stream returns 200, but the first SSE line may contain an error)
                return resp
            else:
                resp = await client.post(f"{LLAMACPP_API_URL}/chat/completions", json=payload)
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


# --- Stop / queued-message support ---------------------------------------
# The UI can abort a reply mid-stream and immediately send the next message.
# This marks "the previous reply for this session is still running / cleaning
# up" so the next request doesn't build its history before the interrupted
# reply has been saved.
_session_inflight: Dict[str, asyncio.Event] = {}


async def _wait_for_previous_reply(session_id: str, timeout: float = 5.0):
    prev = _session_inflight.get(session_id)
    if prev is not None and not prev.is_set():
        try:
            await asyncio.wait_for(prev.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"Previous reply for session {session_id} did not finish within {timeout}s; continuing.")


async def _stream_response(messages: List[Dict[str, Any]], temperature: float = DEFAULT_TEMPERATURE, skill_list: list[dict] = None, voice_mode: bool = False):
    """Stream a response from llama-server, handling tool calls in a loop.

    Emits SSE lines: {content}, {activity}, {media}, {done}, and finally [METRICS]{json}.
    """
    import time as _time_mod
    _start_time = _time_mod.time()
    _total_tool_calls = 0
    _full_response_text = ""
    round_num = 0
    _tts_voice = "frieren"
    _tts_buffer = ""  # accumulates sentence fragments during streaming

    # Emit an initial "thinking" activity indicator for the first round
    yield f"data: {json.dumps({'activity': 'think'})}\n\n"

    while round_num < MAX_TOOL_ROUNDS:
        round_num += 1
        tool_calls_seen = False

        async with httpx.AsyncClient(timeout=LUCY_TIMEOUT) as client:
            # Resolve available tools from active skills (dynamic skill→tool mapping)
            available_tools = _resolve_skills_to_tools(skill_list) if skill_list else TOOLS
            payload = {
                "model": MODEL_NAME,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": 2048,
                "tools": available_tools,
                "tool_choice": "auto",
                "stream": True,
                "stream_options": {"include_usage": True},
            }

            async with client.stream("POST", f"{LLAMACPP_API_URL}/chat/completions", json=payload) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    yield f"data: {json.dumps({'error': 'Failed to connect to brain', 'status': resp.status_code, 'detail': error_body.decode()[:500]})}\n\n"
                    return

                assistant_content = ""
                tool_calls: List[Dict[str, Any]] = []
                finish_reason = None

                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        chunk_data = line[6:]
                        if chunk_data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(chunk_data)
                            if chunk.get("choices"):
                                choice = chunk["choices"][0]
                                delta = choice.get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    assistant_content += content
                                    _full_response_text += content
                                    _tts_buffer += content
                                    yield f"data: {json.dumps({'content': content})}\n\n"

                                    # --- Incremental TTS streaming ---
                                    # When voice_mode is on, fire streaming TTS on each
                                    # completed sentence fragment so audio starts playing
                                    # while the LLM is still generating — not after.
                                    if voice_mode and _tts_buffer:
                                        import re as _re
                                        _sentence_end = _re.search(r'[。．！？！？.!?…\n]\s*$', _tts_buffer)
                                        if _sentence_end:
                                            _chunk = _tts_buffer.strip()
                                            _tts_buffer = ""
                                            if _chunk:
                                                yield f"data: {json.dumps({'activity': 'speak'})}\n\n"
                                                # Stream PCM chunks as they arrive from Lucy Audio SSE
                                                async for _media_event in _stream_tts_to_sse(_chunk, _tts_voice):
                                                    if _media_event.startswith("MEDIA:"):
                                                        _media_path = _media_event.split(" ", 1)[0].replace("MEDIA:", "")
                                                        yield f"data: {json.dumps({'media': _media_path})}\n\n"
                                                yield f"data: {json.dumps({'activity': None})}\n\n"

                                # Handle tool calls (cumulative — we collect args)
                                tc_list = delta.get("tool_calls", [])
                                if tc_list:
                                    for tc in tc_list:
                                        idx = tc.get("index", 0)
                                        while len(tool_calls) <= idx:
                                            tool_calls.append({"id": "", "name": "", "arguments": ""})
                                        if "id" in tc:
                                            tool_calls[idx]["id"] = tc["id"]
                                        if "name" in tc.get("function", {}):
                                            tool_calls[idx]["name"] = tc["function"]["name"]
                                        if "arguments" in tc.get("function", {}):
                                            tool_calls[idx]["arguments"] += tc["function"]["arguments"]

                                finish_reason = choice.get("finish_reason")
                        except json.JSONDecodeError:
                            continue

                if finish_reason == "tool_calls" and tool_calls:
                    tool_calls_seen = True

                    # Add assistant message with tool calls to conversation
                    tool_call_objs = []
                    for tc in tool_calls:
                        tool_call_objs.append({
                            "id": tc["id"],
                            "type": "function",
                            "function": {"name": tc["name"], "arguments": tc["arguments"]},
                        })
                    messages.append({
                        "role": "assistant",
                        "content": assistant_content,
                        "tool_calls": tool_call_objs,
                    })

                    # Execute each tool call and add results — emit per-tool activity events
                    _total_tool_calls += len(tool_calls)
                    for tc in tool_calls:
                        name = tc["name"]
                        try:
                            args = json.loads(tc["arguments"]) if tc["arguments"].strip() else {}
                        except json.JSONDecodeError:
                            args = {}

                        # Emit activity indicator BEFORE executing the tool
                        activity = _tool_to_activity(name, args)
                        yield f"data: {json.dumps({'activity': activity})}\n\n"

                        result = await execute_tool_call(name, args)

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result,
                        })

                        # If the tool returned a MEDIA: marker, emit it to the frontend
                        if result.startswith("MEDIA:"):
                            media_path = result.split(" ", 1)[0].replace("MEDIA:", "")
                            yield f"data: {json.dumps({'media': media_path})}\n\n"

                    # Clear activity, then loop back — emit "think" for the next round
                    yield f"data: {json.dumps({'activity': None})}\n\n"
                    yield f"data: {json.dumps({'activity': 'think'})}\n\n"

                    # Loop back to request another round of completions
                    continue
                else:
                    # No tool calls — response is complete
                    break

    else:
        # While-loop exhausted (MAX_TOOL_ROUNDS reached without final text response).
        # Force a final generation with no tools available.
        logger.warning("Max tool rounds reached without completion — forcing final response")
        force_messages = list(messages)
        force_messages.append({
            "role": "system",
            "content": (
                "You have used all your tool calls for this turn and cannot call any more. "
                "Do not fill remaining gaps with assumptions or guesses. Report only what you "
                "actually confirmed via tools. For anything you did not confirm, say plainly "
                "that you don't know yet and ask the user, rather than presenting a guess as fact."
            ),
        })
        payload = {
            "model": MODEL_NAME,
            "messages": force_messages,
            "temperature": temperature,
            "max_tokens": 2048,
            "tools": [],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        async with httpx.AsyncClient(timeout=LUCY_TIMEOUT) as client:
            async with client.stream("POST", LLAMACPP_API_URL + "/chat/completions", json=payload) as resp:
                if resp.status_code == 200:
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            chunk_data = line[6:]
                            if chunk_data == "[DONE]":
                                break
                            try:
                                chunk = json.loads(chunk_data)
                                if chunk.get("choices"):
                                    choice = chunk["choices"][0]
                                    delta = choice.get("delta", {})
                                    content = delta.get("content", "")
                                    if content:
                                        _full_response_text += content
                                        _tts_buffer += content
                                        yield f"data: {json.dumps({'content': content})}\n\n"
                                        # Incremental TTS in the forced-response path too (streaming)
                                        if voice_mode and _tts_buffer:
                                            import re as _re2
                                            _sent2 = _re2.search(r'[。．！？！？.!?…\n]\s*$', _tts_buffer)
                                            if _sent2:
                                                _chunk2 = _tts_buffer.strip()
                                                _tts_buffer = ""
                                                if _chunk2:
                                                    yield f"data: {json.dumps({'activity': 'speak'})}\n\n"
                                                    async for _ev2 in _stream_tts_to_sse(_chunk2, _tts_voice):
                                                        if _ev2.startswith("MEDIA:"):
                                                            _mp2 = _ev2.split(" ", 1)[0].replace("MEDIA:", "")
                                                            yield f"data: {json.dumps({'media': _mp2})}\n\n"
                                                    yield f"data: {json.dumps({'activity': None})}\n\n"
                            except json.JSONDecodeError:
                                                continue

    # --- Final TTS for any remaining buffered text (voice_mode) ---
    # During streaming, most of the response was already spoken incrementally.
    # This catches the last sentence fragment that didn't end with punctuation.
    if voice_mode and _tts_buffer.strip():
        yield f"data: {json.dumps({'activity': 'speak'})}\n\n"
        async for _ev_final in _stream_tts_to_sse(_tts_buffer.strip(), _tts_voice):
            if _ev_final.startswith("MEDIA:"):
                _media_final = _ev_final.split(" ", 1)[0].replace("MEDIA:", "")
                yield f"data: {json.dumps({'media': _media_final})}\n\n"
        yield f"data: {json.dumps({'activity': None})}\n\n"

    # Emit final metrics line per SSE protocol: [METRICS]{json}
    _elapsed = max(_time_mod.time() - _start_time, 0.001)
    _token_count = _count_tokens(_full_response_text)
    _tps = round(_token_count / _elapsed, 1)
    _metrics_json = json.dumps({
        'tokens_generated': _token_count,
        'time_ms': int(_elapsed * 1000),
        'tokens_per_second': _tps,
        'tool_calls': _total_tool_calls,
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
    # Generate session title from first message if it's still "New Session"
    asyncio.create_task(_generate_session_title(session_id))
    reply_done = asyncio.Event()
    _session_inflight[session_id] = reply_done

    async def event_stream():
        full_response = ""
        # Parse voice_mode from form field (string "true" or "false")
        voice_mode_bool = voice_mode and voice_mode.lower() == "true"
        try:
            async for event_data in _stream_response(messages, voice_mode=voice_mode_bool):
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
                if memory_fact:
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
    asyncio.create_task(_generate_session_title(session_id))

    async def event_stream():
        full_response = ""
        try:
            async for event_data in _stream_response(messages, temperature=0.3, voice_mode=voice_mode_bool):
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
                if memory_fact:
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
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": "OK"}],
        "max_tokens": 1,
        "tools": [],
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.post(
                f"{LLAMACPP_API_URL}/chat/completions",
                json=payload,
            )
            if resp.status_code == 200:
                data = resp.json()
                usage = data.get("usage", {})
                return {
                    "status": "healthy",
                    "llama_server": "online",
                    "model": data.get("model", MODEL_NAME),
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
            resp = await client.get(f"{LLAMACPP_API_URL}/models")
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
                        "base_url": LLAMACPP_API_URL,
                    }
    except Exception as e:
        logger.warning(f"Failed to get model metrics: {e}")
    return {"error": "Could not retrieve model metrics", "llama_server": LLAMACPP_API_URL}


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
