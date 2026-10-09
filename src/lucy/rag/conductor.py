"""
RAG Conductor for Lucy Core.

This module is the central 'traffic controller' for the RAG system.
It manages how facts are retrieved and injected into the prompt, using
the EmbeddingEngine to turn text into vectors for semantic search.
"""

from .embedding_engine import EmbeddingEngine
from lucy.paths import RUNTIME
from pathlib import Path
import json

class Conductor:
    """Manages knowledge, retrieval, and context injection for Lucy Core."""

    def __init__(self):
        # The Conductor now uses our dependency-light Embedding Engine
        self.embedding_engine = EmbeddingEngine()

        # Define the internal vector storage location
        self.vector_store_path = RUNTIME / "data" / "knowledge_store.json"

        # Load the existing knowledge base (if it exists)
        self.knowledge_store = []
        if self.vector_store_path.exists():
            with open(self.vector_store_path, "r") as f:
                self.knowledge_store = json.load(f)

        # Ensure the directory for our vector storage exists
        self.vector_store_path.parent.mkdir(parents=True, exist_ok=True)

    # Below this cosine-similarity score, a stored fact is treated as
    # "not actually relevant" and left out, rather than force-fed to the
    # model just because it happened to be the least-bad match.
    MIN_RELEVANCE = 0.08

    def retrieve_context(self, query: str, top_k: int = 3):
        """
        Finds the most relevant context for a query.
        1. Rank every stored fact against the query using one shared vocabulary
        2. Drop anything below MIN_RELEVANCE
        3. Return the top_k results as context to inject into the prompt,
           or "" (falsy) if nothing relevant was found — callers should skip
           injecting a [USER FACTS] block entirely in that case.
        """
        if not self.knowledge_store:
            return ""

        texts = [doc["text"] for doc in self.knowledge_store]
        ranked = self.embedding_engine.rank(query, texts)
        top_docs = [self.knowledge_store[idx] for score, idx in ranked[:top_k] if score >= self.MIN_RELEVANCE]

        if not top_docs:
            return ""

        return "\n---\n".join(doc["text"] for doc in top_docs)

    def add_to_knowledge(self, text: str):
        """Converts new text to vector and adds it to the knowledge store.

        Duplicate detection: if an identical text string already exists in
        the store, skip insertion to prevent duplicate button-clicks from
        creating noise.
        """
        # Dedup check — skip if exact text already stored
        for doc in self.knowledge_store:
            if doc.get("text") == text:
                return

        new_doc = {"text": text}
        self.knowledge_store.append(new_doc)

        # Persist to disk
        with open(self.vector_store_path, "w") as f:
            json.dump(self.knowledge_store, f)
