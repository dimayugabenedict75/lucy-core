"""Stella embed integration: fail-open to TF-IDF, vector freshness, all-or-nothing, negation guard, argv, isolation."""
import pathlib, sys
import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from lucy.rag.conductor import Conductor  # noqa: E402
from lucy.server.embed_manager import EmbedServer, DEFAULT_EMBED_CFG  # noqa: E402


class FakeServer:
    cfg = {"model": "m.snq"}


class FakeNeural:
    """Deterministic unit vectors from a word table; `down=True` mimics a dead server."""
    def __init__(self, table=None, down=False):
        self.table, self.down, self.calls = table or {}, down, 0
        self.mid = "fake#768"

    def model_id(self):
        return self.mid

    def embed_texts(self, texts, prefix, timeout=15.0, background=False):
        self.calls += 1
        if self.down:
            return None
        out = []
        for t in texts:
            t = t.lower()
            v = np.zeros(8, dtype=np.float32)
            for k, i in self.table.items():
                if k in t:
                    v[i] += 1
            if not v.any():
                v[7] = 1
            out.append(v / np.linalg.norm(v))
        return np.stack(out)


def mk(tmp_path, neural):
    return Conductor(store_path=tmp_path / "k.sqlite", json_path=tmp_path / "none.json", neural=neural)


def test_no_neural_in_tests_by_default(tmp_path):
    c = Conductor(store_path=tmp_path / "k.sqlite", json_path=tmp_path / "none.json")
    assert c.neural is None                                  # a custom store never starts llama-server


def test_server_down_falls_back_to_tfidf(tmp_path):
    c = mk(tmp_path, FakeNeural(down=True))
    c.add_to_knowledge("my gpu has 16 gb of vram")
    hits = c.search("how much vram does my gpu have")
    assert hits and hits[0]["text"].startswith("my gpu")


def test_neural_search_and_all_or_nothing(tmp_path):
    n = FakeNeural({"tea": 0, "gpu": 1})
    c = mk(tmp_path, n)
    c._bf_lock.acquire()                              # block the background backfill so the test controls the vectors
    a = c.add_to_knowledge("dima drinks green tea")["id"]
    b = c.add_to_knowledge("the gpu is an rx 9060 xt")["id"]
    c.store.put_vector(a, c.knowledge_store[0]["text"], n.mid, n.embed_texts(["tea"], "")[0])
    # only one of two memories has a vector -> must NOT use neural scores (floor is the TF-IDF one)
    _, floor = c._rank("tea", n.embed_texts(["tea"], ""))
    assert floor == c.MIN_RELEVANCE
    c.store.put_vector(b, c.knowledge_store[1]["text"], n.mid, n.embed_texts(["gpu"], "")[0])
    ranked, floor = c._rank("tea", n.embed_texts(["tea"], ""))
    assert floor == c.MIN_COSINE and c.knowledge_store[ranked[0][1]]["id"] == a


def test_stale_vector_after_edit_and_model_change(tmp_path):
    n = FakeNeural({"tea": 0})
    c = mk(tmp_path, n)
    i = c.add_to_knowledge("likes tea")["id"]
    doc = c.knowledge_store[0]
    c.store.put_vector(i, doc["text"], n.mid, n.embed_texts(["tea"], "")[0])
    assert len(c.store.fresh_vectors(n.mid, c.knowledge_store)) == 1
    assert len(c.store.fresh_vectors("other-model", c.knowledge_store)) == 0           # model changed
    c.add_to_knowledge("likes coffee now", replaces_id=i)                                # text edited
    assert len(c.store.fresh_vectors(n.mid, c.knowledge_store)) == 0


def test_negation_never_overwrites(tmp_path):
    n = FakeNeural({"tea": 0})                       # "likes tea" and "doesn't like tea" -> cosine 1.0
    c = mk(tmp_path, n)
    c.add_to_knowledge("dima likes tea")
    c.backfill_vectors()                             # duplicate check reads the stored vectors
    r = c.add_to_knowledge("dima does not like tea")
    assert r["status"] == "saved" and len(c.knowledge_store) == 2


def test_close_non_conflicting_text_is_merged(tmp_path):
    n = FakeNeural({"tea": 0})
    c = mk(tmp_path, n)
    c.add_to_knowledge("dima likes tea")
    c.backfill_vectors()
    r = c.add_to_knowledge("dima enjoys tea")
    assert r["status"] == "updated" and len(c.knowledge_store) == 1


