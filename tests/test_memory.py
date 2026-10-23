"""P6: memory on SQLite + FTS5 (migration keeps the JSON), consolidation (merge / flag / decay / archive), history search."""
import json
import pathlib
import sqlite3
import sys
import tempfile
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

import pytest  # noqa: E402
from lucy.rag.conductor import Conductor  # noqa: E402
from lucy.memory import consolidate as C  # noqa: E402
from lucy.memory import history  # noqa: E402
from lucy.memory.manager import MemoryManager  # noqa: E402


def mk(tmp_path, legacy=None):
    d = pathlib.Path(tmp_path)
    jp = d / "knowledge_store.json"
    if legacy is not None:
        jp.write_text(json.dumps(legacy), encoding="utf-8")
    return Conductor(store_path=d / "k.sqlite", json_path=jp), jp


def ago(days):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))


def put(c, text, **kw):
    """Insert a row directly (bypasses add_to_knowledge's own duplicate check) so consolidation has work to do."""
    d = {"id": c._next_id(), "text": text, "category": "fact", "importance": 3, "created": ago(1), "updated": ago(1),
         "last_used": None, "uses": 0, "source": None, "flag": None, "merged_into": None, "decayed_at": None}
    d.update(kw)
    c.knowledge_store.append(d)
    c._save()
    return d["id"]


def by_id(c, i):
    return next((d for d in c.all() if d["id"] == i), None)


# ------------------------------------------------------------------------------------------ migration + storage
def test_json_is_imported_once_and_never_touched(tmp_path):
    legacy = [{"text": "Benny likes tea"}, {"id": 9, "text": "Thursday is rest day", "category": "preference", "importance": 4}]
    jp = pathlib.Path(tmp_path) / "knowledge_store.json"
    jp.write_text(json.dumps(legacy), encoding="utf-8")
    raw = jp.read_bytes()
    c, _ = mk(tmp_path)
    assert {d["text"] for d in c.all()} == {"Benny likes tea", "Thursday is rest day"}
    assert by_id(c, 9)["category"] == "preference" and by_id(c, 9)["importance"] == 4
    assert jp.read_bytes() == raw and (jp.with_name(jp.name + ".pre-sqlite.bak")).read_bytes() == raw
    tea = next(d["id"] for d in c.all() if d["text"] == "Benny likes tea")
    assert tea == 10                                                     # ids continue after the highest old id
    assert c.forget(tea)
    c2, _ = mk(tmp_path)                                                 # reopen: NOT re-imported, so not resurrected
    assert [d["id"] for d in c2.all()] == [9]


def test_survives_reopen_updates_and_forget_really_deletes(tmp_path):
    c, _ = mk(tmp_path)
    r = c.add_to_knowledge("Lucy runs on an AMD GPU", category="fact", importance=2)
    c.add_to_knowledge("Lucy runs on an AMD RX GPU card", importance=4, replaces_id=r["id"])
    c2, _ = mk(tmp_path)
    assert by_id(c2, r["id"])["text"].endswith("card") and by_id(c2, r["id"])["importance"] == 4
    assert c2.forget(r["id"]) and mk(tmp_path)[0].all() == []
    assert c2.store.fts_search("GPU") == []                              # the index forgot it too


def test_ids_of_archived_memories_are_never_reused(tmp_path):
    c, _ = mk(tmp_path)
    a = c.add_to_knowledge("First memory about apples")["id"]
    assert c.archive(a)
    b = c.add_to_knowledge("Second memory about pears")["id"]
    assert b > a and [d["id"] for d in c.archived()] == [a]
    assert c.unarchive(a) and {d["id"] for d in c.all()} == {a, b}


def test_fts_keeps_in_step_and_fills_in_when_tfidf_finds_nothing(tmp_path):
    c, _ = mk(tmp_path)
    i = c.add_to_knowledge("Benny enjoys oolong in the evening")["id"]
    assert c.store.fts_search("oolo") == [i]                             # prefix match, the TF-IDF rank has no such word
    assert [d["id"] for d in c.search("oolo", 3)] == [i]
    c.add_to_knowledge("Benny enjoys jasmine in the evening", replaces_id=i)
    assert c.store.fts_search("oolong") == [] and c.store.fts_search("jasmine") == [i]


def test_old_search_behaviour_is_unchanged_for_normal_queries(tmp_path):
    c, _ = mk(tmp_path)
    c.add_to_knowledge("Benny likes tea")
    c.add_to_knowledge("Lucy runs on an AMD GPU")
    assert c.search("gpu", 3)[0]["text"] == "Lucy runs on an AMD GPU"
    assert "- Benny likes tea" in c.retrieve_context("what about tea")


