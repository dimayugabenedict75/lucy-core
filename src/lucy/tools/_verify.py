"""VERIFY step: after the agent changes a code file, the harness checks it and appends the result to the tool answer, so the
model sees breakage immediately instead of discovering it three rounds later.

Runs inside the harness, not as a model tool call, so it needs no approval and the model can't skip it.
  LUCY_VERIFY=syntax  (default)  syntax check of the changed file (python, json, yaml, javascript via `node --check`)
  LUCY_VERIFY=tests              syntax check, then the project's tests (user opt-in: it runs the project's code)
  LUCY_VERIFY=off                nothing
Leading underscore: not a tool module.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from . import _code
from ._common import resolve_path

VERIFY_TOOLS = frozenset({"edit_local_file", "write_local_file", "apply_patch"})
TEST_WAIT = 60.0
_CODE_FOR_TESTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".cs"}


def mode() -> str:
    m = (os.environ.get("LUCY_VERIFY") or "syntax").strip().lower()
    return m if m in ("syntax", "tests", "off") else "syntax"


def check_syntax(path: Path) -> Optional[str]:
    """None = fine or not checkable; otherwise a one-line problem."""
    ext = path.suffix.lower()
    try:
        if ext in (".py", ".pyi"):
            compile(path.read_text(encoding="utf-8-sig"), str(path), "exec")
        elif ext == ".json":
            json.loads(path.read_text(encoding="utf-8-sig"))
        elif ext in (".yaml", ".yml"):
            import yaml
            yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        elif ext in (".js", ".mjs", ".cjs"):
            node = _code.which("node")
            if node:
                r = subprocess.run([node, "--check", str(path)], capture_output=True, timeout=20, stdin=subprocess.DEVNULL)
                if r.returncode != 0:
                    msg = (r.stderr.decode("utf-8", "replace").strip().splitlines() or ["syntax error"])
                    err = next((l for l in msg if "Error" in l), msg[-1])
                    return err[:200]
    except SyntaxError as e:
        return f"line {e.lineno}: {e.msg}"
    except ImportError:
        return None
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return None if isinstance(e, (OSError, subprocess.SubprocessError)) else str(e)[:200]
    except Exception as e:                                    # noqa: BLE001 - yaml errors etc.
        return str(e).splitlines()[0][:200] if str(e) else type(e).__name__
    return None


def _project_for(path: Path) -> Optional[Path]:
    cur = path.parent
    for _ in range(6):
        if _code.detect_test_kind(cur):
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


async def _tests(path: Path) -> str:
    from . import run_tests
    proj = _project_for(path)
    if proj is None:
        return ""
    p, err = run_tests.plan({"cwd": str(proj)})
    if p is None:
        return ""
    job = await asyncio.to_thread(run_tests.start, p, 300)
    try:
        done = await asyncio.to_thread(job.wait, TEST_WAIT)
    except asyncio.CancelledError:
        job.kill()
        raise
    if not done:
        return f"[Harness check] tests still running after {int(TEST_WAIT)}s (job #{job.id}): poll with run_shell_command."
    return "[Harness check] " + run_tests.finish(p, job).replace("\n", "\n  ")


async def verify_after(name: str, args: dict, result: str) -> str:
    """Text to append to a successful write/edit result ('' = nothing to add)."""
    m = mode()
    if m == "off" or name not in VERIFY_TOOLS:
        return ""
    target = resolve_path(str((args or {}).get("file_path") or ""))
    if target is None or not target.is_file():
        return ""
    problem = await asyncio.to_thread(check_syntax, target)
    if problem:
        return (f"[Harness check] SYNTAX PROBLEM in {target.name}: {problem}. The file is saved but broken: fix this before "
                "anything else.")
    out = ""
    if target.suffix.lower() in (".py", ".js", ".mjs", ".cjs", ".json", ".yaml", ".yml"):
        out = f"[Harness check] syntax OK ({target.name})."
    if m == "tests" and target.suffix.lower() in _CODE_FOR_TESTS:
        t = await _tests(target)
        if t:
            out = (out + "\n" if out else "") + t
    return out
