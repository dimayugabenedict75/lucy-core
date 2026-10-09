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
        # NOTE: we intentionally do NOT keep one long-lived fitted vectorizer.
        # TfidfVectorizer.fit() is cheap at this corpus size, and a vectorizer
        # fitted once at startup goes stale: any word not present in that
        # first fit is out-of-vocabulary forever, silently breaking retrieval
        # for every fact saved afterward. We refit fresh per call instead.
        pass

    def get_embedding(self, text: str):
        """Returns the embedding vector for a single piece of text."""
        vectorizer = TfidfVectorizer(lowercase=True, token_pattern=r"(?u)\b\w+\b", ngram_range=(1, 2))
        vec = vectorizer.fit_transform([text]).toarray()
        return vec.flatten()

    def compare(self, text1: str, text2: str):
        """Compares two strings and returns their cosine similarity score."""
        vectorizer = TfidfVectorizer(lowercase=True, token_pattern=r"(?u)\b\w+\b", ngram_range=(1, 2))
        vecs = vectorizer.fit_transform([text1, text2])
        similarity = cosine_similarity(vecs[0], vecs[1])[0][0]
        return similarity

    def rank(self, query: str, texts: list):
        """Fits on the query + full corpus together (so shared vocabulary is
        actually shared) and returns [(score, index), ...] sorted descending.
        This is what retrieval should use instead of pairwise `compare` calls,
        since pairwise calls each build their own tiny two-document vocabulary
        and aren't comparable to one another."""
        if not texts:
            return []
        vectorizer = TfidfVectorizer(lowercase=True, token_pattern=r"(?u)\b\w+\b", ngram_range=(1, 2))
        matrix = vectorizer.fit_transform([query] + texts)
        query_vec, doc_vecs = matrix[0], matrix[1:]
        sims = cosine_similarity(query_vec, doc_vecs)[0]
        ranked = sorted(enumerate(sims), key=lambda x: x[1], reverse=True)
        return [(score, idx) for idx, score in ranked]

    def get_embedding_batch(self, texts: list):
        """Returns embeddings for a batch of texts, used during retrieval."""
        vectorizer = TfidfVectorizer(lowercase=True, token_pattern=r"(?u)\b\w+\b", ngram_range=(1, 2))
        vectors = vectorizer.fit_transform(texts).toarray()
        return vectors.tolist()
