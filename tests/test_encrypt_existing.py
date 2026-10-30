"""
tests/test_encrypt_existing.py

`codebud key encrypt-existing` (phase 10, D14g): sessions and history
saved in plain text before encryption was turned on are encrypted in
place -- after each original is copied, unchanged, into a
plaintext-backup-<time>/ directory that codebud never deletes. Sessions
open in a chat are skipped; nothing happens without a yes; no passphrase
is needed.
"""

import os
import stat
import sys
from unittest.mock import patch

import pytest

pytest.importorskip("pyrage")

from codebud import crypto, memory, paths, sessions  # noqa: E402

pytestmark = pytest.mark.usefixtures("fast_passphrase")  # see conftest.py

PASSPHRASE = "s3cret"


@pytest.fixture(autouse=True)
def no_prompt(monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda _p="": pytest.fail("asked for the passphrase"))


@pytest.fixture
def plaintext():
    """A plaintext session and two plaintext history lines; then a key."""
    with sessions.Session.create(name="old") as s:
        s.save([{"role": "user", "content": "before encryption"}], model="m")
    store = memory.MemoryStore()
    store.add_snapshot({"cmd": "one"}, {})
    store.add_snapshot({"cmd": "two"}, {})
    crypto.create_key(PASSPHRASE)
    crypto.lock()
    return paths.data_dir() / "sessions" / f"{s.id}.json"


def _codebud(*argv):
    from codebud.cli import main as cli

    with patch.object(sys, "argv", ["codebud", *argv]):
        try:
            cli.main()
        except SystemExit as exc:
            return exc.code or 0
    return 0


def _history():
    return paths.data_dir() / "memory" / "entries.jsonl"


def _backups():
    return sorted(paths.data_dir().glob("plaintext-backup-*"))


def test_encrypts_in_place_after_copying_the_plaintext(plaintext, capsys):
    session_before = plaintext.read_bytes()
    history_before = _history().read_bytes()

    assert _codebud("key", "encrypt-existing", "--yes") == 0

    assert crypto.is_encrypted(plaintext.read_bytes())
    lines = _history().read_text().splitlines()
    assert len(lines) == 2 and all(crypto.is_encrypted_line(line) for line in lines)

    (backup,) = _backups()
    session_copy = backup / "sessions" / plaintext.name
    history_copy = backup / "memory" / "entries.jsonl"
    assert session_copy.read_bytes() == session_before
    assert history_copy.read_bytes() == history_before
    assert stat.S_IMODE(os.stat(session_copy).st_mode) == 0o600
    out = capsys.readouterr().out
    assert str(session_copy) in out and f"rm -r {backup}" in out


def test_encrypted_data_reads_back_the_same(plaintext, monkeypatch):
    assert _codebud("key", "encrypt-existing", "--yes") == 0
    crypto.unlock(PASSPHRASE)
    (summary,) = sessions.list_sessions()
    assert summary["name"] == "old" and summary["first_request"] == "before encryption"
    assert [e["data"]["step"]["cmd"] for e in memory.MemoryStore().history] == ["one", "two"]


def test_already_encrypted_lines_are_kept(plaintext, monkeypatch):
    monkeypatch.setenv("CODEBUD_ENCRYPT", "1")
    memory.MemoryStore().add_snapshot({"cmd": "three"}, {})
    sealed_line = _history().read_text().splitlines()[2]
    assert _codebud("key", "encrypt-existing", "--yes") == 0
    assert _history().read_text().splitlines()[2] == sealed_line


def test_sessions_open_in_a_chat_are_skipped(plaintext, capsys):
    before = plaintext.read_bytes()
    with sessions.Session.open(plaintext.stem):
        assert _codebud("key", "encrypt-existing", "--yes") == 0
    assert plaintext.read_bytes() == before
    assert "open in a codebud chat" in capsys.readouterr().out


def test_nothing_changes_without_a_terminal_or_yes(plaintext, monkeypatch, capsys):
    before = plaintext.read_bytes()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert _codebud("key", "encrypt-existing") == 1
    assert plaintext.read_bytes() == before
    assert _backups() == []
    assert "--yes" in capsys.readouterr().err


def test_answering_no_changes_nothing(plaintext, monkeypatch):
    before = plaintext.read_bytes()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _p="": "n")
    assert _codebud("key", "encrypt-existing") == 1
    assert plaintext.read_bytes() == before
    assert _backups() == []


def test_answering_yes(plaintext, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _p="": "y")
    assert _codebud("key", "encrypt-existing") == 0
    assert crypto.is_encrypted(plaintext.read_bytes())


def test_nothing_to_do(capsys):
    crypto.create_key(PASSPHRASE)
    assert _codebud("key", "encrypt-existing", "--yes") == 0
    assert "Nothing to encrypt" in capsys.readouterr().out
    assert _backups() == []


def test_needs_a_key(capsys):
    assert _codebud("key", "encrypt-existing", "--yes") == 1
    assert "codebud key init" in capsys.readouterr().err


def test_older_plaintext_is_reported_not_converted(plaintext, capsys):
    legacy = paths.data_dir() / "memory" / "entries.json.migrated"
    legacy.write_text("[]")
    assert _codebud("key", "encrypt-existing", "--yes") == 0
    assert legacy.read_text() == "[]"
    assert f"Not converted (older plaintext; delete it yourself if unneeded): {legacy}" in (
        capsys.readouterr().out
    )


def test_notes_when_encryption_is_off(plaintext, capsys):
    assert _codebud("key", "encrypt-existing", "--yes") == 0
    assert "encryption is off" in capsys.readouterr().out


# --- converting an index while something writes it (security review, low) --


@pytest.fixture
def wal_index(tmp_path):
    """A plaintext WAL-mode index database in an index directory."""
    import sqlite3

    from codebud.rag import store

    directory = paths.data_dir() / "index" / "abcd"
    directory.mkdir(parents=True)
    db = directory / store.DB_NAME
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (x)")
    con.commit()
    con.close()
    crypto.create_key(PASSPHRASE)
    crypto.lock()
    return db


def test_rows_still_in_the_wal_are_in_the_encrypted_copy(wal_index, tmp_path):
    import sqlite3

    from codebud.encrypt_existing import _encrypt_index
    from codebud.rag import store

    writer = sqlite3.connect(wal_index)
    writer.execute("PRAGMA wal_autocheckpoint=0")  # keep the rows in the WAL
    writer.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(50)])
    writer.commit()
    try:
        _encrypt_index(wal_index, tmp_path / "backup")
    finally:
        writer.close()
    crypto.unlock(PASSPHRASE)
    plain = tmp_path / "check.sqlite3"
    plain.write_bytes(crypto.decrypt((wal_index.parent / store.SEALED_NAME).read_bytes()))
    con = sqlite3.connect(plain)
    try:
        assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 50
    finally:
        con.close()


def test_a_writer_in_progress_is_waited_for_not_overrun(wal_index, tmp_path):
    import sqlite3

    from codebud.encrypt_existing import _encrypt_index
    from codebud.rag import store

    writer = sqlite3.connect(wal_index)
    writer.execute("BEGIN IMMEDIATE")  # a `codebud index` mid-write
    writer.execute("INSERT INTO t VALUES (1)")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            _encrypt_index(wal_index, tmp_path / "backup", timeout=0.2)
    finally:
        writer.rollback()
        writer.close()
    # Nothing was converted, copied or removed.
    assert wal_index.exists()
    assert not (wal_index.parent / store.SEALED_NAME).exists()
    assert not (tmp_path / "backup").exists()
