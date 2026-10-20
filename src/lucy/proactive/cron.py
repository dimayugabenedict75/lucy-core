"""Tiny cron parser for repeat rules: "m h dom mon dow" (+ @daily style aliases).

    "30 8 * * mon-fri"   weekdays 08:30        "*/15 * * * *"  every 15 minutes
    "0 9 1 * *"          9:00 on the 1st       "0 22 * * sun"  Sunday 22:00
    @hourly @daily @weekly @monthly @yearly

Names (jan..dec, sun..sat) work; dow 0 and 7 are both Sunday. As in classic cron, when BOTH day-of-month and
day-of-week are restricted a day matches if EITHER does. `next_after(expr, after)` is a pure function of the
expression and a timezone-aware `after` (first match strictly later), so a missed run can never drift or pile up.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@weekly": "0 0 * * 0",
           "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *"}
_MONTHS = {n: i for i, n in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"], 1)}
_DAYS = {n: i for i, n in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}


class CronError(ValueError):
    pass


def _num(tok: str, names: dict, lo: int, hi: int, label: str) -> int:
    t = tok.strip().lower()
    v = names[t[:3]] if t[:3] in names and t.isalpha() else None
    if v is None:
        try:
            v = int(t)
        except ValueError:
            raise CronError(f"'{tok}' is not a valid {label}")
    if not lo <= v <= hi:
        raise CronError(f"{label} {v} is out of range {lo}-{hi}")
    return v


def _field(text: str, lo: int, hi: int, label: str, names: dict | None = None) -> tuple[frozenset, bool]:
    """-> (allowed values, restricted?) ('*' and '*/1' are not restricted)."""
    names = names or {}
    out: set[int] = set()
    star = text.strip().startswith("*")                      # classic cron: a leading '*' means "not restricted"
    for part in text.split(","):
        if not part:
            raise CronError(f"empty item in {label}")
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            try:
                step = int(s)
            except ValueError:
                raise CronError(f"bad step '{s}' in {label}")
            if step < 1:
                raise CronError(f"step must be 1 or more in {label}")
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            a, b = _num(x, names, lo, hi, label), _num(y, names, lo, hi, label)
            if a > b:
                raise CronError(f"range {part} runs backwards in {label}")
        else:
            a = _num(part, names, lo, hi, label)
            b = hi if step > 1 else a
        out.update(range(a, b + 1, step))
    return frozenset(out), not star


def parse(expr: str) -> dict:
    """-> {"min","hour","dom","mon","dow" (sets), "dom_r","dow_r" (restricted flags)}. Raises CronError."""
    e = " ".join(str(expr or "").split())
    e = ALIASES.get(e.lower(), e)
    parts = e.split(" ")
    if len(parts) != 5:
        raise CronError("a cron rule has 5 fields: minute hour day-of-month month day-of-week (e.g. '30 8 * * mon-fri')")
    mi, _ = _field(parts[0], 0, 59, "minute")
    hh, _ = _field(parts[1], 0, 23, "hour")
    dom, dom_r = _field(parts[2], 1, 31, "day-of-month")
    mon, _ = _field(parts[3], 1, 12, "month", _MONTHS)
    dow_raw, dow_r = _field(parts[4], 0, 7, "day-of-week", _DAYS)
    dow = frozenset(0 if d == 7 else d for d in dow_raw)
    return {"min": mi, "hour": hh, "dom": dom, "mon": mon, "dow": dow, "dom_r": dom_r, "dow_r": dow_r}


def validate(expr: str) -> str:
    """Normalised expression text, or CronError."""
    parse(expr)
    e = " ".join(str(expr).split())
    return ALIASES.get(e.lower(), e)


def _day_ok(c: dict, d: date) -> bool:
    if d.month not in c["mon"]:
        return False
    dom_ok = d.day in c["dom"]
    dow_ok = ((d.weekday() + 1) % 7) in c["dow"]            # python Mon=0 -> cron Mon=1, Sun=0
    if c["dom_r"] and c["dow_r"]:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def next_after(expr: str, after: datetime) -> datetime:
    """First matching minute strictly after `after` (timezone of `after`). Searches up to 5 years ahead."""
    c = parse(expr)
    tz = after.tzinfo
    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    d = start.date()
    hours, mins = sorted(c["hour"]), sorted(c["min"])
    for _ in range(366 * 5):
        if _day_ok(c, d):
            for h in hours:
                for m in mins:
                    cand = datetime(d.year, d.month, d.day, h, m, tzinfo=tz)
                    if cand >= start:
                        return cand
        d += timedelta(days=1)
    raise CronError(f"'{expr}' never matches (check day-of-month and month, e.g. 31 in a short month)")


def describe(expr: str) -> str:
    e = " ".join(str(expr or "").split())
    try:
        c = parse(e)
    except CronError:
        return f"cron {e}"
    if e.lower() in ALIASES:
        return e.lower().lstrip("@")
    if len(c["hour"]) == 1 and len(c["min"]) == 1:
        hh, mm = next(iter(c["hour"])), next(iter(c["min"]))
        at = f"{hh:02d}:{mm:02d}"
        if not c["dom_r"] and not c["dow_r"] and len(c["mon"]) == 12:
            return f"daily at {at}"
        if not c["dom_r"] and len(c["mon"]) == 12:
            names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
            return f"{'/'.join(names[d] for d in sorted(c['dow']))} at {at}"
    return f"cron {e}"
