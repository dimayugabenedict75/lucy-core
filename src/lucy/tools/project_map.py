"""project_map: a compact outline of a code project (folders, files, classes/functions with line numbers) so a small
model can orient itself without reading whole files. Symbol outlines are cached per file (mtime + size), so a second
call on a big project only re-reads what changed."""
from __future__ import annotations

import ast
import asyncio
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import _code

BUDGET = 6000                 # characters of map for a whole project
FOCUS_BUDGET = 9000           # ... when zoomed in on a folder or file
MAX_FILES = 5000
TIME_LIMIT = 20.0
MAX_SYMBOLS_PER_FILE = 150

SCHEMA = {
    "type": "function",
    "function": {
        "name": "project_map",
        "description": ("Show a compact map of a code project: folders and files with the classes and functions in each "
                        "(with line numbers). Use it first on an unfamiliar project, then read only the parts you need."),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Project folder (default: the Lucy Core folder)."},
                "focus": {"type": "string", "description": "A sub-folder or file inside the project to zoom in on."},
            },
            "required": [],
        },
    },
}
TIER = "read"
LABEL = "read"

Symbol = Tuple[int, int, str]                 # (line, depth 0|1, text such as "class Foo(Base)" or "def run(a, b)")
_LANG = {".py": "Python", ".pyi": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript",
         ".cjs": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".cs": "C#", ".java": "Java", ".kt": "Kotlin",
         ".go": "Go", ".rs": "Rust", ".ps1": "PowerShell", ".sh": "Shell", ".md": "Markdown", ".vue": "Vue",
         ".svelte": "Svelte"}
_SYMBOL_EXTS = set(_LANG)
_LISTED_EXTS = _code.CODE_EXTS - _SYMBOL_EXTS            # shown by name only (json, yaml, css, html, txt ...)

