"""Helpers shared by the coding tools (code_search, project_map, apply_patch, run_tests, git, python, format_and_lint).

Leading underscore: never treated as a tool module. Nothing here decides what is ALLOWED (that is the safety layer's
job: it sees the `path` / `file_path` / `cwd` arguments of every tool call); it only walks folders sensibly and keeps
the walk out of places the safety config marks as blocked (secrets), because a recursive tool reads far more than the
one path it was given.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Tuple

from lucy import paths as _paths

from ._common import outside_root, resolve_path

# Folders that are never worth searching or mapping (dependencies, caches, build output, VCS internals).
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".nox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".cache", "site-packages", "dist", "build", ".next", ".nuxt", ".gradle",
    ".idea", ".vs", "target", "obj", ".terraform", "bower_components", "coverage", "htmlcov", ".eggs",
}
MAX_FILE_BYTES = 1_000_000          # bigger files are skipped (generated bundles, data dumps)

CODE_EXTS = {
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".cs", ".java", ".kt", ".go", ".rs", ".c", ".h",
    ".cpp", ".hpp", ".cc", ".rb", ".php", ".swift", ".sh", ".ps1", ".bat", ".cmd", ".sql", ".vue", ".svelte",
    ".html", ".css", ".scss", ".md", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml", ".txt",
}


# ------------------------------------------------------------------------------------------------ folders
def resolve_dir(raw: object) -> Tuple[Optional[Path], str]:
    """What the model typed -> (existing folder, "") or (None, error text). Empty = the Lucy Core folder."""
    text = str(raw or "").strip()
    if not text:
        return _paths.ROOT, ""
    p = resolve_path(text)
    if p is None:
        return None, outside_root(text)
    if p.is_file():
        return p.parent, ""                                  # a file was given where a folder was expected
    if not p.is_dir():
        return None, f"Error: folder not found: {text}. Use find_file or list_local_files to locate it."
    return p, ""


def blocked_checker() -> Callable[[Path], bool]:
    """-> f(path) True when the safety config says the location is blocked (secrets, private data). Recursive tools
    must skip these: the policy only checks the ONE path in the arguments, not what a walk would reach below it."""
    try:
        from lucy import safety
        from lucy.safety.config import is_inside, norm_path
        denied = list(safety.get().cfg.denied_paths)
    except Exception:                                         # noqa: BLE001 - no safety layer (unit tests): nothing blocked
        return lambda _p: False
    if not denied:
        return lambda _p: False

    def blocked(p: Path) -> bool:
        n = norm_path(p)
        return any(is_inside(n, d) for d in denied)
    return blocked


def _is_venv(path: Path) -> bool:
    return (path / "pyvenv.cfg").is_file()


def walk_files(root: Path, *, blocked: Optional[Callable[[Path], bool]] = None, max_files: int = 20000,
               deadline: Optional[float] = None) -> Iterator[Path]:
    """Every regular file under `root` (sorted, junk folders pruned). Stops after max_files or at `deadline`
    (a time.monotonic() value); the caller counts what it got to know whether the walk was cut short."""
    import time
    blocked = blocked or (lambda _p: False)
    seen = 0
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        here = Path(dirpath)
        dirs[:] = sorted(d for d in dirs if d.lower() not in SKIP_DIRS and not d.endswith(".egg-info")
                         and not _is_venv(here / d) and not blocked(here / d))
        for f in sorted(files):
            p = here / f
            if blocked(p):
                continue
            seen += 1
            if seen > max_files or (deadline is not None and time.monotonic() > deadline):
                return
            yield p


def glob_to_regex(pattern: str) -> "re.Pattern[str]":
    """'*.py' matches that file name at any depth; 'src/**/*.ts' matches the relative path. Case-insensitive;
    '/' and '\\' are the same."""
    pat = pattern.strip().replace("\\", "/")
    while pat.startswith("./"):
        pat = pat[2:]
    anchored = "/" in pat
    out, i = [], 0
    while i < len(pat):
        c = pat[i]
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        else:
            out.append("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c))
            i += 1
    return re.compile(("^" if anchored else "^(?:.*/)?") + "".join(out) + "$", re.IGNORECASE)


def matches_glob(rx: "re.Pattern[str]", rel_posix: str) -> bool:
    return bool(rx.match(rel_posix))


def looks_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(2048)
    except OSError:
        return True


def read_text(path: Path) -> Optional[str]:
    """Text of a source file (utf-8, falling back to latin-1 so odd encodings still search), None if unreadable/binary."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES or looks_binary(path):
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        return raw.decode("utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def rel_posix(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


# ------------------------------------------------------------------------------------------------ projects
def python_for(cwd: Path) -> str:
    """The project's own virtualenv Python if it has one (so its dependencies are found), else Lucy's interpreter."""
    for rel in (".venv/Scripts/python.exe", "venv/Scripts/python.exe", ".venv/bin/python", "venv/bin/python"):
        cand = cwd / rel
        if cand.is_file():
            return str(cand)
    return sys.executable


def _has_pytest_config(cwd: Path) -> bool:
    if (cwd / "pytest.ini").is_file() or (cwd / "conftest.py").is_file():
        return True
    for name, marker in (("pyproject.toml", "[tool.pytest"), ("setup.cfg", "[tool:pytest]"), ("tox.ini", "[pytest]")):
        f = cwd / name
        if f.is_file():
            try:
                if marker in f.read_text(encoding="utf-8", errors="replace"):
                    return True
            except OSError:
                pass
    tests = cwd / "tests"
    if tests.is_dir() and any(tests.glob("test_*.py")):
        return True
    try:
        return any(cwd.glob("test_*.py")) or any(cwd.glob("*_test.py"))
    except OSError:
        return False


def _npm_has_test(cwd: Path) -> bool:
    pkg = cwd / "package.json"
    if not pkg.is_file():
        return False
    try:
        import json
        script = ((json.loads(pkg.read_text(encoding="utf-8-sig")).get("scripts") or {}).get("test") or "").strip()
    except (OSError, ValueError):
        return False
    return bool(script) and "no test specified" not in script


def _dotnet_project(cwd: Path) -> bool:
    try:
        if any(cwd.glob("*.sln")) or any(cwd.glob("*.csproj")):
            return True
        return any(d.is_dir() and any(d.glob("*.csproj")) for d in cwd.iterdir() if d.name.lower() not in SKIP_DIRS)
    except OSError:
        return False


def detect_test_kind(cwd: Path) -> Optional[str]:
    """'pytest' | 'npm' | 'dotnet' | None. A package.json test script wins over stray Python files in a JS project
    only when there is no Python test config; pytest config is the stronger signal."""
    if _has_pytest_config(cwd):
        return "pytest"
    if _npm_has_test(cwd):
        return "npm"
    if _dotnet_project(cwd):
        return "dotnet"
    return None


def npm_exe() -> str:
    return "npm.cmd" if os.name == "nt" else "npm"


def which(name: str) -> Optional[str]:
    return shutil.which(name)


SAFE_ARG = re.compile(r"^[A-Za-z0-9_\-./\\:\[\]\*\?,=@ ]+$")


def safe_target(value: object) -> Tuple[Optional[str], str]:
    """A test target / file argument handed to another program as ONE argv item (never through a shell). It may not
    start with '-': that would let the model pass options (pytest -p <plugin> loads and runs arbitrary code)."""
    text = str(value or "").strip()
    if not text:
        return None, ""
    if text.startswith("-"):
        return None, "Error: a target may not start with '-' (options cannot be passed). Give a file, folder or test name."
    if not SAFE_ARG.match(text) or len(text) > 300:
        return None, "Error: the target has characters that are not allowed. Use a file path, folder or plain test name."
    return text, ""


def tail_lines(text: str, n: int = 40, max_chars: int = 3500) -> str:
    lines = [l.rstrip() for l in text.strip().splitlines()]
    out = "\n".join(lines[-n:])
    return out[-max_chars:]
