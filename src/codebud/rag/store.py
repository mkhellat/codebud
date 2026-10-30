"""
codebud/rag/store.py

The project index store (phase 9, B3; decision D13b).

One SQLite database per project, in
$XDG_DATA_HOME/codebud/index/<id>/index.sqlite3, where <id> is derived
from the project's absolute path. The directory is created 0700 and the
database 0600: the index holds copies of the project's text.

Tables:

- meta:   key/value -- project root, embedding model and dimension,
          schema version, whether keyword search is available, last update,
          and a generation counter bumped by every write (cache key)
- files:  path -> content hash, to re-index only what changed
- chunks: path, line range, symbol, kind, text, and the embedding as a
          unit-length float32 BLOB (so cosine similarity is a dot product)
- chunks_fts: FTS5 keyword index over path, symbol and text, keyed by
          chunk id; left out when SQLite was built without FTS5

An index belongs to one embedding model (A3): vectors from different
models are not comparable, so a different model or dimension is refused
until the index is rebuilt.

With encryption at rest on (crypto.py; D14g), a new index is stored as
index.sqlite3.age. SQLite cannot work on an encrypted file, so the index
is decrypted into a private directory under $XDG_RUNTIME_DIR (RAM-backed,
cleared at logout), used there and removed on close; `codebud index`
encrypts it back. A plaintext index from before stays as it is.
"""

import fcntl
import hashlib
import math
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from array import array
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path

from codebud import crypto, paths
from codebud.fsutil import create_private, private_dir
from codebud.rag.chunker import Chunk

SCHEMA_VERSION = "1"
DB_NAME = "index.sqlite3"
SEALED_NAME = DB_NAME + ".age"  # the index encrypted at rest
LOCK_NAME = ".lock"


class ModelMismatch(RuntimeError):
    """The index was built with a different embedding model or dimension."""


def index_dir(root: Path | str) -> Path:
    """Directory holding the index of the project at ``root``."""
    key = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]
    return paths.data_dir() / "index" / key


def _private_dir(path: Path) -> None:
    private_dir(path)
    os.chmod(path, 0o700)


def has_index(root: Path | str) -> bool:
    """Whether ``root`` has an index, plain or encrypted."""
    directory = index_dir(Path(root).resolve())
    return (directory / DB_NAME).exists() or (directory / SEALED_NAME).exists()


def is_sealed(root: Path | str) -> bool:
    """Whether ``root``'s index is encrypted at rest."""
    return (index_dir(Path(root).resolve()) / SEALED_NAME).exists()


RAM_FILESYSTEMS = ("tmpfs", "ramfs")


def filesystem_type(path: Path, mountinfo: str | None = None) -> str | None:
    """The type of the file system holding ``path`` on Linux, or None.

    Read from /proc/self/mountinfo (``mountinfo`` for tests): the mount
    with the longest mount point that contains ``path``.
    """
    if mountinfo is None:
        try:
            mountinfo = Path("/proc/self/mountinfo").read_text()
        except OSError:
            return None
    target = os.path.realpath(path)
    best, kind = "", None
    for line in mountinfo.splitlines():
        left, sep, right = line.partition(" - ")
        fields = left.split()
        if not sep or len(fields) < 5 or not right.split():
            continue
        mount = fields[4].replace("\\040", " ")  # mountinfo escapes spaces
        inside = target == mount or target.startswith(mount.rstrip("/") + "/")
        if inside and len(mount) >= len(best):
            best, kind = mount, right.split()[0]
    return kind


def _check_runtime_base(base: Path) -> None:
    """Refuse a runtime directory that is not private, or not in RAM (Linux)."""
    info = base.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise crypto.CryptoError(
            f"$XDG_RUNTIME_DIR ({base}) is not a directory only you can use "
            "(owned by you, mode 0700); the encrypted index is not decrypted there"
        )
    if sys.platform.startswith("linux"):
        kind = filesystem_type(base)
        if kind not in RAM_FILESYSTEMS:
            raise crypto.CryptoError(
                f"$XDG_RUNTIME_DIR ({base}) is not in memory (file system: "
                f"{kind or 'unknown'}); the encrypted index is not decrypted to disk"
            )