# --------------------------------------------------------------------------------------------- symbol extraction
_JS = [
    (re.compile(r"^(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)"), "class {0}"),
    (re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*\("), "function {0}()"),
    (re.compile(r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|[\w$]+)\s*(?::\s*[^=]+)?=>"),
     "function {0}()"),
    (re.compile(r"^(?:export\s+)?interface\s+(\w+)"), "interface {0}"),
    (re.compile(r"^(?:export\s+)?type\s+(\w+)\s*(?:<[^>]*>)?\s*="), "type {0}"),
    (re.compile(r"^(?:export\s+)?(?:const\s+)?enum\s+(\w+)"), "enum {0}"),
]
_MODS = r"(?:(?:public|private|protected|internal|static|sealed|abstract|partial|readonly|unsafe|virtual|override|async|extern)\s+)"
_CS = [
    (re.compile(r"^\s*" + _MODS + r"*(class|interface|struct|enum|record)\s+(\w+)"), 0, "{0} {1}"),
    (re.compile(r"^\s{4,12}" + _MODS + r"+[\w<>\[\],.?\s]+?\s+(\w+)\s*\("), 1, "{0}()"),
]
_GO = [
    (re.compile(r"^func\s+\(([^)]*)\)\s*(\w+)\s*\("), 1, "func ({0}) {1}()"),
    (re.compile(r"^func\s+(\w+)\s*\("), 0, "func {0}()"),
    (re.compile(r"^type\s+(\w+)\s+(struct|interface)"), 0, "type {0} {1}"),
]
_RS = [
    (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)"), 0, "fn {0}()"),
    (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(struct|enum|trait)\s+(\w+)"), 0, "{0} {1}"),
]
_JVM = [
    (re.compile(r"^\s*" + _MODS + r"*(?:data\s+|open\s+|final\s+)?(class|interface|enum|object)\s+(\w+)"), 0, "{0} {1}"),
    (re.compile(r"^\s*(?:(?:public|private|protected|internal|override|suspend|inline)\s+)*fun\s+(?:<[^>]*>\s*)?(\w+)\s*\("), 1, "fun {0}()"),
]
_SH = [(re.compile(r"^\s*function\s+([\w-]+)"), 0, "function {0}"), (re.compile(r"^([\w-]+)\s*\(\)\s*\{"), 0, "{0}()")]
_MD = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")


def _sig(node: ast.AST) -> str:
    a = node.args                                              # type: ignore[attr-defined]
    parts = [x.arg for x in getattr(a, "posonlyargs", [])] + [x.arg for x in a.args]
    if a.vararg:
        parts.append("*" + a.vararg.arg)
    parts += [x.arg for x in a.kwonlyargs]
    if a.kwarg:
        parts.append("**" + a.kwarg.arg)
    text = ", ".join(parts)
    return text if len(text) <= 48 else text[:45] + "..."


def _py_symbols(text: str) -> List[Symbol]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return _py_symbols_regex(text)
    out: List[Symbol] = []

    def fn(n, depth):
        prefix = "async def" if isinstance(n, ast.AsyncFunctionDef) else "def"
        out.append((n.lineno, depth, f"{prefix} {n.name}({_sig(n)})"))

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in node.bases)[:40]
            out.append((node.lineno, 0, f"class {node.name}" + (f"({bases})" if bases else "")))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn(sub, 1)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn(node, 0)
    return out


_PY_RX = re.compile(r"^(\s*)(async\s+def|def|class)\s+(\w+)")


def _py_symbols_regex(text: str) -> List[Symbol]:
    out: List[Symbol] = []
    for i, line in enumerate(text.split("\n"), 1):
        m = _PY_RX.match(line)
        if m and len(m.group(1)) <= 4:
            out.append((i, 1 if m.group(1) else 0, f"{m.group(2)} {m.group(3)}"))
    return out


def _regex_symbols(lines: List[str], rules) -> List[Symbol]:
    out: List[Symbol] = []
    for i, line in enumerate(lines, 1):
        for rx, depth, fmt in rules:
            m = rx.match(line)
            if m:
                out.append((i, depth, fmt.format(*m.groups())))
                break
    return out


def extract_symbols(path: Path, text: str) -> List[Symbol]:
    ext = path.suffix.lower()
    if ext in (".py", ".pyi"):
        syms = _py_symbols(text)
    elif ext in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte"):
        syms = []
        for i, line in enumerate(text.split("\n"), 1):
            for rx, fmt in _JS:
                m = rx.match(line)
                if m:
                    syms.append((i, 0, fmt.format(*m.groups())))
                    break
    elif ext == ".cs":
        syms = _regex_symbols(text.split("\n"), _CS)
    elif ext == ".go":
        syms = _regex_symbols(text.split("\n"), _GO)
    elif ext == ".rs":
        syms = _regex_symbols(text.split("\n"), _RS)
    elif ext in (".java", ".kt"):
        syms = _regex_symbols(text.split("\n"), _JVM)
    elif ext in (".ps1", ".sh"):
        syms = _regex_symbols(text.split("\n"), _SH)
    elif ext == ".md":
        syms = []
        for i, line in enumerate(text.split("\n"), 1):
            m = _MD.match(line)
            if m and len(m.group(1)) <= 2:
                syms.append((i, len(m.group(1)) - 1, "# " + m.group(2)[:70]))
    else:
        syms = []
    return syms[:MAX_SYMBOLS_PER_FILE]


# --------------------------------------------------------------------------------------------- cache
_CACHE: Dict[str, Dict[str, tuple]] = {}          # root -> {relpath: (mtime_ns, size, lines, symbols)}
_LOCK = threading.Lock()
_MAX_ROOTS = 8


def _outline(path: Path, cache: Dict[str, tuple], rel: str) -> Optional[tuple]:
    try:
        st = path.stat()
    except OSError:
        return None
    hit = cache.get(rel)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit
    if st.st_size > _code.MAX_FILE_BYTES:
        entry = (st.st_mtime_ns, st.st_size, None, [])
    else:
        text = _code.read_text(path)
        if text is None:
            return None
        entry = (st.st_mtime_ns, st.st_size, text.count("\n") + 1, extract_symbols(path, text))
    cache[rel] = entry
    return entry


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


# --------------------------------------------------------------------------------------------- build + render
class _Dir:
    def __init__(self) -> None:
        self.dirs: Dict[str, "_Dir"] = {}
        self.files: List[Tuple[str, Optional[int], List[Symbol]]] = []

    def count(self) -> int:
        return len(self.files) + sum(d.count() for d in self.dirs.values())


def _add(tree: _Dir, rel: str, lines: Optional[int], syms: List[Symbol]) -> None:
    parts = rel.split("/")
    node = tree
    for d in parts[:-1]:
        node = node.dirs.setdefault(d, _Dir())
    node.files.append((parts[-1], lines, syms))


def _file_lines(name: str, lines: Optional[int], syms: List[Symbol], level: int, indent: str) -> List[str]:
    head = f"{indent}{name}" + (f" ({lines} lines)" if lines else "")
    out = [head]
    if level >= 2:
        for line, depth, text in syms:
            if depth == 0 or level >= 3:
                out.append(f"{indent}  L{line} {'  ' * depth}{text}")
    return out


def _render_tree(node: _Dir, level: int, indent: str = "", depth: int = 0) -> List[str]:
    out: List[str] = []
    for name in sorted(node.dirs):
        sub = node.dirs[name]
        label = name
        while not sub.files and len(sub.dirs) == 1:                 # src/lucy/ -> one line
            (n2, sub), = sub.dirs.items()
            label += "/" + n2
        if level == 0 and depth >= 1:
            out.append(f"{indent}{label}/ ({sub.count()} files)")
            continue
        out.append(f"{indent}{label}/" + (f" ({sub.count()} files)" if level == 0 else ""))
        out += _render_tree(sub, level, indent + "  ", depth + 1)
    if level >= 1:
        for name, lines, syms in sorted(node.files):
            out += _file_lines(name, lines, syms, level, indent)
    elif node.files and depth == 0:
        out.append(f"{indent}({len(node.files)} files in this folder)")
    return out


def _languages(files: List[Tuple[str, Optional[int]]]) -> str:
    totals: Dict[str, int] = {}
    for rel, lines in files:
        lang = _LANG.get(Path(rel).suffix.lower())
        if lang and lines:
            totals[lang] = totals.get(lang, 0) + lines
    top = sorted(totals.items(), key=lambda kv: -kv[1])[:5]
    return ", ".join(f"{k} {v:,}" for k, v in top)


def _build(root: Path, focus: Optional[Path]) -> str:
    blocked = _code.blocked_checker()
    deadline = time.monotonic() + TIME_LIMIT
    key = root.as_posix()
    with _LOCK:
        cache = _CACHE.setdefault(key, {})
        while len(_CACHE) > _MAX_ROOTS:
            _CACHE.pop(next(iter(_CACHE)))
    start = focus if (focus is not None and focus.is_dir()) else root
    tree, seen, walked = _Dir(), [], 0
    for p in _code.walk_files(start, blocked=blocked, max_files=MAX_FILES, deadline=deadline):
        walked += 1
        ext = p.suffix.lower()
        rel = _code.rel_posix(p, root)
        if ext in _SYMBOL_EXTS:
            entry = _outline(p, cache, rel)
            if entry is None:
                continue
            _add(tree, rel, entry[2], entry[3])
            seen.append((rel, entry[2]))
        elif ext in _LISTED_EXTS:
            _add(tree, rel, None, [])
    cut = walked >= MAX_FILES or time.monotonic() > deadline
    scope = _code.rel_posix(start, root) if start != root else ""
    total_lines = sum(n or 0 for _r, n in seen)
    head = (f"Project map of {start.as_posix()}: {tree.count()} files"
            + (f", {total_lines:,} lines of code ({_languages(seen)})" if total_lines else ""))
    if not tree.count():
        return head + ". Nothing to map here (no source files found)."
    chosen = None
    budget = FOCUS_BUDGET if focus is not None else BUDGET
    for level in (3, 2, 1, 0):
        body = "\n".join(_render_tree(tree, level))
        if len(body) <= budget:
            chosen = (level, body)
            break
    if chosen is None:
        body = "\n".join(_render_tree(tree, 0))
        chosen = (0, body[:budget].rsplit("\n", 1)[0] + "\n[... cut ...]")
    level, body = chosen
    notes = {3: "", 2: "\n[methods left out to keep it short: use focus=<file> to see everything in one file]",
             1: "\n[symbols left out to keep it short: use focus=<folder or file> to zoom in]",
             0: "\n[only folders shown: use focus=<folder> to zoom in]"}[level]
    if cut:
        notes += "\n[the project is large: only the first files were mapped; use focus to look at one part]"
    return f"{head}\n{body}{notes}"


def _file_view(root: Path, path: Path) -> str:
    key = root.as_posix()
    with _LOCK:
        cache = _CACHE.setdefault(key, {})
    rel = _code.rel_posix(path, root)
    entry = _outline(path, cache, rel)
    if entry is None:
        return f"Error: {rel} is not a text file the map can read."
    _m, _s, lines, syms = entry
    if path.suffix.lower() not in _SYMBOL_EXTS:
        return f"{rel}: {lines or '?'} lines. No symbol outline for this file type: use read_local_file."
    body = "\n".join(f"L{n} {'  ' * d}{t}" for n, d, t in syms) or "(no classes or functions found)"
    return f"{rel} ({lines} lines)\n{body}"


def _run(args: dict) -> str:
    raw = str(args.get("path") or "").strip()
    focus_raw = str(args.get("focus") or "").strip()
    root, err = _code.resolve_dir(raw)
    if root is None:
        return err
    if raw and _code.resolve_path(raw) is not None and _code.resolve_path(raw).is_file() and not focus_raw:
        focus_raw = _code.resolve_path(raw).name                   # path pointed at a file: show that file
    focus: Optional[Path] = None
    if focus_raw:
        cand = Path(focus_raw.replace("\\", "/"))
        focus = cand if cand.is_absolute() else root / cand
        try:
            from lucy.safety.config import is_inside, norm_path
            inside = is_inside(norm_path(focus), norm_path(root))
        except Exception:                                           # noqa: BLE001
            inside = True
        if not inside:                                              # `focus` is not a policy-checked argument: keep it in the project
            return f"Error: focus must be inside the project folder ({root.as_posix()})."
        if not focus.exists():
            return f"Error: focus not found: {focus_raw} (inside {root.as_posix()}). Run project_map without focus to see the paths."
        if _code.blocked_checker()(focus):
            return "Refused: that location is blocked (secrets / private data)."
    if focus is not None and focus.is_file():
        return _file_view(root, focus)
    return _build(root, focus)


async def handler(args, ctx):
    return await asyncio.to_thread(_run, args or {})
