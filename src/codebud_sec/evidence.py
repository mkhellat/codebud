"""
codebud_sec/evidence.py

The evidence log (phase 10): a hash-chained, append-only record of a
security engagement -- the chain of custody for incident response on hosts
the operator owns, or for a contracted penetration test.

The log is a JSON Lines file. Each entry records what happened (``kind``
and ``data``), when (UTC), the SHA-256 of any artefact the step collected,
a random ``nonce``, the hash of the previous entry (``prev``) and its own
hash (``hash``, the SHA-256 of the entry's canonical JSON without that
field). The first entry (``open``) links to GENESIS and names the
engagement; ``close`` ends the log. Editing, removing or reordering an
entry breaks the chain, and verify() reports the first line where it
breaks.

What the chain proves is relative to a head hash kept somewhere else.
Anyone who can write the file can rewrite it consistently -- edit an
entry and recompute every hash after it, or cut entries off the end --
and verify() will still pass. So record the head hash verify() reports
outside the log (in the case notes, a ticket, a signed message); the log
then shows any change made before that point.

Encrypted at rest (D14b: all security data). A log created with
encryption on (codebud.crypto) stores each entry as

    {"seq": .., "kind": .., "prev": .., "hash": .., "sealed": "age:..."}

-- the entry itself is in ``sealed``; the sequence number, kind and the
two hashes stay readable so that appending needs only the public key,
never the passphrase. The nonce keeps a readable hash from confirming a
guess at an entry's contents. Reading the contents, and so a full
verify(), needs the key unlocked; open() checks what it can without it.

Codebud only ever appends; the file is 0600 and appends hold an exclusive
flock(2). Entries are stored verbatim -- secret masking would alter the
evidence.
"""

import fcntl
import hashlib
import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from codebud import crypto
from codebud.fsutil import private_dir

GENESIS = "0" * 64
KINDS = ("open", "step", "warning", "note", "close")
_CLEAR = ("seq", "kind", "prev", "hash")  # readable fields of an encrypted line


class EvidenceError(Exception):
    """An evidence log that cannot be read, written or trusted."""


def _canonical(entry: dict) -> bytes:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def entry_hash(entry: dict) -> str:
    """The SHA-256 of ``entry``'s canonical JSON, without its ``hash`` field."""
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(_canonical(body)).hexdigest()


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _artefact(path) -> dict:
    resolved = Path(path).resolve()
    try:
        return {
            "path": str(resolved),
            "sha256": file_sha256(resolved),
            "size": resolved.stat().st_size,
        }
    except OSError as exc:
        raise EvidenceError(f"cannot hash artefact {resolved}: {exc}") from exc


def _store(entry: dict, encrypted: bool) -> str:
    """The line ``entry`` is stored as."""
    if not encrypted:
        return json.dumps(entry, sort_keys=True, ensure_ascii=False)
    wrapper = {k: entry[k] for k in _CLEAR}
    wrapper["sealed"] = crypto.encrypt_line(json.dumps(entry, sort_keys=True, ensure_ascii=False))
    return json.dumps(wrapper, sort_keys=True)


def _unseal(record: dict) -> dict:
    """The entry inside an encrypted line (the key must be unlocked)."""
    try:
        entry = json.loads(crypto.decrypt_line(record["sealed"]))
    except (crypto.CryptoError, ValueError, TypeError) as exc:
        raise ValueError(f"cannot decrypt the entry: {exc}") from exc
    if not isinstance(entry, dict):
        raise ValueError("the decrypted entry is not a JSON object")
    if any(entry.get(k) != record.get(k) for k in _CLEAR):
        raise ValueError("its readable fields do not match the encrypted entry")
    return entry


@dataclass
class VerifyResult:
    ok: bool
    entries: int = 0
    closed: bool = False
    head: str | None = None  # hash of the last good entry
    error: str | None = None
    encrypted: bool = False
    artefact_problems: list[str] = field(default_factory=list)


