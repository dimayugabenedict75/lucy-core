"""file_ops: copy / move / delete (to the Recycle Bin) / make a folder. Works on whole folders too.

Normal places run in Lucy's process. Protected system locations (permissions.yaml readonly_paths, e.g. Program Files)
go to the admin broker after a hard-ask approval card. Drive rules (permissions.yaml `drives`) decide what asks:
e.g. deleting or moving something OUT of a `storage` drive (your backup drive) always asks.
Big copies/moves turn into a job: call again with action=poll and the job_id.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import _pc
from ._common import outside_root, resolve_path

_ACTIONS = ("copy", "move", "delete", "mkdir", "poll")


def _protected(p: Path) -> bool:
    try:
        from lucy import safety
        from lucy.safety.config import is_inside, norm_path
        cfg = safety.get().cfg
        n = norm_path(p)
        return any(is_inside(n, r) for r in cfg.readonly_paths)
    except Exception:                                           # noqa: BLE001
        return False


def _resolve(raw) -> "Path | str":
    if not str(raw or "").strip():
        return ""
    r = resolve_path(str(raw).strip())
    return r if r is not None else outside_root(str(raw))


def _h_file_ops(args, ctx):
    a = _pc.action_of(args)
    if a not in _ACTIONS:
        return "Failed: action must be copy, move, delete, mkdir or poll"
    if a == "poll":
        return _pc.poll(args.get("job_id"), "file_ops")
    keys = {"copy": ("source", "destination"), "move": ("source", "destination"),
            "delete": ("path",), "mkdir": ("path",)}[a]
    if a in ("delete", "mkdir") and not args.get("path"):
        args = {**args, "path": args.get("source") or args.get("destination")}
    real = {}
    for k in keys:
        r = _resolve(args.get(k))
        if r == "":
            return f"Failed: '{k}' is required for {a}"
        if isinstance(r, str):
            return f"Failed: {r}"
        real[k] = r
    op_args = {**{k: str(v) for k, v in real.items()}, "overwrite": bool(args.get("overwrite"))}
    admin = any(_protected(p) for p in real.values())
    res = _pc.execute("fs." + a, op_args, tool="file_ops", dry_run=args.get("dry_run") is True, force_admin=admin)
    if res.startswith("Done") and a == "move" and os.path.exists(real["source"]):
        res += " WARNING: the source still exists; check before deleting anything."
    return res


def describe_call(name: str, args: dict) -> str:
    a = _pc.action_of(args)
    dry = " (dry run)" if args.get("dry_run") is True else ""
    if a in ("copy", "move"):
        return f"{a.capitalize()} {args.get('source')} -> {args.get('destination')}{dry}"
    if a == "delete":
        return f"Delete {args.get('path') or args.get('source')} (to the Recycle Bin){dry}"
    if a == "mkdir":
        return f"Create folder {args.get('path')}{dry}"
    return ""


SCHEMA = _pc.schema(
    "file_ops", "Copy, move, delete (to the Recycle Bin) or create files/folders, whole folders included. "
    "Use for backups, tidying and moving things between drives.", list(_ACTIONS),
    {"source": {"type": "string", "description": "copy/move: what to copy or move"},
     "destination": {"type": "string", "description": "copy/move: target folder or new full path"},
     "path": {"type": "string", "description": "delete/mkdir: the file or folder"},
     "overwrite": {"type": "boolean"}, "job_id": {"type": "string"}})
handler = _h_file_ops
TIER = "write"
LABEL = "files"
