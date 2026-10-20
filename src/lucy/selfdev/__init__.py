"""P7 self-evolution: Lane A (tools Lucy writes for herself, sandboxed) and Lane B (gated changes to Lucy Core).

CONSTITUTION: every file in this folder is hash-locked (src/lucy/safety/constitution.py), and runtime/selfdev/ (the
records: tool tiers, proposals, releases) is refused to the agent's file tools by the safety layer. See
docs/SELF_EVOLUTION.md. Keep this module light: `python -m lucy.selfdev rollback` must work when nothing else imports.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

_tools = None
_laneb = None


def _root() -> Path:
    return Path(os.environ.get("LUCY_ROOT") or Path(__file__).resolve().parents[3]).resolve()


def state_dir(root: Optional[Path] = None) -> Path:
    return Path(root or _root()) / "runtime" / "selfdev"


def workspaces_dir(root: Optional[Path] = None) -> Path:
    return Path(os.environ.get("LUCY_WORKSPACES") or (Path(root or _root()) / "runtime" / "workspaces"))


def tools_dir(root: Optional[Path] = None) -> Path:
    return Path(root or _root()) / "src" / "lucy" / "tools_user"


def _deny_read():
    """Secrets the sandbox must never read: the user's denied_paths + the admin helper's secret folder.
    NOT runtime/selfdev: new tools are tested from runtime/selfdev/staging/ (and it holds no secrets)."""
    try:
        from lucy import safety
        layer = safety.get()
        return list(layer.cfg.denied_paths) + [str(layer.runtime / "broker")]
    except Exception:                                    # noqa: BLE001
        return []


def _resolve(p: str):
    try:
        from lucy import safety
        return safety.get()._resolve(p)
    except Exception:                                    # noqa: BLE001
        return None


def get_tools():
    global _tools
    if _tools is None:
        from lucy.tools import registry
        from .user_tools import UserTools
        _tools = UserTools(tools_dir=tools_dir(), state_dir=state_dir(), registry=registry.default,
                           deny_read=_deny_read, resolve=_resolve)
    return _tools


def get_lane_b():
    global _laneb
    if _laneb is None:
        from .lane_b import LaneB
        _laneb = LaneB(root=_root(), state_dir=state_dir(), workspaces=workspaces_dir())
    return _laneb


def set_instances(tools=None, lane_b=None) -> None:
    """Tests swap in instances on temp folders (None = rebuild from the real paths next time)."""
    global _tools, _laneb
    _tools, _laneb = tools, lane_b

_miner = None


def get_miner():
    global _miner
    if _miner is None:
        from .trace_mining import TraceMiner
        root = _root()
        _miner = TraceMiner(trace_path=root / "runtime" / "logs" / "agent_trace.jsonl", state_dir=state_dir(root),
                            notes_dir=root / "runtime" / "data" / "learned")
    return _miner


def set_miner(miner) -> None:
    global _miner
    _miner = miner