def _sweep_stale(parent: Path) -> None:
    """Remove decrypted copies left by codebud processes that are gone.

    Each copy's directory is named index-<pid>-...; a crash or kill -9
    leaves it behind until logout otherwise.
    """
    for directory in parent.glob("index-*"):
        try:
            pid = int(directory.name.split("-")[1])
        except (IndexError, ValueError):
            continue
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            shutil.rmtree(directory, ignore_errors=True)
        except PermissionError:
            pass  # alive, someone else's pid


def _runtime_dir() -> Path:
    """A new private directory in RAM for a decrypted index (D14g)."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base or not os.path.isabs(base) or not os.path.isdir(base):
        raise crypto.CryptoError(
            "an encrypted project index is used from a RAM-backed directory, "
            "$XDG_RUNTIME_DIR, which is not set here; without it the index would "
            "be decrypted to disk, so project search is unavailable"
        )
    _check_runtime_base(Path(base))
    parent = Path(base) / "codebud"
    private_dir(parent)
    _sweep_stale(parent)
    # mkdtemp creates it 0700
    return Path(tempfile.mkdtemp(prefix=f"index-{os.getpid()}-", dir=parent))


class IndexStore:
    def __init__(self, root: Path, db_path: Path, sealed: Path | None = None, lock=None):
        self.root = root
        self.db_path = db_path
        self.sealed = sealed  # the encrypted file at rest, if encrypted
        self._lock = lock  # held while an encrypted index is open for writing
        self._db = sqlite3.connect(db_path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        if sealed is not None:
            # SQLite's temporary files (large sorts, merges) go to /var/tmp
            # before /tmp, usually on disk: keep a decrypted index's in memory.
            self._db.execute("PRAGMA temp_store=MEMORY")
        self._create_schema()

    # --- opening ------------------------------------------------------------

    @classmethod
    def open(cls, root: Path | str, create: bool = False) -> "IndexStore | None":
        """Open the index of ``root``; None if it has none and not ``create``.

        An encrypted index (index.sqlite3.age) is decrypted into a private
        directory under $XDG_RUNTIME_DIR and used there; ``create`` opens it
        for writing, and close() encrypts it back. A new index is created
        encrypted when encryption at rest is on. Decrypting needs the key
        unlocked already: opening never asks for the passphrase.
        """
        root = Path(root).resolve()
        directory = index_dir(root)
        db_path = directory / DB_NAME
        sealed = directory / SEALED_NAME
        if sealed.exists() or (create and not db_path.exists() and crypto.enabled()):
            return cls._open_sealed(root, sealed, writable=create)
        if not db_path.exists():
            if not create:
                return None
            _private_dir(directory.parent)
            _private_dir(directory)
            # Create the file 0600 before SQLite opens it (it would use the umask).
            os.close(os.open(db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        return cls(root, db_path)

    @classmethod
    def _open_sealed(cls, root: Path, sealed: Path, writable: bool) -> "IndexStore":
        lock = None
        if writable:
            # One writer at a time: each encrypts its whole copy back on close.
            _private_dir(sealed.parent.parent)
            _private_dir(sealed.parent)
            create_private(sealed.parent / LOCK_NAME)
            lock = open(sealed.parent / LOCK_NAME, "a")
            fcntl.flock(lock, fcntl.LOCK_EX)
        work = None
        try:
            work = _runtime_dir()
            plain = work / DB_NAME
            data = crypto.decrypt(sealed.read_bytes()) if sealed.exists() else b""
            fd = os.open(plain, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            return cls(root, plain, sealed=sealed, lock=lock)
        except BaseException:
            if work is not None:
                shutil.rmtree(work, ignore_errors=True)
            if lock is not None:
                lock.close()
            raise

    def close(self) -> None:
        """Close; an encrypted index opened for writing is encrypted back first.

        The decrypted copy is removed either way.
        """
        if self.sealed is not None and self._lock is not None:
            # Fold the write-ahead log into the file explicitly: the file
            # alone is what gets encrypted back.
            busy = self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
            if busy:
                self._db.close()
                self._discard_copy()
                raise sqlite3.OperationalError(
                    "could not checkpoint the decrypted index; the encrypted one is unchanged"
                )
        self._db.close()
        if self.sealed is None:
            return
        try:
            if self._lock is not None:
                crypto.write_file(self.sealed, self.db_path.read_bytes())
        finally:
            self._discard_copy()

    def _discard_copy(self) -> None:
        """Remove the decrypted copy and release the writer's lock."""
        shutil.rmtree(self.db_path.parent, ignore_errors=True)
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    def __enter__(self) -> "IndexStore":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _create_schema(self) -> None:
        with self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY,
                    path TEXT NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    symbol TEXT,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    vector BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS chunks_path ON chunks (path);
                """
            )
            if self._meta("schema") is None:
                self._set_meta("schema", SCHEMA_VERSION)
                self._set_meta("root", str(self.root))
                self._set_meta("fts", "1" if self._create_fts() else "0")

    def _create_fts(self) -> bool:
        try:
            self._db.execute("CREATE VIRTUAL TABLE chunks_fts USING fts5(path, symbol, text)")
        except sqlite3.OperationalError:  # SQLite built without FTS5
            return False
        return True

    # --- metadata -----------------------------------------------------------

    def _meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    @property
    def has_fts(self) -> bool:
        return self._meta("fts") == "1"

    def check_model(self, model: str) -> None:
        """Refuse ``model`` if the index was built with another one."""
        stored = self._meta("model")
        if stored is not None and stored != model:
            raise ModelMismatch(
                f"The index of {self.root} was built with {stored}; this run uses "
                f"{model}. Run: codebud index --rebuild"
            )

    def ensure_model(self, model: str, dim: int) -> None:
        """Bind the index to ``model``/``dim``, or refuse a different one (A3)."""
        self.check_model(model)
        stored_dim = self._meta("dim")
        if stored_dim is None:
            with self._db:
                self._set_meta("model", model)
                self._set_meta("dim", str(dim))
        elif int(stored_dim) != dim:
            raise ModelMismatch(
                f"The index of {self.root} holds {stored_dim}-dim vectors; {model} "
                f"now returns {dim}-dim ones. Run: codebud index --rebuild"
            )

    def _dim(self) -> int:
        dim = self._meta("dim")
        if dim is None:
            raise RuntimeError("ensure_model() must be called before storing vectors")
        return int(dim)

    # --- writing ------------------------------------------------------------

    def replace_file(
        self, path: str, sha256: str, items: Sequence[tuple[Chunk, Sequence[float]]]
    ) -> None:
        """Replace everything stored for ``path`` with ``items`` (chunk, vector)."""
        dim = self._dim()
        blobs = []
        for _chunk, vector in items:
            if len(vector) != dim:
                raise ValueError(f"Expected {dim}-dim vectors, got {len(vector)}")
            blobs.append(_unit_blob(vector))
        with self._db:
            self._delete_chunks([path])
            self._db.execute(
                "INSERT OR REPLACE INTO files (path, sha256) VALUES (?, ?)", (path, sha256)
            )
            for (chunk, _vector), blob in zip(items, blobs, strict=True):
                cursor = self._db.execute(
                    "INSERT INTO chunks (path, start_line, end_line, symbol, kind, text, vector)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        path,
                        chunk.start_line,
                        chunk.end_line,
                        chunk.symbol,
                        chunk.kind,
                        chunk.text,
                        blob,
                    ),
                )
                if self.has_fts:
                    self._db.execute(
                        "INSERT INTO chunks_fts (rowid, path, symbol, text) VALUES (?, ?, ?, ?)",
                        (cursor.lastrowid, path, chunk.symbol or "", chunk.text),
                    )
            self._touch()

    def remove_files(self, paths_: Sequence[str]) -> None:
        """Forget ``paths_`` and their chunks (files deleted from the project)."""
        with self._db:
            self._delete_chunks(paths_)
            self._db.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in paths_])
            self._touch()

    def _delete_chunks(self, paths_: Sequence[str]) -> None:
        for path in paths_:
            if self.has_fts:
                self._db.execute(
                    "DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE path = ?)",
                    (path,),
                )
            self._db.execute("DELETE FROM chunks WHERE path = ?", (path,))

    def _touch(self) -> None:
        self._mark_checked()
        self._set_meta("generation", str(self.generation + 1))

    def _mark_checked(self) -> None:
        self._set_meta("updated", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self._set_meta("updated_ts", repr(time.time()))

    def mark_checked(self) -> None:
        """Record that the project was compared with the index just now.

        Staleness checks compare file times with this, so a refresh that
        found nothing to change still makes the index fresh again.
        """
        with self._db:
            self._mark_checked()

    def updated_ts(self) -> float:
        """Seconds since the epoch of the last index run (0 if never)."""
        if self._meta("updated_ts"):
            return float(self._meta("updated_ts"))
        updated = self._meta("updated")  # indexes from before updated_ts
        return datetime.fromisoformat(updated).timestamp() if updated else 0.0

    @property
    def generation(self) -> int:
        """Counter bumped by every write; readers use it to invalidate caches."""
        return int(self._meta("generation") or 0)

    # --- reading ------------------------------------------------------------

    def file_hashes(self) -> dict[str, str]:
        return dict(self._db.execute("SELECT path, sha256 FROM files"))

    def vectors(self) -> Iterator[tuple[int, bytes]]:
        """(chunk id, float32 unit-vector BLOB) for every chunk, by id."""
        yield from self._db.execute("SELECT id, vector FROM chunks ORDER BY id")

    def chunk(self, chunk_id: int) -> Chunk:
        row = self._db.execute(
            "SELECT path, start_line, end_line, symbol, kind, text FROM chunks WHERE id = ?",
            (chunk_id,),
        ).fetchone()
        if row is None:
            raise KeyError(chunk_id)
        return Chunk(*row)

    def symbol_search(self, words: Sequence[str]) -> list[int]:
        """Chunk ids whose symbol is one of ``words`` (or ``Class.<word>``)."""
        ids: list[int] = []
        for word in words:
            rows = self._db.execute(
                "SELECT id FROM chunks WHERE symbol = ? OR symbol LIKE ? ESCAPE '\\' ORDER BY id",
                (word, "%." + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")),
            )
            ids.extend(r[0] for r in rows if r[0] not in ids)
        return ids

    def keyword_search(self, query: str, limit: int = 20) -> list[int]:
        """Chunk ids matching any word of ``query``, best (BM25) first."""
        words = re.findall(r"\w+", query)
        if not self.has_fts or not words:
            return []
        match = " OR ".join('"{}"'.format(w.replace('"', '""')) for w in words)
        rows = self._db.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts)"
            " LIMIT ?",
            (match, limit),
        )
        return [r[0] for r in rows]

    def stats(self) -> dict:
        count = self._db.execute
        return {
            "root": self._meta("root"),
            "model": self._meta("model"),
            "dim": int(self._meta("dim") or 0),
            "files": count("SELECT COUNT(*) FROM files").fetchone()[0],
            "chunks": count("SELECT COUNT(*) FROM chunks").fetchone()[0],
            "keyword_search": self.has_fts,
            "updated": self._meta("updated"),
            "path": str(self.sealed or self.db_path),
            "encrypted": self.sealed is not None,
        }


def _unit_blob(vector: Sequence[float]) -> bytes:
    """``vector`` scaled to length 1 (left as-is if zero), as float32 bytes."""
    norm = math.sqrt(sum(x * x for x in vector))
    if norm:
        vector = [x / norm for x in vector]
    return array("f", vector).tobytes()
