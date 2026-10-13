"""Repeat rules for reminders and tasks.

A rule is a small JSON-able dict stored with the reminder:

    {"type": "daily",    "time": "09:00"}
    {"type": "weekdays", "time": "08:30"}                 Mon-Fri
    {"type": "weekly",   "time": "17:00", "days": [4]}    0 = Monday
    {"type": "monthly",  "time": "09:00", "day": 31}      short months use their last day, without drifting
    {"type": "yearly",   "time": "09:00", "month": 6, "day": 7}
    {"type": "interval", "every_minutes": 90, "anchor": <epoch seconds>}

`next_after(rule, after)` is a pure function of the rule and a timezone-aware `after`: the first
occurrence strictly later than `after`. Because it never depends on when the previous one actually
fired, a reminder that was missed (server off) cannot drift or pile up.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

REPEAT_CHOICES = ("none", "daily", "weekdays", "weekly", "monthly", "yearly")


def make_rule(repeat: str | None, every_minutes: int | None, first: datetime,
              min_interval_minutes: int = 10) -> dict | None:
    """Build the rule for a new reminder whose first occurrence is `first`. Raises ValueError on bad input."""
    r = (repeat or "none").strip().lower()
    if every_minutes not in (None, 0, ""):
        try:
            n = int(every_minutes)
        except (TypeError, ValueError):
            raise ValueError("every_minutes must be a whole number")
        if n < min_interval_minutes:
            raise ValueError(f"every_minutes must be at least {min_interval_minutes}")
        return {"type": "interval", "every_minutes": n, "anchor": first.timestamp()}
    if r in ("none", "", "once", "no", "never"):
        return None
    t = first.strftime("%H:%M")
    if r in ("daily", "every day", "everyday"):
        return {"type": "daily", "time": t}
    if r in ("weekdays", "weekday"):
        return {"type": "weekdays", "time": t}
    if r in ("weekly", "every week"):
        return {"type": "weekly", "time": t, "days": [first.weekday()]}
    if r in ("monthly", "every month"):
        return {"type": "monthly", "time": t, "day": first.day}
    if r in ("yearly", "annually", "every year", "annual"):
        return {"type": "yearly", "time": t, "month": first.month, "day": first.day}
    raise ValueError(f"repeat must be one of {', '.join(REPEAT_CHOICES)} (or give every_minutes)")


def _at(d: date, hh: int, mm: int, tz) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)


def next_after(rule: dict, after: datetime) -> datetime:
    tz = after.tzinfo
    kind = rule["type"]
    if kind == "interval":
        step = int(rule["every_minutes"]) * 60
        n = max(1, int((after.timestamp() - float(rule["anchor"])) // step) + 1)
        return datetime.fromtimestamp(float(rule["anchor"]) + n * step, tz)
    hh, mm = (int(x) for x in rule["time"].split(":"))
    if kind in ("daily", "weekdays", "weekly"):
        days = set(rule.get("days") or [])
        for i in range(0, 15):
            d = after.date() + timedelta(days=i)
            if kind == "weekdays" and d.weekday() >= 5:
                continue
            if kind == "weekly" and d.weekday() not in days:
                continue
            cand = _at(d, hh, mm, tz)
            if cand > after:
                return cand
    elif kind == "monthly":
        y, m = after.year, after.month
        for _ in range(15):
            d = date(y, m, min(int(rule["day"]), calendar.monthrange(y, m)[1]))
            cand = _at(d, hh, mm, tz)
            if cand > after:
                return cand
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    elif kind == "yearly":
        for y in range(after.year, after.year + 9):
            mon = int(rule["month"])
            d = date(y, mon, min(int(rule["day"]), calendar.monthrange(y, mon)[1]))
            cand = _at(d, hh, mm, tz)
            if cand > after:
                return cand
    raise ValueError(f"cannot compute next time for rule {rule}")


def describe_rule(rule: dict | None) -> str:
    if not rule:
        return ""
    k = rule["type"]
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    if k == "interval":
        n = int(rule["every_minutes"])
        return f"every {n // 60}h" + (f" {n % 60}m" if n % 60 else "") if n >= 60 else f"every {n}m"
    if k == "daily":
        return "daily"
    if k == "weekdays":
        return "weekdays"
    if k == "weekly":
        return "weekly on " + "/".join(names[d] for d in rule.get("days", []))
    if k == "monthly":
        return f"monthly on day {rule['day']}"
    if k == "yearly":
        return "every year"
    return k
