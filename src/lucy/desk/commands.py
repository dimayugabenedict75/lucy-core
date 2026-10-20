"""Item 3 - slash commands and @file mentions.

The command list lives here (one source of truth); the UI pops it up when the input starts with "/" and handles the
`action` of the chosen command. Unknown "/something" is NOT a command (so "/etc/hosts" or "/usr/bin" stay normal text).
@file mentions are resolved by the harness: only files/folders inside the allowed roots are named to the model, and the
model still opens them with read_local_file (so the safety layer keeps judging every read). Nothing is inlined here.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

COMMANDS = [
    {"name": "plan", "args": "<task>", "action": "plan", "help": "Show me a plan first and wait for my OK"},
    {"name": "research", "args": "<topic>", "action": "research", "help": "Deep research: search, read, write a report file"},
    {"name": "regen", "args": "", "action": "regenerate", "help": "Answer my last message again"},
    {"name": "branch", "args": "", "action": "branch", "help": "Continue from the last reply in a new chat"},
    {"name": "export", "args": "[md|json]", "action": "export", "help": "Download this chat"},
    {"name": "new", "args": "", "action": "new", "help": "Start a new chat"},
    {"name": "remember", "args": "<fact>", "action": "text", "template": "Please remember this: {args}",
     "help": "Save something to memory"},
    {"name": "rewind", "args": "", "action": "rewind", "help": "Go back to an earlier point (chat and files)"},
    {"name": "help", "args": "", "action": "help", "help": "List the commands"},
]
_BY_NAME = {c["name"]: c for c in COMMANDS}
_CMD_RE = re.compile(r"^/([A-Za-z]+)(?:[ \t]+(.*))?$", re.DOTALL)


def match(prefix: str) -> list[dict]:
    """Commands whose name starts with `prefix` (without the slash)."""
    p = (prefix or "").lstrip("/").lower()
    return [c for c in COMMANDS if c["name"].startswith(p)]


def parse(text: str) -> tuple[dict, str] | None:
    """('/plan fix the login', ...) -> (command, 'fix the login'); None when it is not a known command."""
    m = _CMD_RE.match((text or "").strip())
    if not m:
        return None
    cmd = _BY_NAME.get(m.group(1).lower())
    return (cmd, (m.group(2) or "").strip()) if cmd else None


# ------------------------------------------------------------------------------------------------ @file mentions
MENTION_RE = re.compile(r'(?<![\w.@/\\])@(?:"([^"\n]+)"|([^\s"\'<>|?*,;()\[\]{}]+))')
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "runtime", "cache", ".idea", ".vscode", "dist", "build",
             ".pytest_cache", ".mypy_cache", "$RECYCLE.BIN", "System Volume Information"}
MAX_VISIT = 6000
MAX_MENTIONS = 6


def find_mentions(text: str) -> list[str]:
    out = []
    for m in MENTION_RE.finditer(text or ""):
        tok = (m.group(1) or m.group(2) or "").rstrip(".:!")
        if tok and tok not in out:
            out.append(tok)
    return out[:MAX_MENTIONS]


def _inside(p: Path, roots: list[Path]) -> bool:
    try:
        rp = p.resolve()
    except OSError:
        return False
    for r in roots:
        try:
            rp.relative_to(r.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def _walk(roots: list[Path], max_visit: int = MAX_VISIT):
    seen = 0
    for root in roots:
        if not root.is_dir():
            continue
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                entries = sorted(os.scandir(d), key=lambda e: e.name.lower())
            except OSError:
                continue
            for e in entries:
                seen += 1
                if seen > max_visit:
                    return
                if e.name in SKIP_DIRS or (e.name.startswith(".") and e.name not in (".core",)):
                    continue
                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                yield root, Path(e.path), is_dir
                if is_dir:
                    stack.append(Path(e.path))


def find_files(query: str, roots: list[Path], limit: int = 12) -> list[dict]:
    """Autocomplete for @: files and folders under the roots whose name/path matches `query` (case-insensitive)."""
    roots = [Path(r) for r in roots if r]
    q = (query or "").strip().replace("\\", "/").lower()
    scored = []
    for root, p, is_dir in _walk(roots):
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            continue
        name = p.name.lower()
        if not q:
            score = 0 if "/" not in rel else None
        elif name.startswith(q):
            score = 0
        elif q in name:
            score = 1
        elif q in rel.lower():
            score = 2
        else:
            score = None
        if score is None:
            continue
        scored.append((score, 0 if is_dir else 1, len(rel), rel, p, is_dir))
    scored.sort(key=lambda t: t[:4])
    out = []
    for _s, _d, _l, rel, p, is_dir in scored[:max(1, min(limit, 30))]:
        try:
            size = None if is_dir else p.stat().st_size
        except OSError:
            size = None
        out.append({"name": p.name, "rel": rel, "path": p.as_posix(), "dir": is_dir, "size": size})
    return out


def resolve(token: str, roots: list[Path]) -> tuple[Path | None, str]:
    """Token -> (path, '') or (None, reason). Only paths inside the roots are returned."""
    roots = [Path(r) for r in roots if r]
    tok = token.strip().strip('"').replace("\\", "/")
    cand = Path(tok).expanduser()
    if cand.is_absolute() or re.match(r"^[A-Za-z]:/", tok):
        if not cand.exists():
            return None, "not found"
        return (cand, "") if _inside(cand, roots) else (None, "outside the folders Lucy may use")
    for r in roots:
        p = r / tok
        if p.exists() and _inside(p, roots):
            return p, ""
    hits = [p for _r, p, _d in _walk(roots) if p.name.lower() == tok.lower() or p.as_posix().lower().endswith("/" + tok.lower())]
    if len(hits) == 1:
        return hits[0], ""
    if len(hits) > 1:
        return None, "ambiguous: " + ", ".join(h.as_posix() for h in hits[:3])
    return None, "not found"


def expand(text: str, roots: list[Path]) -> str:
    """The system note that names the mentioned files ('' when the text has no mentions)."""
    toks = find_mentions(text)
    if not toks:
        return ""
    lines = []
    for t in toks:
        p, why = resolve(t, roots)
        if p is None:
            lines.append(f"- @{t}: {why}")
        elif p.is_dir():
            lines.append(f"- @{t} = {p.as_posix()} (folder; list it with list_directory / find tools)")
        else:
            try:
                kb = max(1, p.stat().st_size // 1024)
            except OSError:
                kb = 0
            lines.append(f"- @{t} = {p.as_posix()} (file, {kb} KB; open it with read_local_file)")
    return ("The user mentioned these files with @. Use the paths given here; do not guess other paths:\n" + "\n".join(lines))