def _check_chain(lines: list[str], contents: bool) -> tuple[VerifyResult, list[dict]]:
    """Walk the entries; stop at the first one that breaks the chain.

    Returns the result and the entries read. With ``contents`` False, an
    encrypted entry is checked by its readable fields only (no key needed).
    """
    if not lines:
        return VerifyResult(False, error="the log is empty"), []
    prev, closed, entries = GENESIS, False, []
    encrypted = False
    for seq, line in enumerate(lines):
        where = f"line {seq + 1}"
        partial = VerifyResult(False, entries=seq, head=None if seq == 0 else prev)
        partial.encrypted = encrypted
        try:
            record = json.loads(line)
        except ValueError:
            partial.error = f"{where}: not a JSON entry"
            return partial, entries
        if not isinstance(record, dict):
            partial.error = f"{where}: not a JSON entry"
            return partial, entries
        sealed = "sealed" in record
        if seq == 0:
            encrypted = partial.encrypted = sealed
        elif sealed != encrypted:
            partial.error = f"{where}: mixes encrypted and plain entries"
            return partial, entries
        entry = record
        if sealed and contents:
            try:
                entry = _unseal(record)
            except ValueError as exc:
                partial.error = f"{where}: {exc}"
                return partial, entries
        if closed:
            partial.error = f"{where}: entry after the log was closed"
            return partial, entries
        if entry.get("seq") != seq:
            partial.error = f"{where}: sequence number {entry.get('seq')!r}, expected {seq}"
            return partial, entries
        if entry.get("prev") != prev:
            partial.error = f"{where}: does not link to the previous entry"
            return partial, entries
        if (not sealed or contents) and entry.get("hash") != entry_hash(entry):
            partial.error = f"{where}: hash does not match the entry (edited?)"
            return partial, entries
        if (seq == 0) != (entry.get("kind") == "open"):
            partial.error = f"{where}: the log must start with one 'open' entry"
            return partial, entries
        closed = entry.get("kind") == "close"
        prev = entry["hash"]
        entries.append(entry)
    result = VerifyResult(True, entries=len(lines), closed=closed, head=prev, encrypted=encrypted)
    return result, entries


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text().splitlines()
    except OSError as exc:
        raise EvidenceError(f"cannot read evidence log {path}: {exc}") from exc


def _is_encrypted(lines: list[str]) -> bool:
    try:
        return bool(lines) and "sealed" in json.loads(lines[0])
    except (ValueError, TypeError):
        return False


def verify(path, check_artefacts: bool = False) -> VerifyResult:
    """Check the chain of the log at ``path``, contents included.

    An encrypted log needs the key unlocked (EvidenceError otherwise).
    With ``check_artefacts``, also re-hash every artefact the log recorded
    and report any that are missing or no longer match.
    """
    path = Path(path)
    lines = _read_lines(path)
    if _is_encrypted(lines) and not crypto.is_unlocked():
        raise EvidenceError(
            f"evidence log {path} is encrypted; unlock the key to verify its contents"
        )
    result, entries = _check_chain(lines, contents=True)
    if not result.ok or not check_artefacts:
        return result
    for entry in entries:
        for record in entry.get("artefacts", []):
            try:
                if file_sha256(record["path"]) != record["sha256"]:
                    result.artefact_problems.append(
                        f"{record['path']}: changed since it was logged"
                    )
            except FileNotFoundError:
                result.artefact_problems.append(f"{record['path']}: missing")
            except OSError as exc:
                result.artefact_problems.append(f"{record['path']}: cannot read ({exc})")
    result.ok = not result.artefact_problems
    return result


def entries(path) -> list[dict]:
    """Every entry of a log that verifies (an encrypted one needs the key)."""
    path = Path(path)
    lines = _read_lines(path)
    if _is_encrypted(lines) and not crypto.is_unlocked():
        raise EvidenceError(f"evidence log {path} is encrypted; unlock the key to read it")
    result, found = _check_chain(lines, contents=True)
    if not result.ok:
        raise EvidenceError(f"evidence log {path} does not verify: {result.error}")
    return found


