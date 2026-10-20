"""SafetyLayer: ties policy, approvals, audit, undo, constitution and the injection guard together.

Two enforcement points (both always on):
  * gate()    - called by the agent loop before every tool call. May PAUSE the turn to ask the user (approval card).
  * before() / after() - called by the tool registry around EVERY tool execution, whoever the caller is. Applies the hard
                rules (pause, tampered constitution, deny rules), snapshots files before writes, wraps untrusted results,
                writes the audit log. Because every tool runs through the registry, a caller that skipped gate() still
                cannot touch a constitution file, a blocked path or run a wipe-the-disk command.
"""
from __future__ import annotations

import contextvars
import difflib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Set

from . import config as _config
from .approvals import ApprovalBroker
from .audit import AuditLog
from .backups import Backups
from .config import MODE_LABELS, MODES, Config, is_inside, norm_path
from .constitution import Constitution
from .policy import AUTO, ASK, ASK_HARD, DENY, Decision, Policy
from .taint import TaintStore, wrap

# What the loop's gate decided for the call that is executing right now (read by after() for the audit line).
CURRENT: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("lucy_safety_current", default=None)

Emit = Callable[[str, Any], None]


def _sig(name: str, args: dict) -> str:
    return name + ":" + json.dumps(args or {}, sort_keys=True, default=str)


