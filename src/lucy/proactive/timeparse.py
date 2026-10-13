"""Turn the phrases a person (or a small model) writes into a concrete time.

    parse_when("in 20 minutes", now)       -> now + 20 min
    parse_when("tomorrow 9am", now)        -> tomorrow 09:00
    parse_when("friday 5pm", now)          -> next Friday 17:00
    parse_when("oct 20 8:30", now)         -> next Oct 20 08:30
    parse_when("2026-10-14 09:00", now)    -> exactly that, in `now`'s timezone
    parse_when("6:30", now)                -> the soonest future 6:30 (am or pm)

Pure functions, no model, no clock of their own: `now` is always passed in (a timezone-aware datetime).
Returns None when the text cannot be understood - callers must then ask, never guess.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone

_WORDNUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
            "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
            "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60}
_UNIT_S = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
           "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
           "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
           "d": 86400, "day": 86400, "days": 86400, "w": 604800, "week": 604800, "weeks": 604800}
_WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tues": 1, "tue": 1, "wednesday": 2, "wed": 2,
             "thursday": 3, "thurs": 3, "thur": 3, "thu": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
             "sunday": 6, "sun": 6}
_MONTHS = {"january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
           "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
           "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12}
_DAYPART = {"morning": (9, 0), "afternoon": (15, 0), "evening": (18, 0), "night": (20, 0), "tonight": (20, 0)}
_FILLER = {"please", "sharp", "exactly", "at", "on", "by", "around", "about", "the", "of", "oclock", "next", "this",
           "coming", "and", "then", "me", "to"}

_NUM = r"\d+(?:\.\d+)?|" + "|".join(sorted(_WORDNUM, key=len, reverse=True))
_UNIT = "|".join(sorted(_UNIT_S, key=len, reverse=True))
_DUR_TOKEN = re.compile(rf"(?P<n>{_NUM})\s*(?P<u>{_UNIT})(?![a-z])")
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DAY_RE = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))


def _norm(text: str) -> str:
    s = str(text or "").lower().strip()
    s = re.sub(r"\ba\.m\.?", "am", s)
    s = re.sub(r"\bp\.m\.?", "pm", s)
    s = s.replace("o'clock", "oclock").replace("’", "'")
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    s = re.sub(r"[,;]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .!")
    return s


# ------------------------------------------------------------------------------------ durations
def parse_duration(text: str) -> float | None:
    """'20 minutes', '2h', '1 hour 30 minutes', 'half an hour', 'an hour and a half' -> seconds (None if not a duration)."""
    s = _norm(text)
    s = re.sub(r"^in\s+", "", s)
    s = re.sub(r"\s+from now$", "", s)
    if not s:
        return None
    m = re.fullmatch(r"half(?: an?)? (hour|day|week)s?", s)
    if m:
        return _UNIT_S[m.group(1)] / 2
    m = re.fullmatch(rf"({_NUM}) (hour|day|week)s? and a half", s)
    if m:
        n = _WORDNUM.get(m.group(1)) or float(m.group(1))
        return (n + 0.5) * _UNIT_S[m.group(2)]
    total, pos = 0.0, 0
    rest = s.replace(" and ", " ")
    rest = re.sub(r"\s+", " ", rest).strip()
    while pos < len(rest):
        m = _DUR_TOKEN.match(rest, pos)
        if not m:
            return None
        n = _WORDNUM.get(m.group("n"))
        n = float(m.group("n")) if n is None else n
        total += n * _UNIT_S[m.group("u")]
        pos = m.end()
        while pos < len(rest) and rest[pos] == " ":
            pos += 1
    return total if total > 0 else None


# ------------------------------------------------------------------------------------ absolute times
_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[t ](\d{1,2}):(\d{2})(?::(\d{2}))?)?\s*(z|[+-]\d{2}:?\d{2})?$")


def _iso(s: str, now: datetime) -> datetime | None:
    m = _ISO.match(s)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh = int(m.group(4)) if m.group(4) is not None else 9
    mi = int(m.group(5)) if m.group(5) is not None else 0
    ss = int(m.group(6)) if m.group(6) else 0
    try:
        naive = datetime(y, mo, d, hh, mi, ss)
    except ValueError:
        return None
    off = m.group(7)
    if off:
        if off == "z":
            tz = timezone.utc
        else:
            sign = -1 if off[0] == "-" else 1
            digits = off[1:].replace(":", "")
            tz = timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
        return naive.replace(tzinfo=tz).astimezone(now.tzinfo)
    return naive.replace(tzinfo=now.tzinfo)


def _relative(s: str, now: datetime) -> datetime | None:
    explicit = s.startswith("in ") or s.endswith(" from now")
    # a bare duration ("20 minutes", "2h") is accepted too, but "5pm"/"9" are not durations
    secs = parse_duration(s)
    if secs is None:
        return None
    if not explicit and not re.search(rf"(?<![a-z])(?:{_UNIT})(?![a-z])", s):
        return None
    return (now + timedelta(seconds=secs)).replace(microsecond=0)


def _take(pattern: str, s: str):
    m = re.search(pattern, s)
    if not m:
        return None, s
    return m, (s[:m.start()] + " " + s[m.end():]).strip()


def _clock(h: int, mi: int, mer: str | None) -> tuple[int, int] | None:
    if mi > 59:
        return None
    if mer:
        if not 1 <= h <= 12:
            return None
        h = (h % 12) + (12 if mer == "pm" else 0)
    elif h > 23:
        return None
    return h, mi


def _absolute(s: str, now: datetime) -> datetime | None:
    day: date | None = None
    part: str | None = None
    is_today_word = False
    tz = now.tzinfo
    rest = s

    m, rest = _take(r"\bday after tomorrow\b", rest)
    if m:
        day = (now + timedelta(days=2)).date()
    if day is None:
        m, rest = _take(r"\btomorrow\b", rest)
        if m:
            day = (now + timedelta(days=1)).date()
    if day is None:
        m, rest = _take(r"\btoday\b", rest)
        if m:
            day, is_today_word = now.date(), True
    if day is None:                                          # "in 3 days 9am", "in 2 weeks at noon"
        m, rest = _take(rf"\bin ({_NUM}) (days?|weeks?)\b", rest)
        if m:
            n = _WORDNUM.get(m.group(1))
            n = float(m.group(1)) if n is None else n
            if n != int(n) or n < 1:
                return None
            day = (now + timedelta(days=int(n) * (7 if m.group(2).startswith("week") else 1))).date()
    m, rest = _take(r"\b(tonight)\b", rest)
    if m:
        part = "tonight"
        day = day or now.date()
    if part is None:
        m, rest = _take(r"\b(morning|afternoon|evening|night)\b", rest)
        if m:
            part = m.group(1)
    if day is None:
        m, rest = _take(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", rest)
        if m:
            try:
                day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
    if day is None:
        m, rest = _take(rf"\b({_MONTH_RE})\.? (\d{{1,2}})(?: (\d{{4}}))?\b", rest)
        if m:
            mon, dd, yy = _MONTHS[m.group(1)], int(m.group(2)), m.group(3)
        else:
            m, rest = _take(rf"\b(\d{{1,2}}) ({_MONTH_RE})\.?(?: (\d{{4}}))?\b", rest)
            if m:
                mon, dd, yy = _MONTHS[m.group(2)], int(m.group(1)), m.group(3)
        if m:
            try:
                if yy:
                    day = date(int(yy), mon, dd)
                else:
                    day = date(now.year, mon, dd)
                    if datetime.combine(day, time(23, 59), tz) < now:
                        day = date(now.year + 1, mon, dd)
            except ValueError:
                return None
    weekday_wanted: int | None = None
    if day is None:
        m, rest = _take(rf"\b({_DAY_RE})\b", rest)
        if m:
            weekday_wanted = _WEEKDAYS[m.group(1)]

    # leftover: a time of day
    rest = " ".join(w for w in rest.split() if w not in _FILLER)
    hm: tuple[int, int] | None = None
    ambiguous_hour: tuple[int, int] | None = None          # 1-11 with no am/pm
    if rest in ("noon", "midday"):
        hm = (12, 0)
    elif rest == "midnight":
        hm = (0, 0)
        if day is None or day == now.date():             # "midnight" means the one that is coming
            day = now.date() + timedelta(days=1)
    elif rest in ("eod", "end of day"):
        hm = (17, 0)
    elif rest:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})\s*(am|pm)?", rest) or re.fullmatch(r"(\d{1,2})()\s*(am|pm)", rest)
        if m:
            h, mi, mer = int(m.group(1)), int(m.group(2) or 0), m.group(3)
            hm = _clock(h, mi, mer)
            if hm is None:
                return None
            if not mer and 1 <= h <= 11 and not rest.startswith("0"):
                ambiguous_hour = (h, mi)
        elif re.fullmatch(r"\d{1,2}", rest):
            h = int(rest)
            if h == 0 or h > 23 or rest.startswith("0"):
                hm = _clock(h, 0, None)
            elif h == 12:
                hm = (12, 0)
            elif h <= 11:
                ambiguous_hour = (h, 0)
            else:
                hm = (h, 0)
            if h > 23:
                return None
        elif re.fullmatch(r"\d{4}", rest) and int(rest[:2]) <= 23 and int(rest[2:]) <= 59:
            hm = (int(rest[:2]), int(rest[2:]))
        else:
            return None                                    # unknown words: don't guess

    if hm is None and ambiguous_hour is None:
        if part:
            hm = _DAYPART[part]
        elif day is not None or weekday_wanted is not None:
            if is_today_word and day == now.date() and weekday_wanted is None:
                return None                                # "today" with no time of day
            hm = (9, 0)
        else:
            return None
    elif part and ambiguous_hour:                          # "tomorrow evening 7"
        h, mi = ambiguous_hour
        hm = (h + 12 if part in ("afternoon", "evening", "night", "tonight") and h < 12 else h, mi)
        ambiguous_hour = None

    # ---- put it together
    if weekday_wanted is not None:
        delta = (weekday_wanted - now.weekday()) % 7
        if ambiguous_hour:
            h, mi = ambiguous_hour
            hm = (h + 12 if h <= 6 else h, mi)
            ambiguous_hour = None
        cand = datetime.combine(now.date() + timedelta(days=delta), time(*hm), tz)
        if delta == 0 and cand <= now:
            cand += timedelta(days=7)
        return cand
    if day is not None:
        if ambiguous_hour:
            h, mi = ambiguous_hour
            hm = (h + 12 if h <= 6 else h, mi)
        return datetime.combine(day, time(*hm), tz)
    # time only: the soonest future occurrence
    options = [ambiguous_hour, (ambiguous_hour[0] + 12, ambiguous_hour[1])] if ambiguous_hour else [hm]
    cands = []
    for h, mi in options:
        for add in (0, 1):
            cands.append(datetime.combine(now.date() + timedelta(days=add), time(h, mi), tz))
    future = [c for c in cands if c > now]
    return min(future) if future else None


def parse_when(text: str, now: datetime) -> datetime | None:
    """See module docstring. `now` must be timezone-aware."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    s = _norm(text)
    if not s:
        return None
    return _iso(s, now) or _relative(s, now) or _absolute(s, now)


# ------------------------------------------------------------------------------------ describing
def describe(dt: datetime, now: datetime) -> str:
    """'today 17:00' / 'tomorrow 09:00' / 'Fri 17 Oct 09:00' (+ year if it is not this year)."""
    days = (dt.date() - now.date()).days
    hhmm = dt.strftime("%H:%M")
    if days == 0:
        return f"today {hhmm}"
    if days == 1:
        return f"tomorrow {hhmm}"
    if 1 < days < 7:
        return f"{dt.strftime('%a')} {hhmm}"
    year = f" {dt.year}" if dt.year != now.year else ""
    return f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b')}{year} {hhmm}"


def humanize_delta(seconds: float) -> str:
    """90 -> '1m', 7500 -> '2h 5m', 200000 -> '2d 7h'."""
    seconds = int(abs(seconds))
    if seconds < 60:
        return f"{seconds}s"
    mins = seconds // 60
    if mins < 60:
        return f"{mins}m"
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"
