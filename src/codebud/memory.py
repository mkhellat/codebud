"""
codebud/memory.py

This module defines the MemoryStore class, which maintains a timeline of
execution snapshots. Each snapshot records:

- timestamp
- step metadata
- tool execution result

Memory is persisted as JSON Lines (one snapshot per line, append-only) to:
    $XDG_DATA_HOME/codebud/memory/entries.jsonl   (see codebud/paths.py)

A legacy entries.json (a single JSON array) next to it is migrated
automatically on first load and kept as entries.json.migrated.

With encryption at rest on (crypto.py), each line is stored encrypted on
its own (age:<base64>). Appending needs only the public key, and the
history is read only when asked for (`codebud history`), so running
steps never asks for the passphrase. Plaintext lines written before stay
readable.

The MemoryStore exposes:
- add_snapshot(step, result)
- history (list of snapshots)
"""

import fcntl
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from . import crypto, paths
from .fsutil import atomic_write_text, create_private, private_dir
from .redact import for_storage

# Overridable for tests; None means the default location in the data dir.
MEMORY_PATH: str | None = None


def _path() -> str:
    """Return the memory file in use (MEMORY_PATH, else the data dir's)."""
    return MEMORY_PATH or str(paths.data_dir() / "memory" / "entries.jsonl")


@contextmanager
def locked():
    """Hold an exclusive flock(2) on the memory lock file.

    Everything that writes the history file takes it, so lines from two
    codebud processes never interleave.
    """
    create_private(f"{_path()}.lock")
    with open(f"{_path()}.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


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
        self._history: list[dict[str, Any]] | None = None  # read on first use
        private_dir(os.path.dirname(_path()))
        with self._locked():
            self._migrate_legacy_file()

    @property
    def history(self) -> list[dict[str, Any]]:
        """Every snapshot, read from disk the first time it is asked for.

        Reading is deferred so that running steps -- which only append --
        never needs the encryption key's passphrase.
        """
        if self._history is None:
            with self._locked():
                self._load()
        return self._history

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_snapshot(self, step: dict[str, Any], result: dict[str, Any]):
        """
        Add a new memory snapshot.

        Snapshot format (one per line in the file):
        {
            "timestamp": "...",
            "data": {
                "step": {...},
                "result": {...}
            }
        }
        """
        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            # Secrets in commands and their output are masked (redact.py).
            "data": for_storage({"step": step, "result": result}),
        }

        # Appending only touches the end of the file, so earlier entries
        # can never be lost to an interrupted write. The lock keeps lines
        # from two codebud processes from interleaving.
        with self._locked():
            create_private(_path())
            with open(_path(), "a+b") as f:
                if f.tell() > 0:
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        # A previous write was cut off mid-line; end that
                        # line so it can't swallow this entry.
                        f.write(b"\n")
                f.write(crypto.seal_line(json.dumps(snapshot)).encode() + b"\n")
                f.flush()
                os.fsync(f.fileno())
        if self._history is not None:
            self._history.append(snapshot)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _locked(self):
        return locked()

    def _load(self):
        """Load memory snapshots from disk, skipping unreadable lines.

        Bad lines are left in the file untouched (nothing is ever
        rewritten), so they stay recoverable by hand. An encrypted line
        unlocks the key first; a key that cannot be unlocked raises
        CryptoError, while a line that cannot be decrypted is skipped.
        """
        self._history = []
        skipped = 0
        try:
            with open(_path()) as f:
                for line in f:
                    if not line.strip():
                        continue
                    if crypto.is_encrypted_line(line):
                        crypto.ensure_unlocked()
                        try:
                            line = crypto.decrypt_line(line)
                        except crypto.CryptoError:
                            skipped += 1
                            continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        skipped += 1
                        continue
                    if isinstance(entry, dict):
                        self._history.append(entry)
                    else:
                        skipped += 1
        except FileNotFoundError:
            return
        if skipped:
            print(
                f"codebud: skipped {skipped} unreadable line(s) in {_path()}",
                file=sys.stderr,
            )

    def _migrate_legacy_file(self):
        """Convert a legacy entries.json array into entries.jsonl.

        Runs only while entries.jsonl does not exist yet. The new file is
        written atomically before the old one is renamed to
        entries.json.migrated, so a crash part-way loses nothing.
        """
        legacy = os.path.splitext(_path())[0] + ".json"
        if legacy == _path() or os.path.exists(_path()):
            return
        if not os.path.exists(legacy):
            return
        try:
            with open(legacy) as f:
                entries = json.load(f)
            if not isinstance(entries, list):
                raise ValueError("not a JSON array")
        except ValueError:
            self._quarantine_corrupt_file(legacy)
            return

        # Encrypted like any new line when encryption at rest is on.
        lines = "".join(crypto.seal_line(json.dumps(e)) + "\n" for e in entries)
        atomic_write_text(_path(), lines)
        os.replace(legacy, f"{legacy}.migrated")

    def _quarantine_corrupt_file(self, path):
        """Rename an unparseable memory file to <path>.corrupt-<ts>."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = f"{path}.corrupt-{stamp}"
        os.replace(path, backup)
        print(
            f"codebud: memory file {path} is corrupt; "
            f"moved it to {backup} and started a new history",
            file=sys.stderr,
        )


# ----------------------------------------------------------------------
# Deleting (phase 8, B2)
# ----------------------------------------------------------------------


def history_files() -> list[str]:
    """The step-history files that exist (what `codebud history --purge` removes).

    Besides entries.jsonl: the pre-JSONL entries.json (or its
    entries.json.migrated copy) and quarantined *.corrupt-* files, which
    hold the same history in plaintext and would otherwise survive a purge
    (security review, low).
    """
    found = [_path()]
    legacy = os.path.splitext(_path())[0] + ".json"
    found += [legacy, f"{legacy}.migrated"]
    directory = os.path.dirname(_path())
    if os.path.isdir(directory):
        stems = (os.path.basename(_path()), os.path.basename(legacy))
        found += sorted(
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if ".corrupt-" in name and name.startswith(stems)
        )
    return [p for p in found if os.path.isfile(p)]


def purge() -> list[str]:
    """Delete the step history; return the paths removed."""
    if not os.path.isdir(os.path.dirname(_path())):
        return []
    with locked():
        removed = history_files()
        for path in removed:
            os.remove(path)
    return removed
