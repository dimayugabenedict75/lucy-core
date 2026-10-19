"""git: the local git actions a coding agent needs (status, diff, log, show, branch, add, commit, checkout, stash, init).

Safety by construction rather than by inspection:
  * no free-form arguments: every value is a typed field, passed to git as ONE argv item, never through a shell;
  * refs/files may not start with '-' (no option injection);
  * local only: there is no push / pull / fetch / clone / remote / reset / clean action;
  * restoring files (checkout with `files`) snapshots them first, so Settings > Safety > Restore can undo it;
  * hostile repo config can't run programs through the usual hooks: fsmonitor, external diff and textconv are off.
Registered as tier "write": the target repo is given as `path`, which the safety layer checks like any write.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from . import _code

ACTIONS = ("status", "diff", "log", "show", "branch", "add", "commit", "checkout", "stash", "stash_pop", "stash_list", "init")
TIMEOUT = 60
OUT_CHARS = 6000

SCHEMA = {
    "type": "function",
    "function": {
        "name": "git",
        "description": ("Run a local git action in a project folder (status, diff, log, show, branch, add, commit, checkout, "
                        "stash, init). It never pushes, pulls or changes remotes."),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ACTIONS),
                           "description": "status = what changed; diff = the changes; add = stage files; commit = save staged "
                                          "changes; checkout = switch branch (or restore files); stash = shelve changes."},
                "path": {"type": "string", "description": "The project (repo) folder (default: the Lucy Core folder)."},
                "message": {"type": "string", "description": "Commit message (commit) or note (stash)."},
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "File paths inside the repo (add needs them; ['.'] = everything)."},
                "ref": {"type": "string", "description": "Branch name or commit (log, show, diff, branch, checkout)."},
                "staged": {"type": "boolean", "description": "diff: show what is staged instead of the working changes."},
                "create": {"type": "boolean", "description": "checkout: create the branch named in ref."},
            },
            "required": ["action"],
        },
    },
}
TIER = "write"
LABEL = "run"

_REF = re.compile(r"^[A-Za-z0-9._/@^~{}\-]+$")


def git_exe() -> Optional[str]:
    return shutil.which("git")


def _flag(args: dict, key: str) -> bool:
    v = args.get(key, False)
    return v.strip().lower() in ("true", "1", "yes", "y") if isinstance(v, str) else bool(v)


def _ref(value: object) -> Tuple[Optional[str], str]:
    text = str(value or "").strip()
    if not text:
        return None, ""
    if text.startswith("-") or not _REF.match(text) or len(text) > 200:
        return None, f"Error: {text!r} is not a valid branch name or commit."
    return text, ""


def _files(value: object) -> Tuple[List[str], str]:
    if value is None or value == "":
        return [], ""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return [], "Error: 'files' must be a list of paths."
    out = []
    for f in value:
        s = str(f).strip().strip("\"'")
        if not s:
            continue
        if s.startswith("-") or "\x00" in s or "\n" in s:
            return [], f"Error: {s!r} is not a valid file path."
        out.append(s)
    return out[:100], ""


def _run(argv_tail: List[str], cwd: Path, *, timeout: int = TIMEOUT) -> subprocess.CompletedProcess:
    exe = git_exe()
    if exe is None:
        raise FileNotFoundError("git")
    argv = [exe, "-c", "core.quotepath=off", "-c", "color.ui=never", "-c", "core.pager=cat",
            "-c", "core.fsmonitor=false", "-c", "protocol.ext.allow=never"] + argv_tail
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "GIT_EDITOR": "true", "GIT_OPTIONAL_LOCKS": "0",
           "LC_ALL": "C", "GIT_ASKPASS": "echo"}
    for k in ("DEV_API_KEY", "LUCY_API_KEY"):
        env.pop(k, None)
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env, **kwargs)


def _text(b: bytes) -> str:
    return b.decode("utf-8", "replace").replace("\r\n", "\n")


def _cap(text: str, limit: int = OUT_CHARS, hint: str = "narrow it with files=[...] or ref") -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0] + f"\n[... output cut at {limit} characters: {hint} ...]"


def _explain(action: str, proc: subprocess.CompletedProcess) -> str:
    err = (_text(proc.stderr) + _text(proc.stdout)).strip()
    low = err.lower()
    if "not a git repository" in low:
        return "Error: this folder is not a git repository. Create one with git action 'init', or give the repo folder as path."
    if "dubious ownership" in low:
        return ("Error: git refuses this folder (dubious ownership: it belongs to another Windows user). The user can allow it with "
                "git config --global --add safe.directory <folder>; Lucy won't change git settings.")
    if "please tell me who you are" in low or "unable to auto-detect email" in low:
        return ("Error: git doesn't know who is committing (no user.name / user.email). Ask the user to set them with "
                "git config --global user.name ... and user.email ...; Lucy won't change git settings.")
    return f"Error: git {action} failed (exit code {proc.returncode}): {_cap(err, 1500) or '[no output]'}"


def _do(args: dict, session: Optional[str]) -> str:
    action = str(args.get("action") or "").strip().lower()
    if action not in ACTIONS:
        return f"Error: unknown action {action!r}. Use one of: {', '.join(ACTIONS)}."
    if git_exe() is None:
        return "Error: git is not installed (or not on PATH). Install Git for Windows (winget install Git.Git) and restart Lucy Core."
    cwd, err = _code.resolve_dir(args.get("path"))
    if cwd is None:
        return err
    ref, e1 = _ref(args.get("ref"))
    files, e2 = _files(args.get("files"))
    if e1 or e2:
        return e1 or e2
    message = str(args.get("message") or "").strip()
    try:
        return _dispatch(action, cwd, args, ref, files, message, session)
    except subprocess.TimeoutExpired:
        return f"Error: git {action} did not finish within {TIMEOUT}s and was stopped (a very large repo or a slow disk)."
    except OSError as e:
        return f"Error: could not run git: {e}"


def _dispatch(action: str, cwd: Path, a: dict, ref: Optional[str], files: List[str], message: str, session: Optional[str]) -> str:
    tail_files = (["--"] + files) if files else []

    if action == "init":
        p = _run(["init"], cwd)
        return (f"Initialized a git repository in {cwd.as_posix()}.\n" + _text(p.stdout).strip()) if p.returncode == 0 else _explain(action, p)

    if action == "status":
        p = _run(["status", "--short", "--branch", "--untracked-files=normal"], cwd)
        if p.returncode != 0:
            return _explain(action, p)
        out = _text(p.stdout).strip().splitlines()
        body = "\n".join(out[1:]) if len(out) > 1 else ""
        head = out[0] if out else ""
        return _cap(f"{head}\n{body}" if body else f"{head}\nWorking tree clean: nothing to commit.", 4000, "run status on a sub-folder")

    if action == "diff":
        scope = (["--staged"] if _flag(a, "staged") else []) + ([ref] if ref else [])
        quiet = ["--no-ext-diff", "--no-textconv"]
        stat = _run(["diff", "--shortstat"] + quiet + scope + tail_files, cwd)
        p = _run(["diff", "--unified=3"] + quiet + scope + tail_files, cwd)
        if p.returncode != 0:
            return _explain(action, p)
        text = _text(p.stdout)
        if not text.strip():
            return "No differences" + (" in the staged changes." if _flag(a, "staged") else " (nothing changed, or the changes are already staged: try staged=true).")
        summary = _text(stat.stdout).strip() if stat.returncode == 0 else ""
        return (summary + "\n" if summary else "") + _cap(text)

    if action == "log":
        n = 15
        p = _run(["log", f"-n{n}", "--date=short", "--decorate=short", "--pretty=format:%h %ad %an: %s%d"] + ([ref] if ref else []) + tail_files, cwd)
        if p.returncode != 0:
            low = _text(p.stderr).lower()
            if "does not have any commits" in low or "bad default revision" in low:
                return "No commits yet in this repository."
            return _explain(action, p)
        return _cap(_text(p.stdout).strip() or "No commits.", 4000, "narrow it with ref or files")

    if action == "show":
        p = _run(["show", "--no-ext-diff", "--no-textconv", "--date=short", "--stat", "--patch" if files else "--no-patch",
                  "--pretty=format:%h %ad %an%n%s%n%n%b", ref or "HEAD"] + tail_files, cwd)
        return _cap(_text(p.stdout).strip()) if p.returncode == 0 else _explain(action, p)

    if action == "branch":
        if ref:
            p = _run(["branch", ref], cwd)
            return f"Created branch {ref} (not switched to it: use checkout)." if p.returncode == 0 else _explain(action, p)
        p = _run(["branch", "--list", "-vv", "--no-color"], cwd)
        return _cap(_text(p.stdout).strip() or "No branches yet (no commits).", 3000) if p.returncode == 0 else _explain(action, p)

    if action == "add":
        if not files:
            return "Error: 'add' needs files, e.g. files=['src/app.py'] (files=['.'] stages everything)."
        p = _run(["add", "--"] + files, cwd)
        if p.returncode != 0:
            return _explain(action, p)
        s = _run(["status", "--short"], cwd)
        return "Staged. Now:\n" + _cap(_text(s.stdout).strip() or "(nothing staged)", 2500)

    if action == "commit":
        if not message:
            return "Error: 'commit' needs a message describing the change."
        if len(message) > 4000:
            return "Error: the commit message is too long (max 4000 characters)."
        p = _run(["commit", "-m", message], cwd)
        if p.returncode != 0:
            low = (_text(p.stdout) + _text(p.stderr)).lower()
            if "nothing to commit" in low or "no changes added to commit" in low or "nothing added to commit" in low:
                return ("Nothing to commit: no changes are staged. Stage files first with git action 'add' (files=[...], "
                        "files=['.'] for everything).")
            return _explain(action, p)
        return "Committed.\n" + _cap(_text(p.stdout).strip(), 1500)

    if action == "checkout":
        if files:                                                         # restore files: this DISCARDS their working changes
            _snapshot(cwd, files, session)
            p = _run(["checkout"] + ([ref] if ref else []) + ["--"] + files, cwd)
            return ("Restored " + ", ".join(files[:8]) + (f" from {ref}" if ref else " to the last committed/staged version")
                    + ". (The previous content was backed up: Settings > Safety can undo this.)") if p.returncode == 0 else _explain(action, p)
        if not ref:
            return "Error: 'checkout' needs ref (the branch or commit to switch to)."
        p = _run(["checkout"] + (["-b"] if _flag(a, "create") else []) + [ref], cwd)
        if p.returncode != 0:
            low = _text(p.stderr).lower()
            if "would be overwritten" in low or "local changes" in low:
                return ("Error: switching would overwrite your uncommitted changes. Commit them (add + commit) or shelve them "
                        "(git action 'stash') first, then switch.")
            return _explain(action, p)
        return f"Now on {ref}.\n" + _cap((_text(p.stdout) + _text(p.stderr)).strip(), 800)

    if action == "stash":
        p = _run(["stash", "push"] + (["-m", message] if message else []), cwd)
        if p.returncode != 0:
            return _explain(action, p)
        out = (_text(p.stdout) + _text(p.stderr)).strip()
        return out or "Stashed."

    if action == "stash_pop":
        p = _run(["stash", "pop"], cwd)
        return _cap((_text(p.stdout) + _text(p.stderr)).strip() or "Restored the stash.", 2500) if p.returncode == 0 else _explain(action, p)

    if action == "stash_list":
        p = _run(["stash", "list"], cwd)
        return (_text(p.stdout).strip() or "No stashes.") if p.returncode == 0 else _explain(action, p)

    return f"Error: unknown action {action!r}."


def _snapshot(cwd: Path, files: List[str], session: Optional[str]) -> None:
    """Back up files before `checkout -- files` throws their changes away (the same undo list the file tools use)."""
    try:
        from lucy import safety
        backups = safety.get().backups
        for f in files:
            p = (cwd / f)
            if p.is_file():
                backups.snapshot(p, session=session, tool="git")
    except Exception:                                                     # noqa: BLE001 - never block the action on the undo copy
        pass


async def handler(args, ctx):
    return await asyncio.to_thread(_do, args or {}, getattr(ctx, "session_id", None))
