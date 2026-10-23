"""Lucy follows the PC's own date / time / timezone. There is no timezone setting, no tzdata, no timeapi.io."""
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "src"))
import pytest  # noqa: E402


def test_no_code_path_resolves_timezones_over_the_network_or_through_zoneinfo():
    for f in (ROOT / "src" / "lucy").rglob("*.py"):
        if f.name == "install_proactive.py":           # old one-off installer, not loaded by the app
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        assert "timeapi" not in text.lower(), f
        assert "zoneinfo" not in text and "tz_config" not in text, f


def test_ui_has_no_timezone_selector():
    html = (ROOT / "ui" / "chat.html").read_text(encoding="utf-8")
    js = (ROOT / "ui" / "chat.js").read_text(encoding="utf-8")
    assert "timezoneSelect" not in html + js and "/api/settings/timezone" not in js


def test_tzdata_is_no_longer_required():
    assert not re.search(r"^\s*tzdata\b", (ROOT / "requirements.txt").read_text(encoding="utf-8"), re.M)


def test_timezone_endpoints_are_gone_and_stored_times_use_the_pc_clock():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from lucy.server import api
    h = {"X-API-Key": api.DEV_API_KEY}
    c = TestClient(api.app)
    assert c.get("/api/settings/timezone", headers=h).status_code in (404, 405)
    assert c.post("/api/settings/timezone", headers=h, json={"timezone": "utc"}).status_code in (404, 405)
    assert api._get_tz_modifier() == "localtime"
