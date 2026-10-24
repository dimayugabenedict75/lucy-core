"""
RAG Conductor for Lucy Core.

Persistent long-term memory (runtime/data/knowledge.sqlite: SQLite + FTS5, see memory/store.py) plus retrieval.
The old runtime/data/knowledge_store.json is imported once on first start and then left alone as a backup.

Each memory is a small dict:
    {"id": 7, "text": "...", "category": "preference", "importance": 4,
     "created": "2026-10-10 14:03:11", "updated": "...", "last_used": "...", "uses": 3,
     "source": "agent" | "user" | "reflection" | None,
     "flag": None | "may conflict with #12"}
Memories written by older versions only have "text"; they get an id and defaults on load, so
nothing already saved is lost. Consolidation (memory/consolidate.py) archives instead of deleting.

Retrieval for the prompt has two parts:
  * PINNED   - preferences / standing instructions / importance-5 memories are always included
               (a preference like "keep answers short" never matches a query by words alone)
  * RELEVANT - the best TF-IDF matches for the current message (FTS5 keyword hits fill in when TF-IDF finds little)
"""

from __future__ import annotations

import os
import threading
import time

from lucy.paths import RUNTIME
from lucy.memory.store import MemoryStore
import numpy as np

from .embedding_engine import EmbeddingEngine, NeuralEmbedder, QUERY, DOC

