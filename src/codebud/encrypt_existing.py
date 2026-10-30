"""
codebud/encrypt_existing.py

Encrypting data saved before encryption at rest was turned on
(`codebud key encrypt-existing`; D14g: codebud never deletes the
plaintext, the user does).

For each plaintext session, the step history if it has plaintext lines,
and each plaintext project index, the original is first *copied* byte for
byte into

    $XDG_DATA_HOME/codebud/plaintext-backup-<UTC time>/

and the copy checked; only then is the live file replaced, atomically,
by its encrypted version (an index becomes index.sqlite3.age beside it,
and the plaintext database is removed once its copy is safe). The
backup is listed for the user to delete
once they are satisfied. Encrypting needs only the public key, so no
passphrase is asked for.

Sessions open in a chat are skipped. Other plaintext codebud keeps -- an
old entries.json.migrated, quarantined *.corrupt-* files -- is not
converted, only reported.
"""

import fcntl
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from codebud import crypto, memory, paths, sessions
from codebud.fsutil import atomic_write_bytes, create_private, private_dir
from codebud.rag import store


@dataclass
class Plan:
    sessions: list[Path] = field(default_factory=list)  # plaintext, not open
    history: Path | None = None  # has plaintext lines
    indexes: list[Path] = field(default_factory=list)  # plaintext index.sqlite3 files
    in_use: list[str] = field(default_factory=list)  # session IDs skipped
    other_plaintext: list[Path] = field(default_factory=list)  # reported only

    def files(self) -> list[Path]:
        return [*self.sessions, *([self.history] if self.history else []), *self.indexes]


def plan() -> Plan:
    """What encrypt-existing would convert, skip and leave."""
    result = Plan()
    session_dir = Path(sessions._dir())
    if session_dir.is_dir():
        for path in sorted(session_dir.glob("*.json")):
            if path.name.startswith(".") or crypto.is_encrypted(path.read_bytes()):
                continue
            session_id = path.stem
            if sessions._in_use(session_id):
                result.in_use.append(session_id)
            else:
                result.sessions.append(path)
    history = Path(memory._path())
    if history.is_file() and any(
        line.strip() and not crypto.is_encrypted_line(line)
        for line in history.read_text().splitlines()
    ):
        result.history = history
    index_root = paths.data_dir() / "index"
    if index_root.is_dir():
        result.indexes = sorted(
            d / store.DB_NAME
            for d in index_root.iterdir()
            if (d / store.DB_NAME).is_file() and not (d / store.SEALED_NAME).exists()
        )
    memory_dir = history.parent
    if memory_dir.is_dir():
        result.other_plaintext = sorted(
            p for p in memory_dir.iterdir() if p.name.endswith(".migrated") or ".corrupt-" in p.name
        )
    return result


def backup_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return paths.data_dir() / f"plaintext-backup-{stamp}"


def _back_up(path: Path, backup: Path) -> Path:
    """Copy ``path`` into ``backup`` (same place under the data dir); check it."""
    target = backup / path.relative_to(paths.data_dir())
    private_dir(target.parent)
    shutil.copy2(path, target)
    os.chmod(target, 0o600)
    if target.read_bytes() != path.read_bytes():
        raise OSError(f"the backup copy {target} does not match {path}; nothing was replaced")
    return target


def apply(todo: Plan, backup: Path) -> list[Path]:
    """Back up and encrypt everything in ``todo``; return the backup copies.

    Each file is converted under the lock its writers use, so a chat or a
    run cannot write it meanwhile. A session opened since plan() is skipped.
    """
    copies = []
    for path in todo.sessions:
        lock = sessions._try_lock(path.stem)
        if lock is None:
            todo.in_use.append(path.stem)
            continue
        try:
            raw = path.read_bytes()
            if crypto.is_encrypted(raw):
                continue
            copies.append(_back_up(path, backup))
            atomic_write_bytes(path, crypto.encrypt(raw))
        finally:
            lock.close()
    if todo.history is not None:
        with memory.locked():
            path = todo.history
            lines = path.read_text().splitlines()
            copies.append(_back_up(path, backup))
            sealed = [
                line
                if not line.strip() or crypto.is_encrypted_line(line)
                else crypto.encrypt_line(line)
                for line in lines
            ]
            atomic_write_bytes(path, "".join(line + "\n" for line in sealed).encode())
    for db in todo.indexes:
        copies.extend(_encrypt_index(db, backup))
    return copies


def _encrypt_index(db: Path, backup: Path, timeout: float = 30.0) -> list[Path]:
    """Encrypt one plaintext index; return the backup copies of its files.

    Holds the index's lock (the one encrypted-index writers take) and a
    SQLite write transaction on a guard connection throughout, so a
    `codebud index` running meanwhile is waited for and none of its writes
    is lost (security review, low). The encrypted copy is a consistent
    snapshot made with SQLite's backup API from a second, read-only
    connection -- write-ahead log included. (The backup cannot come from
    the guard connection: SQLite reports "locked" when the source
    connection is itself writing, and Python's backup() retries forever.)
    """
    private_dir(db.parent)
    lock_path = db.parent / store.LOCK_NAME
    create_private(lock_path)
    with open(lock_path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        guard = sqlite3.connect(db, timeout=timeout)
        try:
            guard.execute("BEGIN IMMEDIATE")  # waits for writers, then keeps them out
            live = [p for p in (db, Path(f"{db}-wal"), Path(f"{db}-shm")) if p.exists()]
            copies = [_back_up(p, backup) for p in live]
            fd, snapshot = tempfile.mkstemp(dir=db.parent, prefix=".snapshot-", suffix=".tmp")
            os.close(fd)
            try:
                reader = sqlite3.connect(db, timeout=timeout)
                dest = sqlite3.connect(snapshot)
                try:
                    reader.backup(dest)
                finally:
                    dest.close()
                    reader.close()
                crypto.write_file(db.parent / store.SEALED_NAME, Path(snapshot).read_bytes())
            finally:
                os.unlink(snapshot)
            for path in live:  # each one's copy is in the backup
                path.unlink()
        finally:
            guard.close()
    return copies
