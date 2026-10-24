"""One clock for the whole harness: this PC's own local date / time / timezone.

Everything that stores or shows a time goes through here (or through SQLite's 'localtime' modifier, which is the same
clock). There is NO timezone setting on purpose (tests/test_no_timezone_setting.py); change the Windows clock and Lucy
follows. tests/test_clock_lint.py fails if UTC helpers sneak back into the code.
"""
from __future__ import annotations

import time

FMT = "%Y-%m-%d %H:%M:%S"
# for SQL defaults / UPDATEs: same string as now_str(), computed by SQLite
SQL_NOW = "strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')"


def now_ts() -> float:
    """Seconds since the epoch (timezone-free; use for durations and ordering)."""
    return time.time()


def now_str(ts: float | None = None) -> str:
    """'YYYY-MM-DD HH:MM:SS' on the PC's clock."""
    return time.strftime(FMT, time.localtime(ts))


def parse(s: str | None) -> float | None:
    """Inverse of now_str(): epoch seconds, or None if `s` isn't in that format."""
    try:
        return time.mktime(time.strptime(str(s), FMT))
    except (TypeError, ValueError, OverflowError):
        return None


def utc_offset_hours() -> float:
    """The PC's current offset from UTC in hours (+8.0, -5.0, 5.5 ...)."""
    return time.localtime().tm_gmtoff / 3600.0


def tz_label() -> str:
    """e.g. 'PST (UTC-08:00)': shown in the health panel so a wrong PC timezone is easy to spot."""
    lt = time.localtime()
    off = lt.tm_gmtoff // 60
    sign = "+" if off >= 0 else "-"
    return f"{time.tzname[1 if lt.tm_isdst > 0 else 0]} (UTC{sign}{abs(off) // 60:02d}:{abs(off) % 60:02d})"
