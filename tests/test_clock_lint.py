"""One clock: the PC's local time. Fails if UTC helpers or UTC SQL defaults come back (that was the 8-hour bug)."""
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from lucy import clock  # noqa: E402

SKIP = {"install_proactive.py"}


def test_no_utc_helpers_in_python_code():
    bad = []
    for f in (ROOT / "src" / "lucy").rglob("*.py"):
        if f.name in SKIP or "__pycache__" in f.parts:
            continue
        for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"utcnow\(|time\.gmtime\(|datetime\.now\(\s*(datetime\.)?timezone\.utc", code):
                bad.append(f"{f.relative_to(ROOT)}:{n}")
            if re.search(r"strftime\('%Y-%m-%d %H:%M:%S',\s*'now'\s*\)", code):         # SQLite default = UTC
                bad.append(f"{f.relative_to(ROOT)}:{n} (SQL 'now' without 'localtime')")
    assert not bad, bad


def test_ui_does_not_stamp_times_in_utc():
    for name in ("chat.js", "health.html", "memory.html"):
        text = (ROOT / "ui" / name).read_text(encoding="utf-8")
        assert "toISOString" not in text.replace("(not UTC", ""), name


def test_clock_roundtrip_and_label():
    s = clock.now_str(1_700_000_000)
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", s)
    assert abs(clock.parse(s) - 1_700_000_000) < 1
    assert clock.parse("garbage") is None and clock.parse(None) is None
    assert re.search(r"\(UTC[+-]\d\d:\d\d\)$", clock.tz_label())
    assert "localtime" in clock.SQL_NOW
