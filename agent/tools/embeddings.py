"""
agent/tools/embeddings.py

Provides two tools:

1. EmbedTool ("embed") — generate a real embedding vector via Ollama's
   /api/embed endpoint.  Falls back to a deterministic hash-based stub
   when the Ollama server is not reachable (so tests run offline).

2. EmbeddingSearchTool ("search_embeddings") — load stored embeddings
   from data/embeddings/index.json, embed the query, compute cosine
   similarity, and return top-k matches.

Environment variables:
  OLLAMA_BASE_URL   Base URL of the Ollama server (default: http://localhost:11434)
  OLLAMA_MODEL      Model used for text generation (reused for embeddings)
  EMBED_MODEL       Override which model to use for embeddings specifically.
                    Defaults to OLLAMA_MODEL or "qwen2.5-coder:3b-instruct-q4_K_M".
"""

import json
import os
import math
import logging
from typing import Dict, Any, List, Optional

import requests

logger = logging.getLogger(__name__)

EMBEDDING_INDEX_PATH = "data/embeddings/index.json"

_OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
_DEFAULT_EMBED_MODEL = (
    os.environ.get("EMBED_MODEL")
    or os.environ.get("OLLAMA_MODEL")
    or "qwen2.5-coder:3b-instruct-q4_K_M"
)


# ---------------------------------------------------------------------------
# Embedding backends
# ---------------------------------------------------------------------------


def ollama_embed(text: str, model: Optional[str] = None) -> List[float]:
    """Return a real embedding vector from the Ollama /api/embed endpoint.

    Raises RuntimeError if the server is unreachable or returns an error,
    so callers can fall back to the stub if needed.
    """
    m = model or _DEFAULT_EMBED_MODEL
    r = requests.post(
        f"{_OLLAMA_BASE}/api/embed",
        json={"model": m, "input": text},
        timeout=30,
    )
    r.raise_for_status()
    embeddings = r.json().get("embeddings", [[]])
    if not embeddings or not embeddings[0]:
        raise RuntimeError("Ollama returned empty embedding")
    return embeddings[0]


def stub_embedding(text: str) -> List[float]:
    """Deterministic hash-based embedding (16 floats, offline fallback)."""
    h = abs(hash(text))
    return [(h % (i + 7)) / 10.0 for i in range(16)]


def get_embedding(text: str) -> List[float]:
    """Get an embedding, using Ollama when available and the stub otherwise."""
    try:
        return ollama_embed(text)
    except Exception as exc:
        logger.debug("Ollama embed unavailable (%s), using stub", exc)
        return stub_embedding(text)


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------


def cosine_similarity(a: List[float], b: List[float]) -> float:
    if len(a) != len(b):
        # Dimension mismatch (e.g. mixing stub and real vectors in index)
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class EmbedTool:
    """Generate an embedding vector for text using Ollama (or stub fallback)."""

    description = "Generate an embedding vector for text."
    usage_hint = (
        'Use to embed a piece of text so it can be stored and later searched. '
        'Required args: "text" (string). Returns a vector stored in the embedding index.'
    )

    def run(self, args: Dict[str, Any]) -> Dict[str, Any]:
        text = args.get("text")
        if not text:
            return {"stdout": "", "stderr": "Missing required argument: text", "returncode": 1}

        vector = get_embedding(text)
        return {"stdout": json.dumps(vector), "stderr": "", "returncode": 0}


class EmbeddingSearchTool:
    """Search stored embeddings using cosine similarity."""

    description = "Search stored embeddings and return top-k matches."
    usage_hint = (
        'Use to find previously embedded content that is semantically similar to a query. '
        'Required args: "query" (string). Optional: "top_k" (int, default 5).'
    )

    def __init__(self):
        self._ensure_storage()
        self.index = self._load_index()

    def run(self, args: Dict[str, Any]) -> Dict[str, Any]:
        query = args.get("query")
        k = args.get("k", 5)
        if not query:
            return {"stdout": "", "stderr": "Missing required argument: query", "returncode": 1}

        query_vec = get_embedding(query)

        scored = []
        for entry in self.index:
            sim = cosine_similarity(query_vec, entry.get("vector", []))
            scored.append((sim, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        top_k = [entry for _, entry in scored[:k]]

        return {"stdout": json.dumps(top_k, indent=2), "stderr": "", "returncode": 0}

    def _ensure_storage(self):
        os.makedirs(os.path.dirname(EMBEDDING_INDEX_PATH), exist_ok=True)
        if not os.path.exists(EMBEDDING_INDEX_PATH):
            with open(EMBEDDING_INDEX_PATH, "w") as f:
                json.dump([], f, indent=2)

    def _load_index(self):
        try:
            with open(EMBEDDING_INDEX_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return []
