"""code_search: grep for source code. Uses ripgrep (`rg`) when it is installed, otherwise a pure-Python walk, so it
works on a bare Windows PC. Both paths give the same output and both skip dependency/build folders and anything the
safety config blocks (secrets)."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import _code

MAX_MATCHES = 50
MAX_LINE = 220
MAX_CONTEXT = 5
TIME_LIMIT = 20.0
OUT_CHARS = 8000

SCHEMA = {
    "type": "function",
    "function": {
        "name": "code_search",
        "description": ("Search source code for text or a regex, like grep, and get file:line matches. "
                        "Use it to find where something is defined or used instead of reading whole files."),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to find (or a regex when regex=true)."},
                "path": {"type": "string", "description": "Folder to search (default: the Lucy Core folder)."},
                "glob": {"type": "string", "description": "Only these files, e.g. '*.py' or 'src/**/*.ts'."},
                "regex": {"type": "boolean", "description": "Treat query as a regular expression (default false)."},
                "case_sensitive": {"type": "boolean", "description": "Match upper/lower case exactly (default false)."},
                "context": {"type": "integer", "description": f"Lines to show around each match (0-{MAX_CONTEXT}, default 0)."},
                "files_only": {"type": "boolean", "description": "Only list the files that match, with counts."},
            },
            "required": ["query"],
        },
    },
}
TIER = "read"
LABEL = "search"

Hit = Tuple[int, str, bool]          # (line number, text, is_match)


def find_rg() -> Optional[List[str]]:
    """argv prefix that runs ripgrep, or None. A function so tests can swap it."""
    exe = shutil.which("rg")
    return [exe] if exe else None


def _flag(args: dict, key: str, default: bool = False) -> bool:
    v = args.get(key, default)
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "y")
    return bool(v)


def _clip(line: str) -> str:
    line = line.rstrip("\r\n")
    return line if len(line) <= MAX_LINE else line[:MAX_LINE] + " ..."


# ------------------------------------------------------------------------------------------------ pure Python
def _python_search(root: Path, rx: "re.Pattern[str]", file_rx, context: int, limit: int,
                   ) -> Tuple[Dict[str, List[Hit]], Dict[str, int], int, int, bool]:
    """-> (hits by file, match count by file, matches, files searched, cut short)."""
    blocked = _code.blocked_checker()
    deadline = time.monotonic() + TIME_LIMIT
    hits: Dict[str, List[Hit]] = {}
    counts: Dict[str, int] = {}
    total = searched = 0
    cut = False
    for path in _code.walk_files(root, blocked=blocked, deadline=deadline):
        rel = _code.rel_posix(path, root)
        if file_rx is not None and not _code.matches_glob(file_rx, rel):
            continue
        text = _code.read_text(path)
        if text is None:
            continue
        searched += 1
        lines = text.split("\n")
        found = [i for i, ln in enumerate(lines) if rx.search(ln)]
        if not found:
            continue
        counts[rel] = len(found)
        keep: Dict[int, bool] = {}
        for i in found:
            if total >= limit:
                cut = True
                break
            total += 1
            for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                keep[j] = keep.get(j, False) or j == i
        hits[rel] = [(j + 1, _clip(lines[j]), m) for j, m in sorted(keep.items())]
        if total >= limit:
            cut = True
            break
    if time.monotonic() > deadline:
        cut = True
    return hits, counts, total, searched, cut


# ------------------------------------------------------------------------------------------------ ripgrep
def _rg_search(rg: List[str], root: Path, query: str, *, regex: bool, case_sensitive: bool, glob: str,
               context: int, limit: int):
    """Run ripgrep with --json. -> same tuple as _python_search, or None when rg could not be used."""
    argv = list(rg) + ["--json", "--no-config", "--hidden", "--max-filesize", "1M", "--no-messages"]
    argv += [] if regex else ["-F"]
    argv += [] if case_sensitive else ["-i"]
    if context:
        argv += ["-C", str(context)]
    for d in sorted(_code.SKIP_DIRS):
        argv += ["-g", f"!{d}/"]
    if glob:
        argv += ["-g", glob.replace("\\", "/")]
    argv += ["-e", query, "--", str(root)]
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=TIME_LIMIT, stdin=subprocess.DEVNULL, **kwargs)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in (0, 1):                        # 2 = bad regex / bad glob: let the Python path explain
        return None
    blocked = _code.blocked_checker()
    hits: Dict[str, List[Hit]] = {}
    counts: Dict[str, int] = {}
    files = set()
    total = 0
    cut = False
    for raw in proc.stdout.decode("utf-8", "replace").splitlines():
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        kind, data = ev.get("type"), ev.get("data") or {}
        if kind not in ("match", "context"):
            continue
        ptxt = (data.get("path") or {}).get("text")
        if not ptxt:
            continue
        p = Path(ptxt)
        if blocked(p):
            continue
        rel = _code.rel_posix(p, root)
        text = (data.get("lines") or {}).get("text")
        if text is None:
            continue
        files.add(rel)
        is_match = kind == "match"
        if is_match:
            if total >= limit:
                cut = True
                break
            total += 1
            counts[rel] = counts.get(rel, 0) + 1
        hits.setdefault(rel, []).append((int(data.get("line_number") or 0), _clip(text), is_match))
    return hits, counts, total, len(files), cut


# ------------------------------------------------------------------------------------------------ output
def _render(root: Path, query: str, hits, counts, total, searched, cut, *, files_only: bool, glob: str,
            regex: bool, engine: str) -> str:
    where = root.as_posix()
    if not total:
        extra = f", files matching {glob}" if glob else ""
        return f"No matches for {query!r} in {where} (searched {searched} files{extra}). Try a shorter query, regex=true, or another folder."
    head = f"{total} match(es) in {len(counts)} file(s) for {query!r} in {where}" + (" [regex]" if regex else "")
    if files_only:
        body = "\n".join(f"{rel} ({n})" for rel, n in counts.items())
    else:
        out: List[str] = []
        for rel, rows in hits.items():
            if rel not in counts:
                continue
            out.append(f"== {rel} ({counts[rel]})")
            prev = None
            for no, text, is_match in rows:
                if prev is not None and no > prev + 1:
                    out.append("   ...")
                out.append(f"{no}{':' if is_match else '-'} {text}")
                prev = no
        body = "\n".join(out)
    if len(body) > OUT_CHARS:
        body = body[:OUT_CHARS].rsplit("\n", 1)[0] + "\n[... output cut: narrow it with glob, path or files_only ...]"
    note = ("\n[showing only the first matches or a partial search: narrow it with path or glob]" if cut else "")
    return f"{head}\n{body}{note}"


def _search(args: dict) -> str:
    query = str(args.get("query") or "")
    if not query.strip():
        return "Error: 'query' is required: the text (or regex) to look for."
    root, err = _code.resolve_dir(args.get("path"))
    if root is None:
        return err
    regex, case = _flag(args, "regex"), _flag(args, "case_sensitive")
    files_only = _flag(args, "files_only")
    try:
        context = max(0, min(int(args.get("context") or 0), MAX_CONTEXT))
    except (TypeError, ValueError):
        context = 0
    glob = str(args.get("glob") or "").strip()
    try:
        rx = re.compile(query if regex else re.escape(query), 0 if case else re.IGNORECASE)
    except re.error as e:
        return f"Error: invalid regex {query!r}: {e}. Escape special characters or set regex=false to search plain text."
    file_rx = _code.glob_to_regex(glob) if glob else None
    result = None
    engine = "python"
    rg = find_rg()
    if rg:
        result = _rg_search(rg, root, query, regex=regex, case_sensitive=case, glob=glob, context=context, limit=MAX_MATCHES)
        engine = "ripgrep"
    if result is None:
        result = _python_search(root, rx, file_rx, context, MAX_MATCHES)
        engine = "python"
    hits, counts, total, searched, cut = result
    return _render(root, query, hits, counts, total, searched, cut, files_only=files_only, glob=glob, regex=regex,
                   engine=engine)


async def handler(args, ctx):
    return await asyncio.to_thread(_search, args or {})
