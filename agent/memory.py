"""
agent/memory.py

This module defines the MemoryStore class, which maintains a timeline of
execution snapshots. Each snapshot records:

- timestamp
- step metadata
- tool execution result

Memory is persisted to:
    data/memory/entries.json

The MemoryStore exposes:
- add_snapshot(step, result)
- history (list of snapshots)
"""

import json
import os
from datetime import datetime
from typing import Dict, Any, List


MEMORY_PATH = "data/memory/entries.json"


class MemoryStore:
    """
    Timeline-based memory store.

    Responsibilities:
    - Load memory snapshots from disk
    - Append new snapshots
    - Persist snapshots
    - Expose history for UI
    """

    def __init__(self):
        self.history: List[Dict[str, Any]] = []
        self._ensure_storage()
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_snapshot(self, step: Dict[str, Any], result: Dict[str, Any]):
        """
        Add a new memory snapshot.

        Snapshot format:
        {
            "timestamp": "...",
            "data": {
                "step": {...},
                "result": {...}
            }
        }
        """
        snapshot = {
            "timestamp": datetime.utcnow().isoformat(),
            "data": {
                "step": step,
                "result": result
            }
        }

        self.history.append(snapshot)
        self._save()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_storage(self):
        """Ensure the memory directory and file exist."""
        os.makedirs(os.path.dirname(MEMORY_PATH), exist_ok=True)
        if not os.path.exists(MEMORY_PATH):
            with open(MEMORY_PATH, "w") as f:
                json.dump([], f, indent=2)

    def _load(self):
        """Load memory snapshots from disk."""
        try:
            with open(MEMORY_PATH, "r") as f:
                self.history = json.load(f)
        except Exception:
            self.history = []

    def _save(self):
        """Persist memory snapshots to disk."""
        with open(MEMORY_PATH, "w") as f:
            json.dump(self.history, f, indent=2)
