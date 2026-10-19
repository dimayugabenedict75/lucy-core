"""format_and_lint: run the project's linter / formatter if one is installed (ruff, black, eslint, prettier), and say plainly
when none is. `check` only reports; `fix` rewrites ONE file and snapshots it first (undo list in Settings > Safety).

Like run_tests there is no free-form command: the tool picks the program, the file/folder is a typed argument and goes
to the program as one argv item. Registered as tier "exec" (it runs programs, and a project's eslint config is code);
`path` is the policy-checked argument, so path rules apply to it.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from lucy import paths as _paths

from . import _code, jobs
from ._common import outside_root, resolve_path

TIMEOUT = 90
OUT_CHARS = 3500

SCHEMA = {
    "type": "function",
    "function": {
        "name": "format_and_lint",
        "description": ("Check code style and common mistakes with the project's linter (ruff, black, eslint or prettier, "
                        "whichever is installed). action 'fix' reformats one file; 'check' only reports."),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "A file or folder to check (default: the Lucy Core folder)."},
                "action": {"type": "string", "enum": ["check", "fix"],
                           "description": "check = report problems (default); fix = rewrite ONE file to fix style."},
            },
            "required": [],
        },
    },
}
TIER = "exec"
LABEL = "edit"

PY_EXT = {".py", ".pyi"}
JS_EXT = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue"}


def _module_available(py: str, module: str) -> bool:
    try:
        r = subprocess.run([py, "-c", f"import importlib.util,sys; sys.exit(0 if importlib.util.find_spec({module!r}) else 1)"],
                           capture_output=True, timeout=20, stdin=subprocess.DEVNULL)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _local_bin(root: Path, name: str) -> Optional[str]:
    exe = root / "node_modules" / ".bin" / (name + (".cmd" if os.name == "nt" else ""))
    return str(exe) if exe.is_file() else None


def pick_tools(target: Path, project: Path) -> List[Tuple[str, List[str]]]:
    """-> [(tool name, argv prefix)] that apply to `target`, best first. Empty = nothing installed for this language."""
    out: List[Tuple[str, List[str]]] = []
    exts = {target.suffix.lower()} if target.is_file() else ({p.suffix.lower() for p in list(target.glob("*"))[:200]})
    if exts & PY_EXT or (target.is_dir() and any(target.rglob("*.py"))):
        py = _code.python_for(project)
        ruff = _code.which("ruff")
        if ruff:
            out.append(("ruff", [ruff]))
        elif _module_available(py, "ruff"):
            out.append(("ruff", [py, "-m", "ruff"]))
        if _module_available(py, "black"):
            out.append(("black", [py, "-m", "black"]))
    if exts & JS_EXT or (target.is_dir() and (project / "package.json").is_file()):
        eslint = _local_bin(project, "eslint") or _code.which("eslint")
        if eslint:
            out.append(("eslint", [eslint]))
        prettier = _local_bin(project, "prettier") or _code.which("prettier")
        if prettier:
            out.append(("prettier", [prettier]))
    return out


def _argv(tool: str, prefix: List[str], target: Path, fix: bool) -> List[str]:
    t = str(target)
    if tool == "ruff":
        return prefix + (["check", "--fix", "--no-cache", t] if fix else ["check", "--no-cache", t])
    if tool == "black":
        return prefix + ([t] if fix else ["--check", "--diff", "--quiet", t])
    if tool == "eslint":
        return prefix + (["--fix", t] if fix else [t])
    if tool == "prettier":
        return prefix + (["--write", t] if fix else ["--check", t])
    raise ValueError(tool)


def _run(argv: List[str], cwd: Path) -> Tuple[int, str]:
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    env = {k: v for k, v in os.environ.items() if k.upper() not in jobs._HIDDEN_ENV}
    env.update({"NO_COLOR": "1", "FORCE_COLOR": "0", "PYTHONIOENCODING": "utf-8"})
    p = subprocess.run(argv, cwd=str(cwd), capture_output=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL, env=env, **kwargs)
    return p.returncode, (p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")).replace("\r\n", "\n")


def _snapshot(path: Path, session: Optional[str]) -> None:
    try:
        from lucy import safety
        safety.get().backups.snapshot(path, session=session, tool="format_and_lint")
    except Exception:                                                  # noqa: BLE001 - never block on the undo copy
        pass


def _do(args: dict, session: Optional[str]) -> str:
    raw = str(args.get("path") or "").strip()
    target: Optional[Path] = resolve_path(raw) if raw else _paths.ROOT
    if target is None:
        return outside_root(raw)
    if not target.exists():
        return f"Error: not found: {raw}. Use find_file or list_local_files to locate it."
    action = str(args.get("action") or "check").strip().lower()
    if action not in ("check", "fix"):
        return "Error: action must be 'check' or 'fix'."
    fix = action == "fix"
    if fix and not target.is_file():
        return "Error: 'fix' works on ONE file at a time (so every change can be undone). Give a file path, or use 'check' for a folder."
    project = target if target.is_dir() else target.parent
    tools = pick_tools(target, project)
    if not tools:
        kind = "Python (ruff or black)" if target.suffix.lower() in PY_EXT else "JavaScript/TypeScript (eslint or prettier)" \
            if target.suffix.lower() in JS_EXT else "this kind of file"
        return (f"No linter/formatter is installed for {kind}. Nothing was changed. The user can install one, e.g. "
                "pip install ruff   (or npm install --save-dev eslint prettier in the project).")
    name, prefix = tools[0]
    if fix:
        _snapshot(target, session)
        before = target.read_bytes() if target.is_file() else b""
    try:
        code, out = _run(_argv(name, prefix, target, fix), project)
    except subprocess.TimeoutExpired:
        return f"Command failed: {name} took longer than {TIMEOUT}s and was stopped."
    except OSError as e:
        return f"Error: could not run {name}: {e}"
    out = out.strip()
    if fix:
        changed = target.read_bytes() != before
        left = ""
        if name == "ruff" and code != 0:
            left = "\nStill reported (needs a manual fix):\n" + _code.tail_lines(out, 20, 1800)
        elif name == "black":
            pass
        return (f"{name} {'rewrote' if changed else 'made no changes to'} {target.as_posix()}."
                + (" (Undo: Settings > Safety.)" if changed else "") + left)
    if code == 0:
        return f"{name}: no problems found in {target.as_posix()}." + (f"\n{_code.tail_lines(out, 8, 600)}" if out and name != "black" else "")
    head = f"{name} found problems in {target.as_posix()} (exit code {code}):"
    return f"{head}\n{_code.tail_lines(out, 40, OUT_CHARS)}"


async def handler(args, ctx):
    return await asyncio.to_thread(_do, args or {}, getattr(ctx, "session_id", None))
