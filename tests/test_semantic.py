"""Search by meaning: indexing only when Stella is already up, filters, deleted items, model change, files, pictures."""
import pathlib
import sqlite3
import sys
import threading

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from lucy.memory.semantic import SemanticIndex, chunk_text  # noqa: E402

AXES = {"gpu": 0, "graphics": 0, "vulkan": 0, "driver": 0, "tea": 1, "coffee": 1, "drink": 1, "cat": 2, "kitten": 2,
        "invoice": 3, "budget": 3}


class Srv:
    def __init__(self, ready=True, mm=False):
        self.ready, self.cfg = ready, {"use_mmproj": mm}

    def is_ready(self):
        return self.ready


class FakeNeural:
    def __init__(self, ready=True, mm=False, mid="fake#8"):
        self.srv, self.mid, self.calls = Srv(ready, mm), mid, []

    def model_id(self):
        return self.mid

    def _vec(self, text):
        v = np.zeros(8, dtype=np.float32)
        for w in text.lower().replace("\n", " ").split():
            if w.strip(".,!?") in AXES:
                v[AXES[w.strip(".,!?")]] += 1
        if not v.any():
            v[7] = 1
        return v / np.linalg.norm(v)

    def embed_texts(self, texts, prefix, timeout=15.0, background=False):
        self.calls.append(("texts", background))
        if not self.srv.ready:
            return None
        return np.stack([self._vec(t) for t in texts])

    def embed_image(self, data, mime="image/png", timeout=60.0, background=True):
        self.calls.append(("image", background))
        if not self.srv.ready or not self.srv.cfg["use_mmproj"]:
            return None
        return self._vec("cat" if data == b"CAT" else "invoice")


def sessions(msgs):
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, title TEXT, updated_at TEXT)")
    c.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, created_at TEXT, attachments TEXT)")
    for sid, role, text in msgs:
        c.execute("INSERT OR IGNORE INTO sessions VALUES (?, ?, '2026-10-24 10:00:00')", (sid, f"title {sid}"))
        c.execute("INSERT INTO messages VALUES (?, ?, ?, '2026-10-24 10:00:00', NULL)", (sid, role, text))
    c.commit()
    return c


class FakeLib:
    """Just the parts SemanticIndex reads: lock, conn with library tables, abs_path."""
    def __init__(self, tmp_path, sconn, files):
        self.lock, self.conn, self.root = threading.RLock(), sconn, tmp_path
        sconn.execute("CREATE TABLE library_items (sha TEXT PRIMARY KEY, category TEXT, mime TEXT, size INTEGER, rel_path TEXT, created_at TEXT)")
        sconn.execute("CREATE TABLE library_refs (ref_id INTEGER PRIMARY KEY AUTOINCREMENT, sha TEXT, session_id TEXT, message_rowid INTEGER, origin TEXT, name TEXT, created_at TEXT)")
        for sha, name, data in files:
            (tmp_path / name).write_bytes(data)
            sconn.execute("INSERT INTO library_items VALUES (?, 'Documents', '', ?, ?, '')", (sha, len(data), name))
            sconn.execute("INSERT INTO library_refs(sha, session_id, message_rowid, origin, name, created_at) VALUES (?, 's1', 1, 'user', ?, '2026-10-24 10:00:00')", (sha, name))
        sconn.commit()

    def abs_path(self, rel):
        return self.root / rel


def mk(tmp_path, msgs, neural=None, files=()):
    sc = sessions(msgs)
    lib = FakeLib(tmp_path, sc, files) if files else None
    idx = SemanticIndex(tmp_path / "sem.sqlite", neural if neural is not None else FakeNeural(), sc, threading.RLock(), library=lib)
    return idx, sc


MSGS = [("s1", "user", "my graphics driver keeps crashing with vulkan on the new card"),
        ("s2", "assistant", "you drink green tea every morning before work starts"),
        ("s3", "user", "hi"),                                                    # too short: skipped, cursor still advances
        ("s3", "user", "remind me about the invoice budget for next quarter please")]


