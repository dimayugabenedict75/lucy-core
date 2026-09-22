"""
RAG Conductor for Lucy Core.

This module is the central 'traffic controller' for the RAG system.
It manages how facts are retrieved and injected into the prompt, using
the EmbeddingEngine to turn text into vectors for semantic search.
"""

from .embedding_engine import EmbeddingEngine
from pathlib import Path
import json

class Conductor:
    """Manages knowledge, retrieval, and context injection for Lucy Core."""

    def __init__(self):
        # The Conductor now uses our dependency-light Embedding Engine
        self.embedding_engine = EmbeddingEngine()

        # Define the internal vector storage location
        self.vector_store_path = Path("C:/Users/dimay/Lucy/Lucy_Core/runtime/data/knowledge_store.json")

        # Load the existing knowledge base (if it exists)
        self.knowledge_store = []
        if self.vector_store_path.exists():
            with open(self.vector_store_path, "r") as f:
                self.knowledge_store = json.load(f)

        # Ensure the directory for our vector storage exists
        self.vector_store_path.parent.mkdir(parents=True, exist_ok=True)

    def retrieve_context(self, query: str, top_k: int = 3):
        """
        Finds the most relevant context for a query.
        1. Convert query to vector
        2. Find the most relevant 'chunks' in our library
        3. Return the 'context' to be injected into the prompt
        """
        if not self.knowledge_store:
            return "No prior knowledge found."

        # Calculate similarity between the query and each document
        rankings = []
        for doc in self.knowledge_store:
            sim = self.embedding_engine.compare(query, doc["text"])
            rankings.append((sim, doc))

        # Sort by similarity (descending)
        rankings.sort(key=lambda x: x[0], reverse=True)

        # Return the top_k results
        top_docs = rankings[:top_k]
        context = "\n---\n".join([doc["text"] for _, doc in top_docs])
        
        return context

    def add_to_knowledge(self, text: str):
        """Converts new text to vector and adds it to the knowledge store."""
        new_doc = {"text": text}
        self.knowledge_store.append(new_doc)

        # Persist to disk
        with open(self.vector_store_path, "w") as f:
            json.dump(self.knowledge_store, f)
