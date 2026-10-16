"""Local file tools (read/edit/write/list/find/send). Moved out of api.py with the code kept as-is:
only the helper names changed (_resolve_path -> resolve_path, SAFE_ROOT -> _paths.SAFE_ROOT)."""
import asyncio
import fnmatch
import logging
import os
import time
from pathlib import Path

from lucy import paths as _paths
from lucy.server import file_guard
from ._common import outside_root, resolve_path

logger = logging.getLogger("lucy.core.api")      # same logger name as before: log lines look the same
_HOME = _paths.SAFE_ROOT.as_posix()              # shown to the model in tool descriptions

_SCHEMA_READ_LOCAL_FILE = {
    "type": "function",
    "function": {
        "name": "read_local_file",
        "description": (f"Read a file from the local filesystem (paths under {_HOME}; relative paths resolve from the "
                        "Lucy Core folder). Small files are returned whole. For a LARGE file (like api.py) use "
                        "search='text' to find the lines you need, then start_line/end_line to read just that part."),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": f"Path to the file (e.g. {_HOME}/Desktop/Hello.txt or src/lucy/server/api.py)"
                },
                "start_line": {"type": "integer", "description": "First line to return (1-based)."},
                "end_line": {"type": "integer", "description": "Last line to return (default: 200 lines after start_line)."},
                "search": {"type": "string", "description": "Return only the lines containing this text (case-insensitive), with line numbers."}
            },
            "required": ["file_path"]
        }
    }
}

async def _read_local_file(args, ctx):
    file_path = args.get("file_path", "")
    target = resolve_path(file_path)
    if target is None:
        return outside_root(file_path)
    if not target.exists():
        return await asyncio.to_thread(file_guard.not_found_message, file_path, _paths.ROOT)
    if not target.is_file():
        return f"Not a file: {file_path} (it is a folder: use list_local_files to see what is inside)"
    try:
        try:
            text, _crlf = file_guard.read_text_keep_style(target)
        except ValueError:
            text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.split("\n")
        total = len(lines)
        search = str(args.get("search") or "").strip()
        start, end = args.get("start_line"), args.get("end_line")
        if search:
            hits = [i for i, l in enumerate(lines) if search.lower() in l.lower()]
            if not hits:
                return f"No lines containing '{search}' in {target.as_posix()} ({total} lines)."
            shown = "\n".join(f"{i + 1}: {lines[i]}" for i in hits[:30])
            return (f"[{len(hits)} line(s) containing '{search}' in {target.as_posix()} ({total} lines)"
                    f"{'; showing the first 30' if len(hits) > 30 else ''}. Use start_line/end_line to read around them]\n{shown}")
        if start is not None or end is not None:
            s0 = max(1, int(start or 1))
            e0 = min(total, int(end) if end is not None else s0 + 199)
            body = "\n".join(lines[s0 - 1:e0])
            if len(body) > 11000:
                body = body[:11000]
                e0 = s0 + body.count("\n")
                return f"[{target.as_posix()} lines {s0}-{e0} of {total}; cut to fit - ask for fewer lines]\n{body}"
            return f"[{target.as_posix()} lines {s0}-{e0} of {total}]\n{body}"
        if len(text) > 10_000:
            head = text[:10_000]
            head = head[:head.rfind("\n")] if "\n" in head else head
            return (f"{head}\n\n[... showing the first {head.count(chr(10)) + 1} of {total} lines of "
                    f"{target.as_posix()}. It is large: use search='text' to find lines, then "
                    f"start_line/end_line to read a range ...]")
        return text
    except Exception as e:
        return f"[error reading file: {e}]"


_SCHEMA_EDIT_LOCAL_FILE = {
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
}

async def _edit_local_file(args, ctx):
    file_path = args.get("file_path", "")
    replace_all = args.get("replace_all", False)
    if isinstance(replace_all, str):                      # llama-server may emit "true"/"false" strings
        replace_all = replace_all.lower() == "true"
    target = resolve_path(file_path)
    if target is None:
        return outside_root(file_path)
    try:
        result = await asyncio.to_thread(file_guard.edit_file, target, str(args.get("old_string", "")),
                                         str(args.get("new_string", "")), bool(replace_all), _paths.ROOT)
    except Exception as e:                                # noqa: BLE001
        logger.exception("edit_local_file crashed")
        return f"[error editing file: {e}]"
    if result.startswith("File not found"):
        result = await asyncio.to_thread(file_guard.not_found_message, file_path, _paths.ROOT)
    if result.startswith(("Error", "Refused", "File not found")):
        logger.warning(f"edit_local_file {target}: {result[:200]}")
    else:
        logger.info(f"edit_local_file {target}: {result[:160]}")
    return result


_SCHEMA_WRITE_LOCAL_FILE = {
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
}

