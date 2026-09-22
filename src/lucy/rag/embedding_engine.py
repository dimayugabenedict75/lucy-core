"""
Embedding Engine for Lucy Core.

This module provides a *dependency-light* embedding solution using scikit-learn's
TF-IDF vectorizer. It is intentionally simple and avoids requiring heavy
dependencies like sentence-transformers or llama-cpp-python that are not part
of the core environment.
"""

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np

class EmbeddingEngine:
    """A simple, yet effective, TF-IDF embedding engine."""

    def __init__(self):
        # We initialize the vectorizer. It will build its vocabulary on the fly.
        self.vectorizer = TfidfVectorizer(
            lowercase=True,
            token_pattern=r"(?u)\b\w+\b",
            ngram_range=(1, 2)
        )
        self.fitted = False

    def _ensure_fit(self, texts):
        """Fits the vectorizer on the provided texts."""
        if not self.fitted:
            # We always have at least one document (the query)
            self.vectorizer.fit(texts)
            self.fitted = True

    def get_embedding(self, text: str):
        """Returns the embedding vector for a single piece of text."""
        # The vectorizer needs at least one sample to fit
        self._ensure_fit([text])
        vec = self.vectorizer.transform([text]).toarray()
        return vec.flatten()

    def compare(self, text1: str, text2: str):
        """Compares two strings and returns their cosine similarity score."""
        self._ensure_fit([text1, text2])
        vec1 = self.vectorizer.transform([text1])
        vec2 = self.vectorizer.transform([text2])
        similarity = cosine_similarity(vec1, vec2)[0][0]
        return similarity

    def get_embedding_batch(self, texts: list):
        """Returns embeddings for a batch of texts, used during retrieval."""
        self._ensure_fit(texts)
        vectors = self.vectorizer.transform(texts).toarray()
        return vectors.tolist()