CATEGORIES = ("preference", "instruction", "fact", "person", "project", "other")
PINNED_CATEGORIES = ("preference", "instruction")
PINNED_LIMIT = 8


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Conductor:
    """Manages knowledge, retrieval, and context injection for Lucy Core."""

    # Below this cosine-similarity score a stored fact is "not actually relevant" and left out.
    MIN_RELEVANCE = 0.08
    # At or above this score a new memory is treated as a restatement of an existing one.
    DUPLICATE_SCORE = 0.7
    # Neural (Stella) cosine thresholds. Starting guesses from the embed guide: MEASURE with tools/embed_calibrate.py.
    MIN_COSINE = 0.62
    NEURAL_DUPLICATE = 0.92
    NEURAL_MERGE = 0.95           # consolidation: cosine at/above this = same fact worded differently (guess: calibrate)
    NEURAL_FLAG = 0.78            # consolidation: same topic, so a conflicting pair is worth flagging (guess: calibrate)
    SEARCH_TIMEOUT = 3.0          # seconds a memory lookup may wait for the embed server before using TF-IDF

    _AUTO = object()

    def __init__(self, store_path=None, json_path=None, neural=_AUTO):
        self.embedding_engine = EmbeddingEngine()
        # neural=None -> TF-IDF only. Default: the real Stella server, but only for the production store
        # (a custom store_path means a test / sandbox, which must never start llama-server).
        if neural is Conductor._AUTO:
            neural = None
            if store_path is None and os.environ.get("LUCY_EMBED", "1") != "0":      # LUCY_EMBED=0: TF-IDF only (tests)
                from lucy.server.embed_manager import embed_server
                neural = NeuralEmbedder(embed_server)
        self.neural = neural
        self._bf_lock = threading.Lock()
        data_dir = RUNTIME / "data"
        self.json_path = json_path or (data_dir / "knowledge_store.json")      # legacy file: imported once, kept as backup
        self.store = MemoryStore(store_path or (data_dir / "knowledge.sqlite"))
        self._lock = threading.RLock()
        self.knowledge_store: list[dict] = []
        self._load()

    # -- persistence -------------------------------------------------------------
    def _load(self) -> None:
        with self._lock:
            self.store.migrate_json(self.json_path)
            docs = self.store.load_active()
            for d in docs:                                   # defaults for rows written by older versions
                for k, v in (("category", "fact"), ("importance", 3), ("uses", 0)):
                    if d.get(k) is None:
                        d[k] = v
            self.knowledge_store = docs

    def _save(self) -> None:
        self.store.sync(self.knowledge_store)

    def _next_id(self) -> int:
        # ids are never reused, not even those of archived memories (they stay in the database)
        return max([d["id"] for d in self.knowledge_store] + [self.store.max_id(), 0]) + 1

    # -- writing -------------------------------------------------------------------
    def add_to_knowledge(self, text: str, category: str = "fact", importance: int = 3,
                         source: str | None = None, replaces_id: int | None = None) -> dict:
        """Store a memory. Returns {"status": "saved"|"updated"|"duplicate"|"error", "id", "text"}.

        - replaces_id: overwrite that memory (the way to correct something that changed)
        - a near-identical existing memory is updated in place instead of adding a duplicate
        """
        text = " ".join(str(text).split())
        if not text:
            return {"status": "error", "error": "empty memory"}
        category = category if category in CATEGORIES else "other"
        try:
            importance = max(1, min(5, int(importance)))
        except (TypeError, ValueError):
            importance = 3
        dup_qv = None
        if self.neural is not None and replaces_id is None:
            dup_qv = self.neural.embed_texts([text], DOC, timeout=self.SEARCH_TIMEOUT)      # network call: outside the lock
        with self._lock:
            if replaces_id is not None:
                doc = self._get(replaces_id)
                if doc is None:
                    return {"status": "error", "error": f"no memory with id {replaces_id}"}
                doc.update(text=text, category=category, importance=importance, updated=_now(),
                           source=source or doc.get("source"), flag=None)
                self._save()
                return {"status": "updated", "id": doc["id"], "text": text}

            for doc in self.knowledge_store:
                if doc["text"].strip().lower() == text.lower():
                    doc["importance"] = max(doc["importance"], importance)
                    doc["updated"] = _now()
                    self._save()
                    return {"status": "duplicate", "id": doc["id"], "text": doc["text"]}
            if self.knowledge_store:
                from lucy.memory.consolidate import conflicting
                nd = self._neural_duplicate(dup_qv)
                if nd is not None:
                    # neural scores: "likes tea" vs "doesn't like tea" look like near-duplicates, so a conflicting
                    # pair is never overwritten here (use replaces_id for corrections)
                    idx, score = nd
                    if conflicting(self.knowledge_store[idx]["text"], text):
                        score = 0.0
                    dup = score >= self.NEURAL_DUPLICATE
                else:
                    score, idx = self.embedding_engine.rank(text, [d["text"] for d in self.knowledge_store])[0]
                    dup = score >= self.DUPLICATE_SCORE
                if dup:
                    doc = self.knowledge_store[idx]
                    doc.update(text=text, category=category,
                               importance=max(doc["importance"], importance), updated=_now(), flag=None)
                    self._save()
                    return {"status": "updated", "id": doc["id"], "text": text,
                            "note": "replaced a very similar existing memory"}

            doc = {"id": self._next_id(), "text": text, "category": category, "importance": importance,
                   "created": _now(), "updated": _now(), "last_used": None, "uses": 0, "source": source,
                   "flag": None, "merged_into": None, "decayed_at": None}
            self.knowledge_store.append(doc)
            self._save()
            if self.neural is not None:
                self._backfill_async()
            return {"status": "saved", "id": doc["id"], "text": text}

    def edit(self, memory_id, text=None, category=None, importance=None) -> dict:
        """Memory screen: change text / category / importance of one memory. Returns {"status": "updated"|"error", ...}."""
        with self._lock:
            doc = self._get(memory_id)
            if doc is None:
                return {"status": "error", "error": f"no memory with id {memory_id}"}
            if text is not None:
                text = " ".join(str(text).split())
                if not text:
                    return {"status": "error", "error": "text cannot be empty"}
                doc["text"] = text
            if category is not None:
                if category not in CATEGORIES:
                    return {"status": "error", "error": f"category must be one of {', '.join(CATEGORIES)}"}
                doc["category"] = category
            if importance is not None:
                try:
                    doc["importance"] = max(1, min(5, int(importance)))
                except (TypeError, ValueError):
                    return {"status": "error", "error": "importance must be 1-5"}
            doc.update(updated=_now(), flag=None)
            self._save()
        if self.neural is not None:
            self._backfill_async()
        return {"status": "updated", "memory": dict(doc)}

    def forget(self, memory_id) -> bool:
        with self._lock:
            doc = self._get(memory_id)
            if doc is None:
                return False
            self.knowledge_store.remove(doc)
            self._save()
            return True

    def _get(self, memory_id) -> dict | None:
        try:
            memory_id = int(memory_id)
        except (TypeError, ValueError):
            return None
        return next((d for d in self.knowledge_store if d["id"] == memory_id), None)

    # -- reading -------------------------------------------------------------------
    def all(self) -> list[dict]:
        with self._lock:
            return [dict(d) for d in self.knowledge_store]

    def _query_vec(self, query: str):
        """Stella vector of the query, or None. Does the network call, so never call it while holding self._lock."""
        if self.neural is None or not str(query).strip():
            return None
        return self.neural.embed_texts([query], QUERY, timeout=self.SEARCH_TIMEOUT)

    def search(self, query: str, limit: int = 5, _qv="auto") -> list[dict]:
        """Memories ranked by relevance to `query` (each with a 'score'); irrelevant ones are dropped.
        TF-IDF ranks; when it finds fewer than `limit`, FTS5 keyword hits fill the gap (low fixed score)."""
        if isinstance(_qv, str):                                    # "auto": embed the query now, before taking the lock
            _qv = self._query_vec(query)
        with self._lock:
            if not self.knowledge_store or not str(query).strip():
                return []
            ranked, floor = self._rank(query, _qv)
            out = [dict(self.knowledge_store[i], score=round(float(s), 3))
                   for s, i in ranked[:limit] if s >= floor]
            if len(out) < limit:
                have = {d["id"] for d in out}
                for mid in self.store.fts_search(query, limit=limit):
                    doc = self._get(mid)
                    if doc is not None and mid not in have and len(out) < limit:
                        out.append(dict(doc, score=floor))
            return out

    def _rank(self, query: str, qv=None):
        """([(score, index)], floor). Neural cosines only when the server answers AND every active memory has a fresh
        vector (all-or-nothing: two score scales are never mixed); otherwise exactly the old TF-IDF ranking."""
        docs = self.knowledge_store
        if self.neural is not None:
            if qv is not None:
                vecs = self.store.fresh_vectors(self.neural.model_id(), docs)
                if len(vecs) == len(docs):
                    sims = np.stack([vecs[d["id"]] for d in docs]) @ qv[0]
                    return sorted(((float(x), i) for i, x in enumerate(sims)), reverse=True), self.MIN_COSINE
                self._backfill_async()
        return self.embedding_engine.rank(query, [d["text"] for d in docs]), self.MIN_RELEVANCE

    def vector_status(self) -> dict:
        """How many active memories have a fresh Stella vector (search/consolidation use Stella only when ALL do)."""
        with self._lock:
            docs = [dict(d) for d in self.knowledge_store]
        if self.neural is None:
            return {"mode": "tfidf", "have": 0, "total": len(docs), "model": None}
        mid = self.neural.model_id()
        have = len(self.store.fresh_vectors(mid, docs))
        return {"mode": "neural" if docs and have == len(docs) else "tfidf", "have": have, "total": len(docs), "model": mid}

    def neural_sims(self):
        """Pairwise cosine matrix aligned with knowledge_store (caller holds the lock), or None unless every memory has a fresh vector."""
        if self.neural is None or len(self.knowledge_store) < 2:
            return None
        docs = self.knowledge_store
        vecs = self.store.fresh_vectors(self.neural.model_id(), docs)
        if len(vecs) != len(docs):
            return None
        m = np.stack([vecs[d["id"]] for d in docs])
        return m @ m.T

    def explain(self, query: str, top_k: int = 5) -> dict:
        """Why would these memories be recalled for `query`? Same ranking as the prompt uses; changes nothing."""
        qv = self._query_vec(query)                                 # network call outside the lock
        with self._lock:
            pins = self.pinned()
            pin_ids = {d["id"] for d in pins}
            injected = [d["id"] for d in pins]
            found = self.search(query, limit=top_k + len(pin_ids), _qv=qv) if str(query).strip() else []
            injected += [d["id"] for d in found if d["id"] not in pin_ids][:top_k]
            ranked, floor = self._rank(query, qv) if self.knowledge_store and str(query).strip() else ([], self.MIN_RELEVANCE)
            rows = [{"id": self.knowledge_store[i]["id"], "text": self.knowledge_store[i]["text"],
                     "category": self.knowledge_store[i]["category"], "importance": self.knowledge_store[i]["importance"],
                     "score": round(float(s), 3), "above_floor": bool(s >= floor),
                     "pinned": self.knowledge_store[i]["id"] in pin_ids,
                     "in_prompt": self.knowledge_store[i]["id"] in injected} for s, i in ranked[:12]]
            seen = {r["id"] for r in rows}
            for d in pins:                                           # pinned ones go in whatever the query is
                if d["id"] not in seen:
                    rows.append({"id": d["id"], "text": d["text"], "category": d["category"], "importance": d["importance"],
                                 "score": None, "above_floor": False, "pinned": True, "in_prompt": True})
            mode = "neural" if (qv is not None and floor == self.MIN_COSINE) else "tfidf"
            return {"mode": mode, "floor": floor, "rows": rows,
                    "note": ("Meaning-based search (Stella)." if mode == "neural" else
                             "Keyword search (TF-IDF): Stella is off, still loading, or some memories have no vector yet.")}

    # -- neural vectors ------------------------------------------------------------
    def backfill_vectors(self, background: bool = False) -> int:
        """Embed every active memory that has no fresh vector. Returns how many were written (0 if server down).
        background=True: only use a server that is already up and don't reset its idle timer."""
        if self.neural is None:
            return 0
        with self._lock:
            docs = [dict(d) for d in self.knowledge_store]
        mid = self.neural.model_id()
        have = self.store.fresh_vectors(mid, docs)
        todo = [d for d in docs if d["id"] not in have]
        done = 0
        for i in range(0, len(todo), 32):
            batch = todo[i:i + 32]
            v = self.neural.embed_texts([d["text"] for d in batch], DOC, background=background)
            if v is None:
                break
            for d, vec in zip(batch, v):
                self.store.put_vector(d["id"], d["text"], mid, vec)
                done += 1
        return done

    def _backfill_async(self) -> None:
        if not self._bf_lock.acquire(blocking=False):
            return

        def run():
            try:
                self.backfill_vectors()
            finally:
                self._bf_lock.release()
        threading.Thread(target=run, daemon=True).start()

    def _neural_duplicate(self, qv):
        """(index, cosine) of the nearest memory, from the STORED vectors (no re-embedding of the whole memory).
        None when the server is down or any memory lacks a fresh vector -> caller uses the TF-IDF check."""
        if self.neural is None or qv is None:
            return None
        docs = self.knowledge_store
        vecs = self.store.fresh_vectors(self.neural.model_id(), docs)
        if len(vecs) != len(docs):
            return None
        sims = np.stack([vecs[d["id"]] for d in docs]) @ qv[0]
        i = int(np.argmax(sims))
        return i, float(sims[i])

    def pinned(self, limit: int = PINNED_LIMIT) -> list[dict]:
        """Always-on memories: preferences / standing instructions / importance 5."""
        with self._lock:
            pins = [d for d in self.knowledge_store
                    if d["category"] in PINNED_CATEGORIES or d["importance"] >= 5]
            pins.sort(key=lambda d: (d["importance"], d.get("updated") or d.get("created") or ""), reverse=True)
            return [dict(d) for d in pins[:limit]]

    def mark_used(self, ids: list[int]) -> None:
        """Bookkeeping: remember when/how often a memory was surfaced (saved on the next write)."""
        now = _now()
        with self._lock:
            for d in self.knowledge_store:
                if d["id"] in ids:
                    d["last_used"], d["uses"] = now, d.get("uses", 0) + 1

    def retrieve_context(self, query: str, top_k: int = 5) -> str:
        """Text for the prompt: pinned memories + the best matches for `query`. "" if nothing."""
        qv = self._query_vec(query)                                 # network call outside the lock
        with self._lock:
            pins = self.pinned()
            pin_ids = {d["id"] for d in pins}
            relevant = [d for d in self.search(query, limit=top_k + len(pin_ids), _qv=qv) if d["id"] not in pin_ids][:top_k]
            chosen = pins + relevant
            if not chosen:
                return ""
            self.mark_used([d["id"] for d in chosen])
            return "\n".join(f"- {d['text']}" for d in chosen)

    # -- consolidation support (memory/consolidate.py decides what to do; these only apply it) ------
    def flagged(self) -> list[dict]:
        """Active memories marked as possibly conflicting with another one."""
        with self._lock:
            return [dict(d) for d in self.knowledge_store if d.get("flag")]

    def set_flags(self, flags: dict) -> None:
        """{memory_id: text or None}: replaces every flag at once (consolidation recomputes them from scratch)."""
        with self._lock:
            for d in self.knowledge_store:
                d["flag"] = flags.get(d["id"])
            self._save()

    def archive(self, memory_id, merged_into: int | None = None) -> bool:
        """Take a memory out of use but keep it in the database (reversible with unarchive)."""
        with self._lock:
            doc = self._get(memory_id)
            if doc is None:
                return False
            self._save()                                    # persist pending bookkeeping (e.g. last_used) first
            self.knowledge_store.remove(doc)
            return self.store.archive(doc["id"], merged_into)

    def unarchive(self, memory_id) -> bool:
        with self._lock:
            ok = self.store.unarchive(int(memory_id))
            if ok:
                self._load()
            return ok

    def archived(self, limit: int = 200) -> list[dict]:
        return self.store.archived(limit)
