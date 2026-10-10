"""
Memory Manager for Lucy Core.

Thin facade over the RAG Conductor (persistent long-term memory). The agent drives it through the
remember / recall_memory / forget tools; the prompt builder calls get_contextual_memory().
"""

from lucy.rag.conductor import Conductor


class MemoryManager:
    def __init__(self):
        self.conductor = Conductor()

    def get_contextual_memory(self, query: str) -> str:
        """Pinned (preferences / standing instructions) + the memories most relevant to `query`."""
        return self.conductor.retrieve_context(query)

    def save_memory(self, text: str, **kwargs) -> dict:
        """Add a memory (used by the UI's "Save to memory" link and by the agent's remember tool)."""
        return self.conductor.add_to_knowledge(text, **kwargs)

    def recall(self, query: str, limit: int = 5) -> list[dict]:
        return self.conductor.search(query, limit=limit)

    def forget(self, memory_id) -> bool:
        return self.conductor.forget(memory_id)

    def list_all(self) -> list[dict]:
        return self.conductor.all()
