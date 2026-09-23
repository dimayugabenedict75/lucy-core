from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse
from pydantic import BaseModel
from typing import List, Dict, Optional, Any
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
sys.path.insert(0, r"C:/Users/dimay/Lucy/Lucy_Core/src")

from lucy.memory.manager import MemoryManager
from lucy.skills.memory.hook import detect_memory_candidate, render_confirmation
from lucy.skills.skills_manager import SkillsManager
import base64

# Persona is loaded from file
PERSONA_PATH = r"C:/Users/dimay/Lucy/Lucy_Core/.core/personas/lucy.md"

app = FastAPI(title="Lucy Core API")

logger = logging.getLogger("lucy.core.api")
logging.basicConfig(level=logging.INFO)


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
SAFE_ROOT = Path("C:/Users/dimay").resolve()

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

import sqlite3
import threading

# --- Session Storage (SQLite persisted) ---
_DB_PATH = "C:/Users/dimay/Lucy/Lucy_Core/runtime/sessions.sqlite"
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
_conn.commit()


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


def _generate_session_title(session_id: str):
    """Generate a session title from the first user message if title is 'New Session'."""
    with _db_lock:
        row = _conn.execute(
            "SELECT title FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row and row["title"] == "New Session":
            first_msg = _conn.execute(
                "SELECT content FROM messages WHERE session_id = ? AND role = 'user' ORDER BY rowid ASC LIMIT 1",
                (session_id,),
            ).fetchone()
            if first_msg:
                content = first_msg["content"]
                # Generate a concise title from the first message
                title = content.strip()[:60]
                if len(content.strip()) > 60:
                    title = content.strip()[:57] + "..."
                _conn.execute(
                    "UPDATE sessions SET title = ? WHERE session_id = ?",
                    (title, session_id),
                )
                _conn.commit()
                return title
    return row["title"] if row else "New Session"


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
            "description": "Read a file from the local filesystem. Only safe paths under C:/Users/dimay are allowed.",
            "parameters": {
                "type": "json",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file to read (e.g. C:/Users/dimay/Desktop/Hello.txt)"
                    }
                },
                "required": ["file_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_local_file",
            "description": "Write content to a file on the local filesystem. Overwrites if the file exists. Only safe paths under C:/Users/dimay are allowed. Parent directories are created automatically. After writing, you MUST call send_file with the same file_path to deliver it to the user as a downloadable attachment.",
            "parameters": {
                "type": "json",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file to write (e.g. C:/Users/dimay/Desktop/Hello.txt)"
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
            "description": "List files in a local directory. Only safe paths under C:/Users/dimay are allowed.",
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
            "description": "Find a file by name within a specific directory and subdirectories. Only safe paths under C:/Users/dimay are allowed.",
            "parameters": {
                "type": "json",
                "properties": {
                    "file_name": {
                        "type": "string",
                        "description": "Name of the file to search for"
                    },
                    "dir_path": {
                        "type": "string",
                        "description": "Root directory to search in (default: C:/Users/dimay)"
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
            "description": "Signal to the client that a file is ready for the user to view or download. The file must already exist on disk under C:/Users/dimay.",
            "parameters": {
                "type": "json",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file to send (e.g. C:/Users/dimay/data/uploads/screenshot.png)"
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
                        "description": "Path to the uploaded image file on disk (e.g. C:/Users/dimay/Lucy/Lucy_Core/runtime/tmp/screenshot.png)"
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
    """
    tool_names = set()
    for skill in skill_list:
        tools = skill.get("tools", [])
        if isinstance(tools, list):
            for t in tools:
                if isinstance(t, str) and t in TOOL_REGISTRY:
                    tool_names.add(t)
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

    elif tool_name == "write_local_file":
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
        dir_path = args.get("dir_path", "C:/Users/dimay")
        target = _resolve_path(dir_path)
        if target is None:
            return f"Error: {dir_path} is outside the allowed root."
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
        result = f"MEDIA:{target}"
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

    else:
        return f"Unknown tool: {tool_name}"


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
    """Send one request to llama-server. Returns either a streaming response or full JSON."""
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

    async with httpx.AsyncClient(timeout=60.0) as client:
        if stream:
            resp = await client.stream("POST", f"{LLAMACPP_API_URL}/chat/completions", json=payload)
            return resp
        else:
            resp = await client.post(f"{LLAMACPP_API_URL}/chat/completions", json=payload)
            return resp


async def _collect_full_response(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Non-streaming request that returns the full JSON response."""
    resp = await _generate_once(messages, stream=False)
    if resp.status_code != 200:
        return {"error": "Failed to connect to brain", "status": resp.status_code}
    return resp.json()


# --- Activity Indicator Helpers ---
# Maps tool names to human-readable activity indicator labels
_TOOL_ACTIVITY_MAP = {
    "web_search": "search",
    "web_extract": "read",
    "list_local_files": "list",
    "find_file": "search",
    "run_shell_command": "run",
    "send_file": "send",
    "analyze_image": "analyze",
}


def _tool_to_activity(tool_name: str, args: dict) -> str:
    """Map a tool name to an activity indicator label for the frontend."""
    if tool_name in _TOOL_ACTIVITY_MAP:
        return _TOOL_ACTIVITY_MAP[tool_name]
    # For write/edit tools, try to show the file path
    if tool_name == "write_local_file" and args.get("file_path"):
        return f"edit:{args['file_path']}"
    if tool_name == "read_local_file" and args.get("file_path"):
        return f"read:{args['file_path']}"
    if tool_name == "analyze_image":
        return "analyzing image"
    return tool_name  # fallback: use tool name as-is


async def _stream_response(messages: List[Dict[str, Any]], temperature: float = DEFAULT_TEMPERATURE, skill_list: list[dict] = None):
    """Stream a response from llama-server, handling tool calls in a loop.

    Emits SSE lines: {content}, {activity}, {media}, {done}, and finally [METRICS]{json}.
    """
    import time as _time_mod
    _start_time = _time_mod.time()
    _total_tool_calls = 0
    _full_response_text = ""
    round_num = 0

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
                "temperature": DEFAULT_TEMPERATURE,
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
                                    yield f"data: {json.dumps({'content': content})}\n\n"

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
            "content": "You have used all your tool calls. Synthesize your response from the information gathered so far. Do not call any more tools.",
        })
        payload = {
            "model": MODEL_NAME,
            "messages": force_messages,
            "temperature": DEFAULT_TEMPERATURE,
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
                                        yield f"data: {json.dumps({'content': content})}\n\n"
                            except json.JSONDecodeError:
                                continue

    # Emit final metrics line per SSE protocol: [METRICS]{json}
    _elapsed = max(_time_mod.time() - _start_time, 0.001)
    import re as _re
    _token_count = len(_re.findall(r'\w+', _full_response_text))
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
    files: Optional[List[UploadFile]] = File(None),
    _: bool = Depends(verify_api_key),
):
    """Main chat endpoint — streams responses with tool-call support.

    Accepts multipart/form-data for file uploads, or JSON for plain text.
    """
    # Try JSON first (backward compat with plain text requests)
    if not files and not message:
        try:
            body = await request.json()
            message = body.get("message", "")
            session_id = body.get("session_id", "default")
            stream = body.get("stream", True)
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
        except Exception:
            pass

    if not message:
        raise HTTPException(status_code=400, detail="No message provided")

    # Handle uploaded files — save them to a temp upload dir
    uploaded_file_paths = []
    image_paths = []
    if files:
        upload_dir = Path("C:/Users/dimay/Lucy/Lucy_Core/runtime/tmp")
        upload_dir.mkdir(parents=True, exist_ok=True)
        for f in files:
            safe_name = f.filename.replace("../", "").replace("..\\", "")
            fpath = upload_dir / safe_name
            content = await f.read()
            fpath.write_bytes(content)
            uploaded_file_paths.append(str(fpath))
            # If this is an image, pass it for multimodal embedding
            if str(fpath).lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp')):
                image_paths.append(str(fpath))
            await f.seek(0)

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
    _generate_session_title(session_id)
    async def event_stream():
        full_response = ""
        try:
            async for event_data in _stream_response(messages):
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
            # Client disconnected — must re-raise; CancelledError is BaseException on 3.8+
            # and will NOT be caught by except Exception, causing uvicorn worker crash.
            logger.info("Client disconnected (CancelledError in event_stream)")
            raise
        except Exception as e:
            logger.error(f"Streaming error: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    if stream:
        return StreamingResponse(event_stream(), media_type="text/event-stream")
    # Non-streaming: collect the full response
    full_response = ""
    async for event in event_stream():
        stripped = event.replace("data: ", "").strip()
        try:
            parsed = json.loads(stripped)
            if "content" in parsed:
                full_response += parsed["content"]
        except json.JSONDecodeError:
            continue
    return {"response": full_response, "session_id": session_id}


@app.post("/api/cli")
async def cli_chat_endpoint(
    request: Request,
    message: str = Form(None),
    session_id: str = Form("cli-dev"),
    stream: bool = Form(True),
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
        except Exception:
            pass

    if not message:
        raise HTTPException(status_code=400, detail="No message provided")

    session_id = f"cli-{session_id}" if not session_id.startswith("cli-") else session_id

    # Build messages (CLI mode — same as chat but with coding tone)
    messages = _build_messages(session_id, message)

    # Store user message in CLI session
    _append_message(session_id, "user", message)
    memory_fact = detect_memory_candidate(message)
    _generate_session_title(session_id)

    async def event_stream():
        full_response = ""
        try:
            async for event_data in _stream_response(messages, temperature=0.3):
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
    async for event in event_stream():
        stripped = event.replace("data: ", "").strip()
        try:
            parsed = json.loads(stripped)
            if "content" in parsed:
                full_response += parsed["content"]
        except json.JSONDecodeError:
            continue
    return {"response": full_response, "session_id": session_id}


@app.get("/")
async def root(request: Request):
    """Serve the chat UI with API key injected into the DOM."""
    ui_path = "C:/Users/dimay/Lucy/Lucy_Core/ui/chat.html"
    html = Path(ui_path).read_text(encoding="utf-8")
    # Inject the API key into the DOM so browser fetch() can read it
    # The placeholder __API_KEY_PLACEHOLDER__ is replaced with the actual key
    html = html.replace("__API_KEY_PLACEHOLDER__", DEV_API_KEY)
    return HTMLResponse(content=html)


@app.get("/chat.css")
async def serve_css():
    """Serve the external stylesheet for the chat UI."""
    css_path = "C:/Users/dimay/Lucy/Lucy_Core/ui/chat.css"
    css = Path(css_path).read_text(encoding="utf-8")
    return HTMLResponse(content=css, media_type="text/css")


@app.get("/api/file/{path:path}")
async def serve_file(path: str, _: bool = Depends(verify_api_key)):
    """Serve a file from the safe root for the frontend to download/view."""
    file_path = Path(f"C:/Users/dimay/{path}").resolve()
    try:
        file_path.relative_to(SAFE_ROOT)
    except ValueError:
        raise HTTPException(status_code=403, detail="Path outside allowed root")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)


@app.get("/api/skills")
async def list_skills(_: bool = Depends(verify_api_key)):
    """Lists all available skills for the frontend."""
    return {"skills": _skills_manager.list_skills()}


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


@app.get("/api/settings/timezone")
async def get_timezone_setting(_ = Depends(verify_api_key)):
    """Returns the current timezone configuration."""
    row = _conn.execute("SELECT value FROM tz_config WHERE key = 'timezone'").fetchone()
    tz = row["value"] if row else "system"
    return {"timezone": tz, "available_timezones": [
        {"value": "system", "label": "System Default"},
        {"value": "utc", "label": "UTC"},
        {"value": "+08:00", "label": "GMT+8 (Beijing, Singapore)"},
        {"value": "+00:00", "label": "GMT+0 (London)"},
        {"value": "-05:00", "label": "GMT-5 (New York)"},
        {"value": "-08:00", "label": "GMT-8 (Los Angeles)"},
    ]}


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
        ], env={**os.environ, "PYTHONPATH": "C:/Users/dimay/Lucy/Lucy_Core/src"},
        cwd="C:/Users/dimay/Lucy/Lucy_Core")
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
