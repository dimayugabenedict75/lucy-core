"""
Memory Manager for Lucy Core.

This module interfaces with the RAG Conductor to provide memory services.
"""

from lucy.rag.conductor import Conductor

class MemoryManager:
    def __init__(self):
        # The Conductor now sits at the heart of our memory system
        self.conductor = Conductor()

    def get_contextual_memory(self, query: str):
        """
        Instead of just matching keywords, we use the Conductor
        to pull the most semantically relevant facts.
        """
        return self.conductor.retrieve_context(query)

    def save_memory(self, text: str):
        """Add a new memory to the knowledge base."""
        return self.conductor.add_to_knowledge(text)
