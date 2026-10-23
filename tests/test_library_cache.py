"""P9 item 9: Library pins, size cap, cleanup. Run: python -m pytest tests/test_library_cache.py"""
import sqlite3
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from lucy.server.library import Library


@pytest.fixture()
def env(tmp_path):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('s1', 'First')")
    stickers = tmp_path / "assets" / "stickers"
    stickers.mkdir(parents=True)
    lib = Library(tmp_path / "cache", conn, threading.RLock(), stickers)
    return lib, conn


def _add(lib, name, size, n=1):
    att = lib.ingest_bytes((name.encode() * size)[:size], name, "user")
    lib.add_refs("s1", n, "user", [att])
    return att["sha"]


def test_pin_survives_cleanup_and_shows_in_list(env):
    lib, _ = env
    a, b = _add(lib, "a.txt", 100, 1), _add(lib, "b.txt", 100, 2)
    assert lib.set_pin(a, True)
    items = {i["sha"]: i for i in lib.list_items()["items"]}
    assert items[a]["pinned"] is True and items[b]["pinned"] is False
    assert lib.list_items()["items"][0]["sha"] == a                     # pinned first
    assert [i["sha"] for i in lib.list_items(pinned_only=True)["items"]] == [a]
    assert lib.cleanup_preview()["files"] == 1
    assert lib.cleanup()["removed"] == 1
    assert lib.get(a) is not None and lib.get(b) is None
    assert lib.stats()["pinned"] == 1


def test_unpin_makes_it_removable_again(env):
    lib, _ = env
    a = _add(lib, "a.txt", 50)
    lib.set_pin(a, True)
    lib.set_pin(a, False)
    assert lib.cleanup()["removed"] == 1


def test_cap_removes_oldest_unpinned_first(env):
    lib, _ = env
    old, mid, new = _add(lib, "old.txt", 400, 1), _add(lib, "mid.txt", 400, 2), _add(lib, "new.txt", 400, 3)
    lib.set_pin(old, True)
    res = lib.enforce_cap(900)                    # 1200 bytes total -> one file must go; the pinned one stays
    assert res["removed"] == 1
    assert lib.get(old) and lib.get(new) and lib.get(mid) is None
    assert lib.enforce_cap(0) == {"removed": 0, "bytes": 0}      # 0 = no cap


def test_cap_setting_is_saved(env):
    lib, _ = env
    assert lib.cap_bytes() > 0
    lib.set_cap_bytes(5 * 1024 * 1024)
    assert lib.cap_bytes() == 5 * 1024 * 1024
    assert lib.stats()["cap_bytes"] == 5 * 1024 * 1024


def test_kept_files_are_not_orphans(env):
    lib, _ = env
    att = lib.ingest_bytes(b"project file", "p.txt", "user")       # no message references it
    assert lib.keep(att["sha"], "project:1")
    assert lib.gc_orphans() == 0 and lib.get(att["sha"])
    lib.unkeep(att["sha"], "project:1")                             # nothing keeps it any more
    assert lib.get(att["sha"]) is None


def test_cleanup_by_category(env):
    lib, _ = env
    t = _add(lib, "doc.txt", 20, 1)
    img = lib.ingest_bytes(b"\x89PNG....", "pic.png", "user")
    lib.add_refs("s1", 2, "user", [img])
    assert lib.cleanup_preview(categories=["Images"])["files"] == 1
    lib.cleanup(categories=["Images"])
    assert lib.get(img["sha"]) is None and lib.get(t)
