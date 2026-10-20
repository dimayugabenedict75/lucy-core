"""Item 4 - context + usage meter numbers (the UI draws the bar)."""

from __future__ import annotations


def meter(tokens: int, limit: int, reserve: int = 0) -> dict:
    limit = max(1, int(limit or 1))
    tokens = max(0, int(tokens))
    pct = round(100 * tokens / limit, 1)
    level = "ok" if pct < 70 else ("warn" if pct < 90 else "full")
    return {"tokens": tokens, "limit": limit, "pct": min(pct, 100.0), "level": level, "left": max(0, limit - tokens - reserve)}


def human(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)
