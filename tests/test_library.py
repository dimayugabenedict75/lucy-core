"""Tests for the Library cache (lucy/server/library.py). Run: python -m pytest tests/test_library.py"""
import sqlite3
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from lucy.server.library import Library, categorize


@pytest.fixture()
def env(tmp_path):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, title TEXT)")
    conn.executemany("INSERT INTO sessions VALUES (?, ?)", [("s1", "First"), ("s2", "Second")])
    stickers = tmp_path / "assets" / "stickers"
    (stickers / "User").mkdir(parents=True)
    (stickers / "User" / "7.png").write_bytes(b"\x89PNG-sticker-seven")
    cache = tmp_path / "cache"
    lib = Library(cache, conn, threading.RLock(), stickers)
    return lib, cache, stickers, tmp_path


def test_cache_folder_is_lazy(env):
    lib, cache, *_ = env
    assert not cache.exists()                      # nothing cached yet -> nothing created
    assert lib.ingest_bytes(b"hello", "a.txt")
    assert (cache / "Documents").is_dir()
    assert (cache / ".gitignore").read_text().count("*") >= 1
    assert not (cache / "Images").exists()         # only the folders that are needed


def test_categories():
    assert categorize("x.PNG") == "Images" and categorize("a.py") == "Code"
    assert categorize("r.pdf") == "Documents" and categorize("v.mp4") == "Video"
    assert categorize("Dockerfile") == "Code" and categorize("weird.bin") == "Other"


def test_dedupe_same_content(env):
    lib, cache, *_ = env
    a = lib.ingest_bytes(b"same bytes", "one.txt")
    b = lib.ingest_bytes(b"same bytes", "two.txt")
    assert a["sha"] == b["sha"]
    assert len(list((cache / "Documents").iterdir())) == 1


def test_stickers_are_never_cached(env):
    lib, cache, stickers, _ = env
    assert lib.ingest_bytes(b"\x89PNG-sticker-seven", "sticker_7.png") is None      # same bytes as a known sticker
    assert lib.ingest_path(stickers / "User" / "7.png") is None                      # path inside assets/stickers
    assert lib.is_sticker_path(stickers / "User" / "7.png")
    assert not cache.exists()


def test_ingest_path_is_a_snapshot(env):
    lib, _, _, tmp = env
    f = tmp / "report.py"
    f.write_text("v1")
    a = lib.ingest_path(f)
    f.write_text("v2")                                  # edited afterwards
    b = lib.ingest_path(f)
    assert a["sha"] != b["sha"]
    assert lib.get(a["sha"])["path"].read_text() == "v1"


def test_refs_listing_filters_and_delete(env):
    lib, *_ = env
    img = lib.ingest_bytes(b"imgdata", "shot.png")
    code = lib.ingest_bytes(b"print(1)", "run.py")
    lib.add_refs("s1", 1, "user", [img])
    lib.add_refs("s2", 5, "assistant", [code, img])
    allr = lib.list_items()
    assert allr["total"] == 2 and allr["counts"]["_all"] == 2
    assert {i["kind"] for i in allr["items"]} == {"images", "code"}
    assert lib.list_items(categories=["Code"])["items"][0]["name"] == "run.py"
    assert lib.list_items(origin="user")["total"] == 1
    assert lib.list_items(session_id="s2")["total"] == 2
    assert lib.list_items(q="shot")["total"] == 1
    img_item = [i for i in allr["items"] if i["kind"] == "images"][0]
    assert img_item["refs"] == 2                        # one file, used in two messages
    # deleting session s2 keeps `img` (still used by s1), removes `code`
    lib.delete_session("s2")
    assert lib.get(img["sha"]) and lib.get(code["sha"]) is None
    assert lib.delete_item(img["sha"]) and lib.get(img["sha"]) is None


def test_reconcile_heals(env):
    lib, cache, *_ = env
    a = lib.ingest_bytes(b"keep", "keep.txt"); lib.add_refs("s1", 1, "user", [a])
    b = lib.ingest_bytes(b"lose", "lose.txt"); lib.add_refs("s1", 2, "user", [b])
    lib.get(b["sha"])["path"].unlink()                  # file vanished (e.g. cache folder cleaned)
    (cache / "Documents" / "stray.txt").write_text("nobody knows me")
    r = lib.reconcile()
    assert r["missing_records"] == 1 and r["stray_files"] == 1
    assert lib.get(a["sha"]) and not lib.get(b["sha"])
    assert lib.existing_shas([a["sha"], b["sha"]]) == {a["sha"]}


def test_thumbnail(env):
    PIL = pytest.importorskip("PIL.Image")
    import io
    buf = io.BytesIO(); PIL.new("RGB", (900, 600), "red").save(buf, "PNG")
    lib, cache, *_ = env
    a = lib.ingest_bytes(buf.getvalue(), "big.png")
    t = lib.thumbnail(a["sha"])
    assert t and t.suffix == ".webp" and t.parent.name == "Thumbnails"
    assert lib.thumbnail(lib.ingest_bytes(b"x", "n.txt")["sha"]) is None   # not an image


def test_traversal_is_rejected(env):
    lib, *_ = env
    assert lib.get("../../etc/passwd") is None
    a = lib.ingest_bytes(b"x", "../../evil.txt")
    assert ".." not in str(lib.get(a["sha"])["path"].relative_to(lib.dir.resolve()))
