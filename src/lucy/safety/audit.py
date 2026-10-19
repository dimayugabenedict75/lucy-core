"""Append-only audit log: one JSON line per tool call (runtime/logs/audit.jsonl).

Fields: ts, session, tool, tier, decision (auto|approved|denied|refused), by (policy|user|session_rule|timeout|
no_approver|hard_rule|direct), reason, args (shortened, secrets masked), ok, ms, result (first chars).
The agent's file tools cannot write this file (policy: Lucy safety state) and nothing in Lucy ever rewrites it.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

_SECRET_KEY = re.compile(r"(?i)(pass(word)?|secret|token|api[_-]?key|authorization|cookie)")
_SECRET_VAL = re.compile(r"(?i)\b(sk-[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9]{20,}|AIza[0-9a-z_-]{30,}|ya29\.[0-9a-z_-]{20,})")


def redact(v: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "..."
    if isinstance(v, dict):
        return {k: ("***" if _SECRET_KEY.search(str(k)) else redact(x, depth + 1)) for k, x in list(v.items())[:30]}
    if isinstance(v, (list, tuple)):
        return [redact(x, depth + 1) for x in list(v)[:30]]
    if isinstance(v, str):
        s = _SECRET_VAL.sub("***", v)
        return s if len(s) <= 400 else s[:400] + f"...(+{len(s) - 400} chars)"
    return v


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def write(self, **entry: Any) -> None:
        entry = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}
        if "args" in entry:
            entry["args"] = redact(entry["args"])
        line = json.dumps(entry, ensure_ascii=False, default=str)
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:      # append mode only: never truncates
                    f.write(line + "\n")
        except OSError:
            pass                                                         # auditing must never break a tool call

    def tail(self, n: int = 50) -> list:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                lines = f.readlines()[-n:]
        except OSError:
            return []
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out