class SafetyLayer:
    def __init__(self, *, root: Path, runtime: Path, safe_root: Path, home: Optional[Path] = None,
                 enforce_constitution: bool = False, config_path: Optional[Path] = None):
        self.root, self.runtime = Path(root), Path(runtime)
        self.safe_root = Path(safe_root)
        self.home = Path(home or Path.home())
        self.config_path = Path(config_path) if config_path else self.root / ".core" / "permissions.yaml"
        self.state_path = self.runtime / "safety_state.json"
        self.enforce = enforce_constitution
        state_files = [self.runtime / "logs" / "audit.jsonl", self.runtime / "backups", self.state_path,
                       self.runtime / "trash", self.runtime / "broker",      # broker: admin helper secret + port
                       self.runtime / "selfdev"]      # P7: user-tool tiers, Lane B proposals + releases (user decides)
        self.constitution = Constitution(self.root, extra_protected=state_files)
        self.audit = AuditLog(self.runtime / "logs" / "audit.jsonl")
        self.broker = ApprovalBroker()
        self.taint = TaintStore()
        self.session_allow: Dict[str, Set[str]] = {}
        self._lock = threading.Lock()
        self._cfg_mtime: Optional[int] = None
        self.cfg: Config = Config()
        self.policy: Policy
        self.backups: Backups
        self._reload(force=True)
        self.paused = bool(self._read_state().get("paused", False))

    # ============================================================================== config / state
    def _reload(self, force: bool = False) -> None:
        try:
            m = self.config_path.stat().st_mtime_ns
        except OSError:
            m = None
        if not force and m == self._cfg_mtime:
            return
        self._cfg_mtime = m
        self.cfg = _config.load(self.config_path, root=self.root, safe_root=self.safe_root, runtime=self.runtime,
                                home=self.home)
        self.policy = Policy(self.cfg, protected=self.constitution.is_protected, resolve=self._resolve,
                             home=str(self.home), default_cwd=str(self.safe_root))
        self.backups = Backups(self.runtime / "backups" / "undo", max_file_bytes=self.cfg.max_file_bytes,
                               max_total_bytes=self.cfg.max_total_bytes, trash_dir=self.runtime / "trash")

    def _read_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_state(self, **kw) -> None:
        st = {**self._read_state(), **kw}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(json.dumps(st), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def _resolve(self, raw: str) -> Optional[str]:
        """The path a tool will really use for what the model typed (same resolver as the file tools)."""
        try:
            from lucy.tools import _common                          # lazy: lucy.tools imports the registry, which imports us
            p = _common.resolve_path(raw)
        except Exception:                                            # noqa: BLE001
            p = None
        if p is not None:
            return norm_path(p)
        r = str(raw or "").strip().strip("\"'`").replace("\\", "/")
        if r.startswith("~") or (len(r) > 1 and r[1] == ":") or r.startswith("/"):
            return norm_path(r)
        return None

    # ============================================================================== status / controls (user only)
    def start(self) -> bool:
        """Startup check (api.py). False = constitution files changed without the user sealing them."""
        self.enforce = True
        ok = self.constitution.verify()
        self.audit.write(event="startup", ok=ok, problems=self.constitution.problems, note=self.constitution.note)
        return ok

    def halt_reason(self) -> Optional[str]:
        if self.enforce and not self.constitution.recheck():
            return ("Lucy's safety files were changed without being sealed by you: "
                    + "; ".join(self.constitution.problems[:6])
                    + ". The agent will not run tools until you check the change and seal it: open a terminal in the "
                      "Lucy Core folder and run  python -m lucy.safety seal  (or restore the files from git/backup).")
        return None

    @property
    def mode(self) -> str:
        self._reload()
        return self.cfg.mode

    def set_mode(self, mode: str, by: str = "user") -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if self.enforce and not self.constitution.recheck():
            raise PermissionError("the safety files have unsealed changes; seal or restore them first")
        if not self.config_path.exists():
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(f"mode: {mode}\n", encoding="utf-8")
        else:
            _config.set_mode_in_file(self.config_path, mode)
        self.constitution.seal(only=[self.config_path], by=f"{by} (mode switch)")
        self._reload(force=True)
        self.audit.write(event="mode", mode=mode, by=by)

    def set_paused(self, paused: bool, by: str = "user") -> None:
        self.paused = bool(paused)
        self._write_state(paused=self.paused)
        if self.paused:
            self.broker.cancel_all()
        self.audit.write(event="pause" if paused else "resume", by=by)

    def status(self, session: Optional[str] = None) -> dict:
        self._reload()
        return {"mode": self.cfg.mode, "mode_label": MODE_LABELS[self.cfg.mode],
                "modes": [{"id": m, "label": MODE_LABELS[m]} for m in MODES],
                "paused": self.paused, "halted": self.halt_reason(), "note": self.constitution.note,
                "config_problems": self.cfg.problems, "write_folders": self.cfg.write_folders,
                "read_folders": self.cfg.read_folders,
                "drives": {**{k.upper() + ":": v for k, v in self.cfg.drives.items()}, "other": self.cfg.other_drive},
                "pending": self.broker.pending(session), "session_allowed": sorted(self.session_allow.get(session or "_", ()))}

    # ============================================================================== decisions
    def decide(self, name: str, tier: str, args: dict, session: Optional[str]) -> Decision:
        self._reload()
        allowed = name in self.session_allow.get(session or "_", set())
        d = self.policy.decide(name, tier, args, session_allowed=allowed)
        why = self.taint.suspicious(session, name, d.tier, args)
        if why and d.level < ASK_HARD:
            d.raise_to(ASK_HARD, f"possible prompt injection: {why}", "injection")
            d.rule = "injection"
        return d

    @staticmethod
    def _tier_of(name: str) -> str:
        try:
            from lucy.tools import registry
            spec = registry.default.get(name)
        except Exception:                                            # noqa: BLE001
            spec = None
        return spec.tier if spec is not None else "exec"             # unknown -> treated as risky

    def gate_for(self, session: Optional[str]) -> Callable[[str, dict, Optional[Emit]], Awaitable[Optional[str]]]:
        async def gate(name: str, args: dict, emit: Optional[Emit]) -> Optional[str]:
            return await self.gate(name, args, emit, session)
        return gate

    async def gate(self, name: str, args: dict, emit: Optional[Emit], session: Optional[str]) -> Optional[str]:
        """None = go ahead (the tool runs next). A string = the call was not allowed; it is the tool result."""
        tier = self._tier_of(name)
        if self.paused:
            return self._refuse(name, tier, args, session, "paused", "Lucy is paused by the user")
        halt = self.halt_reason()
        if halt:
            return self._refuse(name, tier, args, session, "constitution", halt)
        d = self.decide(name, tier, args, session)
        if d.level == AUTO:
            CURRENT.set({"sig": _sig(name, args), "decision": "auto",
                         "by": "session_rule" if d.rule == "session_rule" else "policy"})
            return None
        if d.level == DENY:
            return self._refuse(name, d.tier, args, session, d.rule, d.reason)
        if emit is None:
            self.audit.write(session=session, tool=name, tier=d.tier, decision="denied", by="no_approver",
                             reason=d.reason, args=args)
            return (f"Not run: {name} needs the user's approval ({d.reason}), and nobody can approve it here "
                    "(background task). Do the parts of the task that need no approval and report what is left.")
        req = self.describe(name, args, d)
        aid = self.broker.create(session, req)
        emit("approval_request", {"id": aid, **req})
        emit("activity", "approval")
        decision = await self.broker.wait(aid, self.cfg.approval_timeout_s)
        emit("approval_resolved", {"id": aid, "decision": decision})
        if decision in ("approve", "always"):
            if decision == "always" and d.can_always_allow:
                self.session_allow.setdefault(session or "_", set()).add(name)
            CURRENT.set({"sig": _sig(name, args), "decision": "approved", "by": "user", "reason": d.reason,
                         "always": decision == "always"})
            return None
        self.audit.write(session=session, tool=name, tier=d.tier, decision="denied",
                         by="timeout" if decision == "timeout" else "user", reason=d.reason, args=args)
        if decision == "timeout":
            head = (f"Not run: nobody answered the approval request for {name} within "
                    f"{int(self.cfg.approval_timeout_s)} s, so it counts as a denial.")
        else:
            head = f"Not run: the user DENIED this {name} call ({req['summary']})."
        return (head + " Do not ask for the same call again and do not get the same effect through another tool. "
                "Carry on with the rest of the task; if this step was essential, tell the user what you wanted to do "
                "and why, and ask how they want to proceed.")

    def _refuse(self, name, tier, args, session, rule, reason) -> str:
        self.audit.write(session=session, tool=name, tier=tier, decision="refused", by="hard_rule", rule=rule,
                         reason=reason, args=args)
        return (f"Refused by Lucy's safety rules: {reason}. Nothing was run. This rule is final: do not retry it or try "
                "to reach the same effect another way. Choose a different action or tell the user.")

    # ============================================================================== registry hooks
    def before(self, name: str, tier: str, args: dict, session: Optional[str]) -> Optional[str]:
        """Hard rules for EVERY execution (never asks). Also snapshots files about to change."""
        if self.paused:
            return self._refuse(name, tier, args, session, "paused", "Lucy is paused by the user")
        halt = self.halt_reason()
        if halt:
            return self._refuse(name, tier, args, session, "constitution", halt)
        d = self.decide(name, tier, args, session)
        if d.level == DENY:
            return self._refuse(name, d.tier, args, session, d.rule, d.reason)
        if d.tier == "write" and d.write_paths:
            for p in dict.fromkeys(d.write_paths):
                self.backups.snapshot(Path(p), session=session, tool=name)
        return None

    def after(self, name: str, tier: str, args: dict, session: Optional[str], result: str, ms: int,
              error: Optional[str] = None) -> str:
        cur = CURRENT.get()
        mine = cur if cur and cur.get("sig") == _sig(name, args) else None
        untrusted = name in self.cfg.untrusted_tools
        if not untrusted and tier == "read":
            for k in ("file_path", "image_path", "path"):
                v = (args or {}).get(k)
                r = self._resolve(v) if isinstance(v, str) and v.strip() else None
                if r and any(is_inside(r, f) for f in self.cfg.untrusted_paths):
                    untrusted = True
        if untrusted and isinstance(result, str) and error is None:
            self.taint.add(session, result)
            result = wrap(name, result)
        self.audit.write(session=session, tool=name, tier=tier, decision=(mine or {}).get("decision", "auto"),
                         by=(mine or {}).get("by", "direct"), reason=(mine or {}).get("reason", ""), args=args,
                         ok=error is None, ms=ms, result=(error or str(result))[:160], untrusted=untrusted or None)
        if mine:
            CURRENT.set(None)
        return result

    # ============================================================================== plain-language request
    def describe(self, name: str, args: dict, d: Decision) -> dict:
        a = args or {}
        summary, details, kind = name, "", "text"
        path = self._resolve(str(a.get("file_path") or "")) if a.get("file_path") else None
        try:
            if name == "write_local_file" and path:
                new = str(a.get("content") or "")
                p = Path(path)
                if p.is_file():
                    from lucy.server import file_guard
                    old, _crlf = file_guard.read_text_keep_style(p)
                    summary = f"Overwrite {path} ({len(new)} characters)"
                    details, kind = self._diff(old, new.replace("\r\n", "\n"), path), "diff"
                else:
                    summary = f"Create {path} ({len(new)} characters)"
                    details = "\n".join(new.splitlines()[:60])
            elif name == "edit_local_file" and path:
                summary = f"Edit {path}"
                old_s, new_s = str(a.get("old_string") or ""), str(a.get("new_string") or "")
                details, kind = self._diff(old_s, new_s, path), "diff"
                p = Path(path)
                if p.is_file():
                    from lucy.server import file_guard
                    text, _crlf = file_guard.read_text_keep_style(p)
                    new_text, _msg = file_guard.apply_edit(text, old_s, new_s, bool(a.get("replace_all")))
                    if new_text is not None:
                        details = self._diff(text, new_text, path)
            elif name == "run_shell_command":
                shell = a.get("shell") or "default shell"
                where = self._resolve(str(a["cwd"])) if a.get("cwd") else None
                bg = " in the background" if str(a.get("action") or "").lower() == "start" else ""
                summary = f"Run a {shell} command{bg}" + (f" in {where}" if where else "")
                details, kind = str(a.get("command") or ""), "command"
            elif name == "cua_mouse":
                summary = f"Control the mouse: {a.get('action', 'click')} at ({a.get('x')}, {a.get('y')})"
            elif name == "cua_keyboard":
                txt = a.get("text") or "+".join(map(str, a.get("keys") or []))
                summary = f"Use the keyboard: {a.get('action', 'press')} {str(txt)[:120]}"
            elif name == "cua_cursor":
                summary = f"Move the mouse pointer to ({a.get('x')}, {a.get('y')})"
            elif name == "web_extract":
                summary = "Open web page(s): " + ", ".join(map(str, a.get("urls") or []))[:300]
            elif name == "remember":
                summary = "Save to long-term memory"
                details = str(a.get("content") or "")[:600]
            elif name == "restore_from_backup":
                summary = f"Undo change {a.get('backup_id')}"
            elif self._tool_summary(name, a):
                summary = self._tool_summary(name, a)
                details = json.dumps(a, ensure_ascii=False, indent=1, default=str)[:2000]
            else:
                summary = f"{name}"
                details = json.dumps(a, ensure_ascii=False, indent=1, default=str)[:2000]
        except Exception as e:                                       # noqa: BLE001 - a description must never block a call
            details = f"(could not prepare a preview: {e})\n" + json.dumps(a, ensure_ascii=False, default=str)[:1500]
        return {"tool": name, "tier": d.tier, "summary": summary, "reason": d.reason, "details": details[:8000],
                "details_kind": kind, "can_always": d.can_always_allow}

    @staticmethod
    def _tool_summary(name: str, args: dict) -> str:
        """One plain-language line from a BUILT-IN tool module's `describe_call(name, args)` (P5 PC tools).
        The full arguments are still shown under it, so a wrong summary cannot hide what will run."""
        try:
            from lucy.tools import registry
            spec = registry.default.get(name)
            mod = spec.module if spec is not None else ""
            if not mod.startswith("lucy.tools.") or mod.startswith("lucy.tools_user"):
                return ""
            import importlib
            fn = getattr(importlib.import_module(mod), "describe_call", None)
            return str(fn(name, args or {}))[:300] if callable(fn) else ""
        except Exception:                                            # noqa: BLE001
            return ""

    @staticmethod
    def _diff(old: str, new: str, path: str) -> str:
        lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), fromfile=path, tofile=path,
                                          lineterm="", n=3))
        if len(lines) > 400:
            lines = lines[:400] + [f"... ({len(lines) - 400} more diff lines)"]
        return "\n".join(lines) or "(no visible change)"
