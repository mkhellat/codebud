"""
codebud/sessions.py

Chat session checkpoints. Each conversation is saved to

    $XDG_DATA_HOME/codebud/sessions/<id>.json   (see codebud/paths.py)

after every turn, so it can be resumed later with `codebud chat --resume`.
This is separate from MemoryStore, which records executed *steps*; a
session records the *conversation*.

Several codebud chats may run at the same time, so:

- IDs are a UTC timestamp plus a random suffix (20261025-093012-7f3a), and
  the file is reserved with O_EXCL, so two chats can never share an ID.
- A chat holds an exclusive flock(2) on <id>.lock while the session is
  open; opening a session that another chat has open is refused.
- Every save is atomic (temp file + rename), so a crash never leaves a
  half-written session.

With encryption at rest on (crypto.py), a session file is an age file
under the same name. Saving needs only the public key; anything that
reads sessions -- resuming, listing, naming, deleting by name or age --
unlocks the key first. Plaintext sessions saved before stay readable.
"""

import fcntl
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from . import crypto, paths
from .fsutil import atomic_write_bytes, create_private, private_dir
from .redact import for_storage

logger = logging.getLogger(__name__)

# Overridable for tests; None means the default location in the data dir.
SESSIONS_DIR: str | None = None
FORMAT_VERSION = 1

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# A session ID is the only thing turned into a file path, so its shape is
# fixed: <UTC timestamp>-<random hex> (20261025-093012-7f3a). A value read
# from a file or the command line that is not this is refused, so it can
# never contain "/" or ".." and reach outside the sessions directory
# (security review H4, 2026-10-29).
_ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}\Z")


def valid_id(session_id: str) -> bool:
    return isinstance(session_id, str) and bool(_ID_RE.match(session_id))


class SessionError(Exception):
    """A session could not be created, opened or saved."""


class SessionInUseError(SessionError):
    """The session is open in another codebud chat."""


class SessionNotFoundError(SessionError):
    """No session matches the given ID or name."""


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _random_suffix() -> str:
    return secrets.token_hex(2)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dir() -> str:
    """Return the sessions directory (SESSIONS_DIR, else the data dir's)."""
    return SESSIONS_DIR or str(paths.data_dir() / "sessions")


def _require_id(session_id: str) -> str:
    if not valid_id(session_id):
        raise SessionError(f"Invalid session ID {session_id!r}.")
    return session_id


def _path(session_id: str) -> str:
    return os.path.join(_dir(), f"{_require_id(session_id)}.json")


def _lock_path(session_id: str) -> str:
    return os.path.join(_dir(), f"{_require_id(session_id)}.lock")