async def _write_local_file(args, ctx):
    file_path = args.get("file_path", "")
    target = resolve_path(file_path)
    if target is None:
        return outside_root(file_path)
    try:
        result = await asyncio.to_thread(file_guard.write_file, target, str(args.get("content", "")), _paths.ROOT)
    except Exception as e:                                # noqa: BLE001
        logger.exception("write_local_file crashed")
        return f"[error writing file: {e}]"
    (logger.warning if result.startswith(("Error", "Refused")) else logger.info)(f"write_local_file {target}: {result[:160]}")
    return result


_SCHEMA_LIST_LOCAL_FILES = {
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
}

async def _list_local_files(args, ctx):
    dir_path = args.get("dir_path", "")
    pattern = args.get("pattern", "*")
    target = resolve_path(dir_path)
    if target is None:
        return outside_root(dir_path)
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


_SCHEMA_FIND_FILE = {
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
}

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "$recycle.bin", "system volume information"}


def _find_matches(root, pattern, max_depth=4, limit=20, time_limit=10.0):
    """Pure-Python replacement for `find DIR -name PATTERN -maxdepth 4 | head -20` (that needed bash, which Windows lacks).
    Case-insensitive glob on the name; files and folders; skips junk folders. -> (paths, stopped_early)"""
    pat = pattern.lower()
    base_depth = len(root.parts)
    found, deadline = [], time.monotonic() + time_limit
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).parts) - base_depth
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in _SKIP_DIRS) if depth < max_depth else []
        for name in sorted(dirnames + filenames if depth < max_depth else filenames):
            if depth + 1 > max_depth:
                continue
            if fnmatch.fnmatchcase(name.lower(), pat):
                found.append((Path(dirpath) / name).as_posix())
                if len(found) >= limit:
                    return found, True
        if time.monotonic() > deadline:
            return found, True
    return found, False


async def _find_file(args, ctx):
    file_name = args.get("file_name", "")
    dir_path = args.get("dir_path", str(_paths.SAFE_ROOT))
    target = resolve_path(dir_path)
    if target is None:
        return outside_root(dir_path)
    if not file_name or any(c in file_name for c in ';|&$`"\'/\\'):
        return "Error: file_name contains invalid characters"
    if not target.is_dir():
        return f"Directory not found: {dir_path}"
    try:
        found, stopped = await asyncio.to_thread(_find_matches, target, file_name)
    except Exception as e:
        return f"[error: {e}]"
    if found:
        return "\n".join(found) + ("\n[showing the first matches only: use a more specific name or folder]" if stopped else "")
    if stopped:
        return f"[find timed out after 10s in {dir_path}]"
    return f"No files matching '{file_name}' found in {dir_path}"


_SCHEMA_SEND_FILE = {
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
}

async def _send_file(args, ctx):
    file_path = args.get("file_path", "")
    caption = args.get("caption")
    target = resolve_path(file_path)
    if target is None:
        return outside_root(file_path)
    if not target.exists():
        return await asyncio.to_thread(file_guard.not_found_message, file_path, _paths.ROOT)
    if target.is_dir():
        # A folder can't be attached: zip it and send the zip instead of failing.
        try:
            import shutil as _sh
            zip_base = _paths.RUNTIME / "tmp" / f"{target.name or 'folder'}"
            zip_base.parent.mkdir(parents=True, exist_ok=True)
            made = await asyncio.to_thread(_sh.make_archive, str(zip_base), "zip", str(target))
            if Path(made).stat().st_size > 200 * 1024 * 1024:
                return f"Error: {file_path} is a folder and its zip is over 200 MB. Send smaller parts."
            target = Path(made)
        except Exception as e:                                # noqa: BLE001
            return f"Error: {file_path} is a folder and zipping it failed: {e}"
    elif not target.is_file():
        return f"File not found: {file_path}"
    # Return MEDIA: marker so Hermes/Discord sends it as an attachment
    # Use forward slashes in the path for cross-platform frontend compatibility
    # Calculate path relative to _paths.SAFE_ROOT
    try:
        relative_path = target.relative_to(_paths.SAFE_ROOT)
        media_path_str = relative_path.as_posix()
    except ValueError:
        # Fallback: use the absolute path if relative fails
        media_path_str = target.as_posix()
        
    # Ensure we have a clean string for the MEDIA marker
    result = f"MEDIA:{media_path_str}"
    if caption:
        result += f" {caption}"
    return result


TOOLS = [
    {"schema": _SCHEMA_READ_LOCAL_FILE, "handler": _read_local_file, "tier": "read", "label": "read"},
    {"schema": _SCHEMA_EDIT_LOCAL_FILE, "handler": _edit_local_file, "tier": "write", "label": "edit"},
    {"schema": _SCHEMA_WRITE_LOCAL_FILE, "handler": _write_local_file, "tier": "write", "label": "write"},
    {"schema": _SCHEMA_LIST_LOCAL_FILES, "handler": _list_local_files, "tier": "read", "label": "list"},
    {"schema": _SCHEMA_FIND_FILE, "handler": _find_file, "tier": "read", "label": "search"},
    {"schema": _SCHEMA_SEND_FILE, "handler": _send_file, "tier": "read", "label": "send"},
]
