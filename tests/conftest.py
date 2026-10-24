"""Tests never start the real Stella embed server: memory falls back to TF-IDF unless a test injects its own embedder."""
import os

os.environ.setdefault("LUCY_EMBED", "0")
