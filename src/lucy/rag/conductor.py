"""
RAG Conductor for Lucy Core.

Persistent long-term memory (runtime/data/knowledge_store.json) plus retrieval.

Each memory is a small JSON object:
    {"id": 7, "text": "...", "category": "preference", "importance": 4,
     "created": "2026-10-10 14:03:11", "updated": "...", "last_used": "...", "uses": 3,
     "source": "agent" | "user" | None}
Memories written by older versions only have "text"; they get an id and defaults on load, so
nothing already saved is lost.

Retrieval for the prompt has two parts:
  * PINNED   - preferences / standing instructions / importance-5 memories are always included
               (a preference like "keep answers short" never matches a query by words alone)
  * RELEVANT - the best TF-IDF matches for the current message
"""

from __future__ import annotations

import json
import os
import threading
import time

from lucy.paths import RUNTIME
from .embedding_engine import EmbeddingEngine

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

    def __init__(self):
        self.embedding_engine = EmbeddingEngine()
        self.vector_store_path = RUNTIME / "data" / "knowledge_store.json"
        self.vector_store_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.knowledge_store: list[dict] = []
        self._load()

    # -- persistence -------------------------------------------------------------
    def _load(self) -> None:
        raw = []
        if self.vector_store_path.exists():
            try:
                with open(self.vector_store_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except (OSError, ValueError):
                raw = []
        next_id = max([d.get("id", 0) for d in raw if isinstance(d, dict)] + [0]) + 1
        docs, migrated = [], False
        for d in raw:
            if not isinstance(d, dict) or not str(d.get("text", "")).strip():
                continue
            if "id" not in d:
                d["id"], next_id, migrated = next_id, next_id + 1, True
            for k, v in (("category", "fact"), ("importance", 3), ("created", None), ("updated", None),
                         ("last_used", None), ("uses", 0), ("source", None)):
                if k not in d:
                    d[k], migrated = v, True
            docs.append(d)
        self.knowledge_store = docs
        if migrated:
            self._save()

    def _save(self) -> None:
        tmp = self.vector_store_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.knowledge_store, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.vector_store_path)         # atomic: never leaves a half-written file

    def _next_id(self) -> int:
        return max([d["id"] for d in self.knowledge_store] + [0]) + 1

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
        with self._lock:
            if replaces_id is not None:
                doc = self._get(replaces_id)
                if doc is None:
                    return {"status": "error", "error": f"no memory with id {replaces_id}"}
                doc.update(text=text, category=category, importance=importance, updated=_now(),
                           source=source or doc.get("source"))
                self._save()
                return {"status": "updated", "id": doc["id"], "text": text}

            for doc in self.knowledge_store:
                if doc["text"].strip().lower() == text.lower():
                    doc["importance"] = max(doc["importance"], importance)
                    doc["updated"] = _now()
                    self._save()
                    return {"status": "duplicate", "id": doc["id"], "text": doc["text"]}
            if self.knowledge_store:
                score, idx = self.embedding_engine.rank(text, [d["text"] for d in self.knowledge_store])[0]
                if score >= self.DUPLICATE_SCORE:
                    doc = self.knowledge_store[idx]
                    doc.update(text=text, category=category,
                               importance=max(doc["importance"], importance), updated=_now())
                    self._save()
                    return {"status": "updated", "id": doc["id"], "text": text,
                            "note": "replaced a very similar existing memory"}

            doc = {"id": self._next_id(), "text": text, "category": category, "importance": importance,
                   "created": _now(), "updated": _now(), "last_used": None, "uses": 0, "source": source}
            self.knowledge_store.append(doc)
            self._save()
            return {"status": "saved", "id": doc["id"], "text": text}

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

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """Memories ranked by relevance to `query` (each with a 'score'); irrelevant ones are dropped."""
        with self._lock:
            if not self.knowledge_store or not str(query).strip():
                return []
            ranked = self.embedding_engine.rank(query, [d["text"] for d in self.knowledge_store])
            return [dict(self.knowledge_store[i], score=round(float(s), 3))
                    for s, i in ranked[:limit] if s >= self.MIN_RELEVANCE]

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
        with self._lock:
            pins = self.pinned()
            pin_ids = {d["id"] for d in pins}
            relevant = [d for d in self.search(query, limit=top_k + len(pin_ids)) if d["id"] not in pin_ids][:top_k]
            chosen = pins + relevant
            if not chosen:
                return ""
            self.mark_used([d["id"] for d in chosen])
            return "\n".join(f"- {d['text']}" for d in chosen)
