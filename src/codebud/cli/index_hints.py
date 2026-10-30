"""
codebud/cli/index_hints.py

Keeping the project index fresh in `codebud chat` (phase 9, D5 and D13d).

Codebud never re-embeds on its own (a first index takes minutes on a
CPU); instead the chat says when /index would help:

- at the start, in a git project without an index, or with an index the
  project has moved away from;
- after a plan's steps changed files (once, until the next /index).

The bottom toolbar shows the same state: "RAG", "RAG stale", "RAG off",
"RAG locked" (an encrypted index whose key is not unlocked), or nothing
when the project has no index. The checks only look at file
names and times (indexer.quick_status), so they are cheap.

CODEBUD_RAG_HINTS=0 turns the hints off; they are also quiet while
project context is off (/rag off, --no-rag, CODEBUD_RAG=0).
"""

from pathlib import Path

from codebud import crypto
from codebud.rag import indexer
from codebud.rag.store import is_sealed


def _quick_status(root: Path) -> dict | None:
    """indexer.quick_status(), or None while an encrypted index is locked."""
    try:
        return indexer.quick_status(root)
    except crypto.CryptoError:
        return None


def _counts(status: dict) -> str:
    return "{changed} changed, {new} new, {deleted} deleted".format(**status)


def rag_indicator(agent, root: Path) -> str:
    """Toolbar text for the project index state ("" without an index)."""
    try:
        status = indexer.quick_status(root)
    except crypto.CryptoError:
        return "RAG locked"
    if status is None:
        return ""
    if not agent.get_rag():
        return "RAG off"
    return "RAG stale" if any(status.values()) else "RAG"


class IndexHints:
    def __init__(self, agent, root: Path):
        self.agent = agent
        self.root = Path(root)
        self._changes_hinted = False

    def _active(self) -> bool:
        from codebud import settings

        return settings.flag("CODEBUD_RAG_HINTS", True) and self.agent.get_rag()

    def at_start(self) -> str | None:
        """A hint for the start of a chat, or None."""
        if not self._active():
            return None
        status = _quick_status(self.root)
        if status is None:
            if is_sealed(self.root):
                return None  # an encrypted index, locked: nothing to suggest
            if not (self.root / ".git").exists():
                return None  # not obviously a project (e.g. the home directory)
            return (
                "Tip: /index builds a project index, so requests get your code as "
                "context (a few minutes the first time). CODEBUD_RAG_HINTS=0 hides tips."
            )
        if any(status.values()):
            return (
                f"The project index is out of date ({_counts(status)} since it was "
                "built): /index refreshes it."
            )
        return None

    def after_changes(self) -> str | None:
        """A hint after steps changed files: once until refreshed()."""
        if not self._active() or self._changes_hinted:
            return None
        if _quick_status(self.root) is None:
            return None
        self._changes_hinted = True
        return "Files changed: /index refreshes the project index."

    def refreshed(self) -> None:
        """The index was just refreshed (/index): hint again after new changes."""
        self._changes_hinted = False