def test_indexes_new_messages_and_finds_them_by_meaning(tmp_path):
    idx, _ = mk(tmp_path, MSGS)
    done = idx.index_pending()
    assert done["msg"] == 3 and idx.counts() == {"msg": 3}
    hits = idx.search("gpu problems", limit=3)
    assert hits and hits[0]["session_id"] == "s1" and hits[0]["semantic"] and hits[0]["score"] > 0.9
    assert all(h["kind"] == "msg" for h in hits)
    assert idx.index_pending()["msg"] == 0                                          # nothing new: cursor moved on


def test_incremental_new_messages_only(tmp_path):
    idx, sc = mk(tmp_path, MSGS)
    idx.index_pending()
    sc.execute("INSERT INTO messages VALUES ('s9', 'user', 'my kitten knocked the coffee over again today', '2026-10-24 11:00:00', NULL)")
    sc.commit()
    assert idx.index_pending()["msg"] == 1 and idx.counts()["msg"] == 4


def test_indexing_never_starts_or_wakes_the_server(tmp_path):
    n = FakeNeural(ready=False)
    idx, _ = mk(tmp_path, MSGS, neural=n)
    assert idx.index_pending() == {"msg": 0, "file": 0, "image": 0}
    assert idx.counts() == {} and all(bg is True for _, bg in n.calls)               # background=True: no launch, no idle reset
    n.srv.ready = True
    assert idx.index_pending()["msg"] == 3                                           # picks up where it left off


def test_server_down_means_empty_search_not_an_error(tmp_path):
    n = FakeNeural()
    idx, _ = mk(tmp_path, MSGS, neural=n)
    idx.index_pending()
    n.srv.ready = False
    assert idx.search("gpu") == []


def test_filters_and_deleted_messages(tmp_path):
    idx, sc = mk(tmp_path, MSGS)
    idx.index_pending()
    assert idx.search("gpu", exclude_session="s1") == []
    assert idx.search("gpu", session_id="s2") == []
    assert idx.search("gpu", session_id="s1")
    sc.execute("DELETE FROM messages WHERE session_id = 's1'")
    sc.commit()
    assert idx.search("gpu") == []                                                   # deleted chats never come back


def test_unrelated_query_below_cutoff_returns_nothing(tmp_path):
    idx, _ = mk(tmp_path, MSGS)
    idx.index_pending()
    assert idx.search("zebra stripes", limit=5) == []


def test_model_change_rebuilds_the_index(tmp_path):
    n = FakeNeural(mid="old#8")
    idx, _ = mk(tmp_path, MSGS, neural=n)
    idx.index_pending()
    assert idx.counts()["msg"] == 3
    n.mid = "new#8"
    assert idx.search("gpu") == []                                                   # old vectors are ignored at once
    idx.index_pending()
    assert idx.counts()["msg"] == 3 and idx.search("gpu")


def test_files_are_chunked_indexed_and_resolved_to_their_chat(tmp_path):
    files = [("a" * 64, "gpu_notes.md", b"my vulkan driver notes: the graphics card crashes on startup"),
             ("b" * 64, "big.bin", b"\x00" * 10), ("c" * 64, "empty.txt", b"   ")]
    idx, _ = mk(tmp_path, MSGS, files=files)
    done = idx.index_pending()
    assert done["file"] == 1 and idx.counts().get("file") == 1
    hits = [h for h in idx.search("gpu driver", kinds=("file",))]
    assert hits and hits[0]["name"] == "gpu_notes.md" and hits[0]["session_id"] == "s1" and hits[0]["role"] == "file"
    assert idx.index_pending()["file"] == 0                                          # not re-embedded


def test_pictures_only_when_the_sensor_is_on(tmp_path):
    files = [("d" * 64, "cat.png", b"CAT"), ("e" * 64, "inv.png", b"INV")]
    idx, _ = mk(tmp_path, [], neural=FakeNeural(mm=False), files=files)
    assert idx.index_pending()["image"] == 0
    sub = tmp_path / "sensor_on"
    sub.mkdir()
    idx2, _ = mk(sub, [], neural=FakeNeural(mm=True), files=files)
    assert idx2.index_pending()["image"] == 2
    hits = idx2.search("a kitten", kinds=("image",), limit=2)
    assert hits and hits[0]["name"] == "cat.png"


def test_chunking():
    assert chunk_text("   ") == []
    c = chunk_text("word " * 1000)
    assert 1 < len(c) <= 40 and all(len(x) <= 900 for x in c)
