"""
agent/tools/embeddings.py

This module defines two tools:

1. EmbedTool (tool name: "embed")
   - Generates a deterministic embedding vector for a given text.
   - Uses a stub embedding function (hash-based) for now.

2. EmbeddingSearchTool (tool name: "search_embeddings")
   - Loads stored embeddings from disk
   - Embeds the query
   - Computes cosine similarity
   - Returns top-k matches

Embeddings are stored in:
    data/embeddings/index.json
"""

import json
import os
import math
from typing import Dict, Any, List


EMBEDDING_INDEX_PATH = "data/embeddings/index.json"


# ----------------------------------------------------------------------
# Utility functions
# ----------------------------------------------------------------------

def stub_embedding(text: str) -> List[float]:
    """
    Deterministic hash-based embedding.
    Produces a vector of 16 floats.
    """
    h = abs(hash(text))
    return [(h % (i + 7)) / 10.0 for i in range(16)]


def cosine_similarity(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ----------------------------------------------------------------------
# Tools
# ----------------------------------------------------------------------

class EmbedTool:
    """
    Generate an embedding vector for text.

    Args:
    {
        "text": "some text"
    }
    """

    description = "Generate an embedding vector for text."

    def run(self, args: Dict[str, Any]) -> Dict[str, Any]:
        text = args.get("text")

        if not text:
            return {
                "stdout": "",
                "stderr": "Missing required argument: text",
                "returncode": 1
            }

        vector = stub_embedding(text)

        return {
            "stdout": json.dumps(vector),
            "stderr": "",
            "returncode": 0
        }


class EmbeddingSearchTool:
    """
    Search stored embeddings using cosine similarity.

    Args:
    {
        "query": "search text",
        "k": 5
    }
    """

    description = "Search stored embeddings and return top-k matches."

    def __init__(self):
        self._ensure_storage()
        self.index = self._load_index()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, args: Dict[str, Any]) -> Dict[str, Any]:
        query = args.get("query")
        k = args.get("k", 5)

        if not query:
            return {
                "stdout": "",
                "stderr": "Missing required argument: query",
                "returncode": 1
            }

        query_vec = stub_embedding(query)

        # Compute similarities
        scored = []
        for entry in self.index:
            sim = cosine_similarity(query_vec, entry["vector"])
            scored.append((sim, entry))

        # Sort by similarity
        scored.sort(key=lambda x: x[0], reverse=True)

        top_k = [entry for _, entry in scored[:k]]

        return {
            "stdout": json.dumps(top_k, indent=2),
            "stderr": "",
            "returncode": 0
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
