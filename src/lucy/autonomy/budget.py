"""Budgets for a job run: wall-clock seconds (waiting for the user does not count), tool calls, tokens.

Defaults are modest on purpose; a model-created job can ask for less or more but never beyond `jobs.max_budget`.
Tokens are an estimate of what the job read and wrote (size of the messages added to its conversation).
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT = {"seconds": 1200, "tool_calls": 60, "tokens": 80000}
CEILING = {"seconds": 4 * 3600, "tool_calls": 600, "tokens": 800000}
FLOOR = {"seconds": 60, "tool_calls": 3, "tokens": 4000}
LABELS = {"seconds": "time", "tool_calls": "tool calls", "tokens": "tokens"}


@dataclass
class Budget:
    seconds: int = DEFAULT["seconds"]
    tool_calls: int = DEFAULT["tool_calls"]
    tokens: int = DEFAULT["tokens"]

    @classmethod
    def make(cls, data: dict | None = None, defaults: dict | None = None, ceiling: dict | None = None) -> "Budget":
        """Clamp whatever was asked for into [FLOOR, ceiling]; unknown / bad values fall back to the defaults."""
        base = {**DEFAULT, **(defaults or {})}
        top = {**CEILING, **(ceiling or {})}
        out = {}
        for k in DEFAULT:
            raw = (data or {}).get(k, base[k])
            try:
                v = int(float(raw))
            except (TypeError, ValueError):
                v = int(base[k])
            out[k] = max(FLOOR[k], min(v, int(top[k])))
        return cls(**out)

    def to_dict(self) -> dict:
        return {"seconds": self.seconds, "tool_calls": self.tool_calls, "tokens": self.tokens}

    def exceeded(self, used: dict) -> str | None:
        """-> which limit is used up ('time' | 'tool calls' | 'tokens') or None."""
        for k in ("seconds", "tool_calls", "tokens"):
            if float(used.get(k, 0) or 0) >= getattr(self, k):
                return LABELS[k]
        return None

    def describe(self) -> str:
        return f"{max(1, self.seconds // 60)} min, {self.tool_calls} tool calls, {self.tokens // 1000}k tokens"