def _try_lock(session_id: str):
    """Return an open, exclusively flock'ed lock file, or None if held elsewhere."""
    create_private(_lock_path(session_id))
    lock = open(_lock_path(session_id), "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    return lock


def _read(session_id: str) -> dict[str, Any]:
    """The session's contents; an encrypted one unlocks the key first.

    A key that cannot be unlocked raises CryptoError (nothing can be read,
    so no point going on); a file that cannot be decrypted is corrupt.
    """
    try:
        with open(_path(session_id), "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        raise SessionNotFoundError(f"No session '{session_id}'.") from None
    if crypto.is_encrypted(raw):
        crypto.ensure_unlocked()
        try:
            raw = crypto.decrypt(raw)
        except crypto.CryptoError as exc:
            raise SessionError(f"Session file {_path(session_id)}: {exc}") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise SessionError(f"Session file {_path(session_id)} is corrupt.") from None
    if not isinstance(data, dict):
        raise SessionError(f"Session file {_path(session_id)} is corrupt.")
    if data.get("version", 0) > FORMAT_VERSION:
        raise SessionError(
            f"Session '{session_id}' was saved by a newer codebud "
            f"(format {data.get('version')}); upgrade to open it."
        )
    return data


def _all_session_data() -> list[dict[str, Any]]:
    """Return the parsed contents of every readable session file."""
    if not os.path.isdir(_dir()):
        return []
    found = []
    for entry in sorted(os.listdir(_dir())):
        if not entry.endswith(".json") or entry.startswith("."):
            continue
        stem = entry[: -len(".json")]
        if not valid_id(stem):
            continue  # not a session file codebud wrote
        try:
            data = _read(stem)
        except SessionError as exc:
            logger.warning("Skipping session: %s", exc)
            continue
        if data.get("id") != stem:
            # The ID inside the file must match its name, so a forged id
            # (e.g. "../../x") can never be used as a path later.
            logger.warning("Skipping session %s: its stored id does not match its file name", stem)
            continue
        found.append(data)
    return found


def validate_name(name: str) -> None:
    """Raise SessionError unless ``name`` is a valid session name."""
    if not _NAME_RE.fullmatch(name):
        raise SessionError(f"Invalid session name '{name}': use letters, digits, '.', '_' or '-'.")


class Session:
    """An open chat session. Holds the session's lock until close()."""

    def __init__(self, data: dict[str, Any], lock):
        self.id: str = data["id"]
        self.name: str | None = data.get("name")
        self.created: str = data.get("created", "")
        self.updated: str = data.get("updated", "")
        self.model: str = data.get("model", "")
        self.mode: str = data.get("mode", "")
        self.privacy: str = data.get("privacy", "")
        self.profile: str = data.get("profile", "")
        self.history: list[dict[str, str]] = list(data.get("history", []))
        self._lock = lock

    # ------------------------------------------------------------------
    # Creating and opening
    # ------------------------------------------------------------------

    @classmethod
    def create(cls, name: str | None = None) -> "Session":
        """Reserve a new, unique session file and open it."""
        private_dir(_dir())
        for _ in range(100):
            session_id = f"{_timestamp()}-{_random_suffix()}"
            try:
                os.close(os.open(_path(session_id), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            except FileExistsError:
                continue
            break
        else:
            raise SessionError("Could not allocate a unique session ID.")

        lock = _try_lock(session_id)
        if lock is None:  # pragma: no cover - the file was only just created
            raise SessionInUseError(f"Session '{session_id}' is already open.")
        now = _now_iso()
        session = cls({"id": session_id, "created": now, "updated": now}, lock)
        session._write()
        if name is not None:
            session.set_name(name)
        return session

    @classmethod
    def open(cls, ref: str) -> "Session":
        """Open the session whose ID or name is ``ref``."""
        session_id = resolve(ref)
        lock = _try_lock(session_id)
        if lock is None:
            raise SessionInUseError(f"Session '{ref}' is open in another codebud chat.")
        try:
            data = _read(session_id)
        except BaseException:
            lock.close()
            raise
        return cls(data, lock)

    # ------------------------------------------------------------------
    # Saving
    # ------------------------------------------------------------------

    def save(
        self,
        history: list[dict[str, str]],
        model: str,
        mode: str = "",
        privacy: str = "",
        profile: str = "",
    ) -> None:
        """Checkpoint ``history``, ``model`` and agent ``mode`` atomically.

        Secrets in the history are masked first (redact.py); the caller's
        live history is not changed.
        """
        self.history = for_storage(list(history))
        self.model = model
        self.mode = mode
        # Recorded so a session can be reviewed; never restored on resume
        # (a resumed chat must not silently become "trusted"), like the profile.
        self.privacy = privacy
        self.profile = profile
        self.updated = _now_iso()
        self._write()

    def set_name(self, name: str) -> None:
        """Give the session a unique, human-friendly name and save it."""
        validate_name(name)
        for other in _all_session_data():
            if other.get("name") == name and other["id"] != self.id:
                raise SessionError(f"Session name '{name}' is already used by {other['id']}.")
        self.name = name
        self._write()

    def close(self) -> None:
        """Release the session so another chat can open it."""
        if self._lock is not None:
            self._lock.close()  # closing the file releases the flock
            self._lock = None

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _write(self) -> None:
        data = {
            "version": FORMAT_VERSION,
            "id": self.id,
            "name": self.name,
            "created": self.created,
            "updated": self.updated,
            "model": self.model,
            "mode": self.mode,
            "privacy": self.privacy,
            "profile": self.profile,
            "history": self.history,
        }
        atomic_write_bytes(_path(self.id), crypto.seal_text(json.dumps(data, indent=2) + "\n"))


# ----------------------------------------------------------------------
# Listing
# ----------------------------------------------------------------------


def _in_use(session_id: str) -> bool:
    lock = _try_lock(session_id)
    if lock is None:
        return True
    lock.close()
    return False


def list_sessions() -> list[dict[str, Any]]:
    """Return a summary of every saved session, most recently updated first."""
    summaries = []
    for data in _all_session_data():
        history = data.get("history", [])
        first = next((t["content"] for t in history if t.get("role") == "user"), "")
        summaries.append(
            {
                "id": data["id"],
                "name": data.get("name"),
                "created": data.get("created", ""),
                "updated": data.get("updated", ""),
                "model": data.get("model", ""),
                "turns": len(history),
                "first_request": first,
                "in_use": _in_use(data["id"]),
            }
        )
    summaries.sort(key=lambda s: s["updated"], reverse=True)
    return summaries


def resolve(ref: str) -> str:
    """The ID of the session whose ID or name is ``ref``."""
    if valid_id(ref) and os.path.exists(_path(ref)):
        return ref
    matches = [d["id"] for d in _all_session_data() if d.get("name") == ref]
    if not matches:
        raise SessionNotFoundError(f"No session with ID or name '{ref}'.")
    return matches[0]


# ----------------------------------------------------------------------
# Deleting (phase 8, B2: sessions are kept until the user deletes them)
# ----------------------------------------------------------------------


def _older_than(updated: str, days: int) -> bool:
    try:
        when = datetime.fromisoformat(updated)
    except ValueError:
        return False  # unknown age: never deleted by age
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when < datetime.now(timezone.utc) - timedelta(days=days)


def unreadable_sessions() -> list[str]:
    """IDs of session files list_sessions() skips: corrupt, undecryptable,
    or holding an id that does not match their name. Their age is unknown,
    so only a purge of everything removes them (security review, low)."""
    if not os.path.isdir(_dir()):
        return []
    found = []
    for entry in sorted(os.listdir(_dir())):
        stem = entry[: -len(".json")]
        if not entry.endswith(".json") or entry.startswith(".") or not valid_id(stem):
            continue
        try:
            data = _read(stem)
        except SessionError:
            found.append(stem)
            continue
        if data.get("id") != stem:
            found.append(stem)
    return found


def deletion_candidates(older_than_days: int | None = None):
    """(sessions to delete, IDs skipped because they are open) -- by age if given.

    Sessions open in a chat are never deleted.
    """
    candidates, in_use = [], []
    for summary in list_sessions():
        if older_than_days is not None and not _older_than(summary["updated"], older_than_days):
            continue
        if summary["in_use"]:
            in_use.append(summary["id"])
        else:
            candidates.append(summary)
    return candidates, in_use


def session_files(session_id: str) -> list[str]:
    """The files of a session that exist: its JSON and its lock."""
    return [p for p in (_path(session_id), _lock_path(session_id)) if os.path.exists(p)]


def delete(session_ids: list[str]) -> list[str]:
    """Delete these sessions' files; return the paths removed.

    Each session is locked while its files are removed, so a chat cannot
    open it meanwhile; one already open raises SessionInUseError.
    """
    removed = []
    for session_id in session_ids:
        lock = _try_lock(session_id)
        if lock is None:
            raise SessionInUseError(f"Session '{session_id}' is open in a codebud chat.")
        try:
            for path in session_files(session_id):
                os.remove(path)
                removed.append(path)
        finally:
            lock.close()
    return removed


def most_recent_available() -> str | None:
    """Return the ID of the most recently updated session not open elsewhere."""
    for summary in list_sessions():
        if not summary["in_use"]:
            return summary["id"]
    return None