class EvidenceLog:
    """An open evidence log that entries are appended to."""

    def __init__(self, path: Path, encrypted: bool = False):
        self.path = path
        self.encrypted = encrypted

    @classmethod
    def create(
        cls, path, engagement=None, header: str = "", encrypt: bool | None = None
    ) -> "EvidenceLog":
        """Start a new log at ``path`` with its ``open`` entry.

        ``engagement`` (an engagement.Engagement) is recorded with the SHA-256
        of its file, so the log names exactly what was authorized.
        ``encrypt`` defaults to whether encryption at rest is on; it needs
        only the public key.
        """
        path = Path(path)
        encrypted = crypto.enabled() if encrypt is None else encrypt
        if encrypted:
            try:
                crypto.recipient()
            except crypto.CryptoError as exc:
                raise EvidenceError(f"cannot encrypt the evidence log: {exc}") from exc
        private_dir(path.parent)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise EvidenceError(f"evidence log {path} already exists") from exc
        os.close(fd)
        data: dict = {"header": header}
        if engagement is not None:
            data = {
                "header": header or engagement.header(),
                "engagement": str(engagement.path.resolve()),
                "engagement_sha256": file_sha256(engagement.path),
                "in_scope": [t.raw for t in engagement.in_scope],
                "out_of_scope": [t.raw for t in engagement.out_of_scope],
            }
        log = cls(path, encrypted)
        log._append("open", data, ())
        return log

    @classmethod
    def open(cls, path) -> "EvidenceLog":
        """Open an existing log for appending; refuse one whose chain is broken.

        An encrypted log is checked fully when the key is unlocked, and by
        its readable fields (sequence, links, kinds) when it is not.
        """
        path = Path(path)
        lines = _read_lines(path)
        result, _ = _check_chain(lines, contents=crypto.is_unlocked())
        if not result.ok:
            raise EvidenceError(f"evidence log {path} does not verify: {result.error}")
        return cls(path, result.encrypted)

    def append(self, kind: str, data: dict, artefacts=()) -> dict:
        """Append an entry; ``artefacts`` are paths whose SHA-256 is recorded."""
        if kind not in KINDS or kind == "open":
            raise EvidenceError(f"unknown entry kind {kind!r}")
        return self._append(kind, data, artefacts)

    def close(self, data: dict | None = None) -> dict:
        """End the log; nothing can be appended after this."""
        return self._append("close", data or {}, ())

    def _append(self, kind: str, data: dict, artefacts) -> dict:
        records = [_artefact(a) for a in artefacts]
        # Never recreate the log (a moved or deleted one would restart as a
        # new, unverified chain with default permissions) and never follow a
        # symlink put in its place (security review, low).
        flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags)
        except FileNotFoundError as exc:
            raise EvidenceError(
                f"evidence log {self.path} is gone; not recreating it "
                "(the chain would restart unverified)"
            ) from exc
        except OSError as exc:  # ELOOP: a symbolic link in its place
            raise EvidenceError(f"cannot open evidence log {self.path}: {exc}") from exc
        with os.fdopen(fd, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)  # released when f is closed
            f.seek(0)
            lines = f.read().splitlines()
            if lines:
                last = json.loads(lines[-1])  # readable fields suffice
                if last.get("kind") == "close":
                    raise EvidenceError(f"evidence log {self.path} is closed")
                seq, prev = last["seq"] + 1, last["hash"]
            else:
                seq, prev = 0, GENESIS
            entry = {
                "seq": seq,
                "time": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                "kind": kind,
                "data": data,
                "artefacts": records,
                "nonce": secrets.token_hex(16),
                "prev": prev,
            }
            entry["hash"] = entry_hash(entry)
            f.write(_store(entry, self.encrypted) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return entry