# ------------------------------------------------------------------------------------------ consolidation
def test_near_duplicates_merge_and_the_loser_is_archived_not_deleted(tmp_path):
    c, _ = mk(tmp_path)
    a = put(c, "Benny likes his tea hot in the morning", importance=2, uses=2)
    b = put(c, "Benny likes his tea hot in the morning mostly", importance=4, uses=3)
    rep = C.consolidate(c)
    assert rep["merged"] == [(a, b)]
    assert by_id(c, a) is None and by_id(c, b)["importance"] == 4 and by_id(c, b)["uses"] == 5
    assert c.archived()[0]["id"] == a and c.archived()[0]["merged_into"] == b
    assert c.unarchive(a) and by_id(c, a) is not None


def test_contradictions_are_flagged_not_merged_and_the_flag_clears_when_fixed(tmp_path):
    c, _ = mk(tmp_path)
    a = put(c, "Benny likes tea")
    b = put(c, "Benny doesn't like tea")
    d1 = put(c, "Thursday is the rest day")
    d2 = put(c, "Friday is the rest day")
    rep = C.consolidate(c)
    assert rep["merged"] == [] and {tuple(sorted(p)) for p in rep["flagged"]} == {(a, b), (d1, d2)}
    assert by_id(c, a)["flag"] == f"may conflict with #{b}" and {x["id"] for x in c.flagged()} == {a, b, d1, d2}
    c.add_to_knowledge("Benny used to like tea but stopped last year", replaces_id=a)      # the user's correction clears it
    assert by_id(c, a)["flag"] is None
    c.forget(b)
    C.consolidate(c)
    assert by_id(c, a)["flag"] is None                                   # flags are recomputed every run


def test_decay_happens_once_per_period_and_skips_pinned_and_five(tmp_path):
    c, _ = mk(tmp_path)
    old = dict(created=ago(60), updated=ago(60))
    x = put(c, "The router lives in the hall cupboard", **old)
    pref = put(c, "Keep answers short", category="preference", **old)
    five = put(c, "Benny's name is Ben", importance=5, **old)
    fresh = put(c, "Lucy shipped the new office tools")
    rep = C.consolidate(c)
    assert rep["decayed"] == [x] and by_id(c, x)["importance"] == 2
    assert by_id(c, pref)["importance"] == 3 and by_id(c, five)["importance"] == 5 and by_id(c, fresh)["importance"] == 3
    assert C.consolidate(c)["decayed"] == []                             # not again the next night


def test_stale_importance_one_is_archived_and_dry_run_changes_nothing(tmp_path):
    c, _ = mk(tmp_path)
    s = put(c, "An old forgotten detail about a printer", importance=1, created=ago(400), updated=ago(400))
    before = json.dumps(c.all(), sort_keys=True)
    rep = C.consolidate(c, dry_run=True)
    assert rep["archived"] == [s] and json.dumps(c.all(), sort_keys=True) == before and c.archived() == []
    C.consolidate(c)
    assert by_id(c, s) is None and c.archived()[0]["id"] == s


def test_consolidating_twice_is_a_no_op_and_summaries_read_well(tmp_path):
    c, _ = mk(tmp_path)
    put(c, "Benny likes tea")
    put(c, "Benny doesn't like tea")
    first = C.consolidate(c)
    assert "possible conflict" in " ".join(C.summarize(first, c))
    second = C.consolidate(c)
    assert second["merged"] == [] and second["decayed"] == [] and len(second["flagged"]) == 1


def test_manager_passthroughs(tmp_path):
    m = MemoryManager.__new__(MemoryManager)
    m.conductor, _ = mk(tmp_path)
    put(m.conductor, "Benny likes tea")
    put(m.conductor, "Benny doesn't like tea")
    m.consolidate()
    assert len(m.flagged()) == 2


# ------------------------------------------------------------------------------------------ conversation search (regression)
def test_search_conversations_works_on_the_real_messages_schema():
    """history.py used a column (`rowid_ref`) that the messages table never had: every real search raised."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, title TEXT, updated_at TEXT)")
    conn.execute("CREATE TABLE messages (session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT, attachments TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('s1', 'Fruit chat', '2026-01-01')")
    conn.execute("INSERT INTO messages VALUES ('s1', 'user', 'I bought a banana yesterday', '2026-01-01', NULL)")
    conn.execute("INSERT INTO messages VALUES ('s1', 'assistant', 'Bananas are great', '2026-01-01', NULL)")
    lock = threading.RLock()
    res = history.search_history(conn, lock, "banana")
    assert {r["role"] for r in res} == {"user", "assistant"} and res[0]["title"] == "Fruit chat"
    conn.execute("INSERT INTO messages VALUES ('s1', 'user', 'and a cherry later', '2026-01-02', NULL)")
    assert [r["snippet"] for r in history.search_history(conn, lock, "cherry")] == ["and a cherry later"]    # incremental index
    assert [m["role"] for m in history.read_session(conn, lock, "s1")] == ["user", "assistant", "user"]