def test_duplicate_check_without_vectors_uses_tfidf(tmp_path):
    c = mk(tmp_path, FakeNeural({"tea": 0}))
    c._bf_lock.acquire()                             # no vectors stored
    c.add_to_knowledge("my gpu is an rx 9060 xt with 16 gb")
    r = c.add_to_knowledge("my gpu is an rx 9060 xt with 16 gb vram")
    assert r["status"] in ("updated", "saved")       # no crash; TF-IDF decides


def test_lookup_does_not_hold_lock_during_network_call(tmp_path):
    import threading
    class Probe(FakeNeural):
        locked = None
        def embed_texts(self, texts, prefix, timeout=15.0, background=False):
            ok = c._lock.acquire(blocking=False)     # RLock from another thread would fail if it were held
            if ok: c._lock.release()
            Probe.locked = not ok
            return super().embed_texts(texts, prefix)
    n = Probe({"tea": 0})
    c = mk(tmp_path, n)
    c._bf_lock.acquire()
    c.add_to_knowledge("likes tea")
    box = {}
    t = threading.Thread(target=lambda: box.update(r=c.retrieve_context("tea")))
    t.start(); t.join(5)
    assert Probe.locked is False


def test_orphan_vectors_ignored_and_archive_roundtrip(tmp_path):
    n = FakeNeural({"tea": 0})
    c = mk(tmp_path, n)
    i = c.add_to_knowledge("likes tea")["id"]
    c.store.put_vector(i, "likes tea", n.mid, n.embed_texts(["tea"], "")[0])
    c.archive(i)
    assert c.store.fresh_vectors(n.mid, c.knowledge_store) == {}
    c.unarchive(i)
    assert len(c.store.fresh_vectors(n.mid, c.knowledge_store)) == 1
    c.forget(i)
    assert c.store.vacuum_vectors() == 1


def test_backfill(tmp_path):
    n = FakeNeural({"tea": 0})
    c = mk(tmp_path, n)
    c.add_to_knowledge("likes tea"); c.add_to_knowledge("owns a cat")
    c.backfill_vectors()
    assert len(c.store.fresh_vectors(n.mid, c.knowledge_store)) == 2


def test_build_argv(tmp_path):
    s = EmbedServer(runtime=tmp_path)
    a = s.build_argv()
    assert a[a.index("--port") + 1] == "8092" and "--mmproj" not in a
    assert a[a.index("-c") + 1] == a[a.index("-b") + 1] == a[a.index("-ub") + 1]
    s.cfg["use_mmproj"], s.cfg["image_tokens"] = True, 560
    a = s.build_argv()
    assert "--mmproj" in a and a[a.index("-c") + 1] == str(s.cfg["media_ctx"]) and "--image-max-tokens" in a


def test_isolated_from_chat_models():
    from lucy.server.llm_manager import llm
    assert DEFAULT_EMBED_CFG["port"] not in (m["port"] for m in llm.models.values())
    assert DEFAULT_EMBED_CFG["port"] not in (8080, 8081, 8090, 8091)
    assert "winget" in llm.cfg["llama_server"].lower() or llm.models_path.exists()


def test_message_timestamps_follow_pc_clock():
    src = (HERE.parent / "src" / "lucy" / "server" / "api.py").read_text(encoding="utf-8")
    assert "INSERT INTO messages (session_id, role, content, created_at, attachments)" in src   # never the column default
    assert "toISOString" not in (HERE.parent / "ui" / "chat.js").read_text(encoding="utf-8").replace("// \"YYYY", "")


def test_embed_server_cooldown_after_failed_start(tmp_path):
    s = EmbedServer(runtime=tmp_path)
    s.cfg["llama_server"] = str(tmp_path / "missing.exe")
    starts = []
    s._start_orig = s._start
    s.is_ready = lambda: False
    assert s.ensure_up() is False
    for _ in range(50):                               # background thread finishes quickly (files missing)
        if s._retry_at: break
        __import__("time").sleep(0.05)
    assert s._retry_at > 0
    t = s._retry_at
    s.ensure_up()
    assert s._retry_at == t                           # no new start attempt during the cooldown
