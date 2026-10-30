"""
tests/test_purge.py

Deleting sessions and step history on request (phase 8, Part B2; decision
D12e: sessions are kept forever by default). Every deletion lists what it
removes and needs a "y" -- or --yes when there is no terminal; sessions
open in another chat are never touched. An optional retention limit,
set by the user, removes old sessions when a chat starts.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from codebud import memory, paths, sessions


def _session(name=None, days_old=0):
    s = sessions.Session.create(name=name)
    s.save([{"role": "user", "content": "hi"}], model="m", mode="ask")
    s.close()
    if days_old:
        path = paths.data_dir() / "sessions" / f"{s.id}.json"
        data = json.loads(path.read_text())
        data["updated"] = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
        path.write_text(json.dumps(data))
    return s.id


def _ids():
    return sorted(s["id"] for s in sessions.list_sessions())


def _codebud(*argv):
    from codebud.cli import main as cli

    with patch.object(sys, "argv", ["codebud", *argv]):
        try:
            cli.main()
        except SystemExit as exc:
            return exc.code or 0
    return 0


@pytest.fixture
def tty(monkeypatch):
    """Pretend stdin is a terminal; queue the answers typed."""
    answers = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": answers.pop(0))
    return answers


# --- library ----------------------------------------------------------------


def test_resolve_by_id_or_name():
    sid = _session(name="work")
    assert sessions.resolve(sid) == sid
    assert sessions.resolve("work") == sid
    with pytest.raises(sessions.SessionNotFoundError):
        sessions.resolve("nope")


def test_candidates_by_age_skip_sessions_in_use():
    old = _session(days_old=40)
    new = _session()
    busy = sessions.Session.create()  # open, holds its lock
    try:
        candidates, in_use = sessions.deletion_candidates(older_than_days=30)
        assert [c["id"] for c in candidates] == [old]
        candidates, in_use = sessions.deletion_candidates()
        assert sorted(c["id"] for c in candidates) == sorted([old, new])
        assert in_use == [busy.id]
    finally:
        busy.close()


def test_delete_removes_the_file_and_its_lock():
    sid = _session()
    removed = sessions.delete([sid])
    assert sorted(p.rsplit("/", 1)[1] for p in removed) == [f"{sid}.json", f"{sid}.lock"]
    assert _ids() == []


def test_delete_refuses_a_session_in_use():
    busy = sessions.Session.create()
    try:
        with pytest.raises(sessions.SessionInUseError):
            sessions.delete([busy.id])
    finally:
        busy.close()


def test_history_purge():
    memory.MemoryStore().add_snapshot({"tool": "command"}, {"returncode": 0})
    files = memory.history_files()
    assert [f.rsplit("/", 1)[1] for f in files] == ["entries.jsonl"]
    assert memory.purge() == files
    assert memory.history_files() == []
    assert memory.MemoryStore().history == []


# --- CLI --------------------------------------------------------------------


def test_sessions_delete_asks_and_lists(tty, capsys):
    sid = _session(name="work")
    tty.append("n")
    assert _codebud("sessions", "--delete", "work") == 1
    assert _ids() == [sid]
    assert f"{sid}.json" in capsys.readouterr().out
    tty.append("y")
    assert _codebud("sessions", "--delete", "work") == 0
    assert _ids() == []


def test_sessions_purge_needs_yes_without_a_terminal(monkeypatch, capsys):
    _session()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert _codebud("sessions", "--purge") == 1
    assert "--yes" in capsys.readouterr().err
    assert len(_ids()) == 1
    assert _codebud("sessions", "--purge", "--yes") == 0
    assert _ids() == []


def test_sessions_purge_older_than(tty, capsys):
    old = _session(days_old=100)
    new = _session()
    tty.append("y")
    assert _codebud("sessions", "--purge", "--older-than", "30") == 0
    assert _ids() == [new]
    assert old in capsys.readouterr().out


def test_purge_with_nothing_to_remove(capsys):
    assert _codebud("sessions", "--purge", "--yes") == 0
    assert "Nothing to delete" in capsys.readouterr().out


def test_history_purge_cli(tty):
    memory.MemoryStore().add_snapshot({"tool": "command"}, {"returncode": 0})
    tty.append("y")
    assert _codebud("history", "--purge") == 0
    assert memory.history_files() == []


def test_index_drop_still_confirms(tmp_path, monkeypatch, capsys):
    """index --drop now uses the shared confirmation helper."""
    (tmp_path / "a.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)
    assert _codebud("--no-progress", "index") == 0
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert _codebud("index", "--drop") == 1
    assert "--yes" in capsys.readouterr().err


# --- retention limit ----------------------------------------------------------


def test_retention_limit_removes_old_sessions_when_chat_starts(monkeypatch, capsys):
    old = _session(days_old=10)
    new = _session()
    monkeypatch.setenv("CODEBUD_SESSION_RETENTION_DAYS", "7")
    from codebud.cli.admin import apply_retention

    apply_retention()
    assert _ids() == [new]
    assert old in capsys.readouterr().err


def test_no_limit_by_default(monkeypatch):
    _session(days_old=3650)
    monkeypatch.delenv("CODEBUD_SESSION_RETENTION_DAYS", raising=False)
    from codebud.cli.admin import apply_retention

    apply_retention()
    assert len(_ids()) == 1


def test_a_project_config_cannot_set_retention(tmp_path, monkeypatch):
    from codebud import settings

    monkeypatch.delenv("CODEBUD_SESSION_RETENTION_DAYS", raising=False)
    monkeypatch.setattr(settings, "_warned", set())
    (tmp_path / ".codebud").mkdir()
    (tmp_path / ".codebud" / "config.toml").write_text("session_retention_days = 1\n")
    monkeypatch.chdir(tmp_path)
    assert settings.resolve()["CODEBUD_SESSION_RETENTION_DAYS"] == (0, "default")


def test_deleting_an_open_session_is_refused_before_asking(capsys):
    busy = sessions.Session.create(name="busy")
    try:
        assert _codebud("sessions", "--delete", "busy", "--yes") == 1
        assert "open in a codebud chat" in capsys.readouterr().err
        assert _ids() == [busy.id]
    finally:
        busy.close()


# --- purge leaves no plaintext behind (security review, low) ----------------


def test_history_purge_takes_legacy_and_quarantined_copies(capsys):
    memory.MemoryStore().add_snapshot({"tool": "command"}, {"returncode": 0})
    directory = paths.data_dir() / "memory"
    extras = [
        directory / "entries.json.migrated",
        directory / "entries.jsonl.corrupt-20260101T000000Z",
        directory / "entries.json.corrupt-20250101T000000Z",
    ]
    for path in extras:
        path.write_text("[old history]")
    unrelated = directory / "notes.txt"
    unrelated.write_text("keep")
    assert _codebud("history", "--purge", "--yes") == 0
    assert not any(p.exists() for p in extras)
    assert not (directory / "entries.jsonl").exists()
    assert unrelated.exists()
    out = capsys.readouterr().out
    assert all(str(p) in out for p in extras)  # listed before deletion


def _corrupt_session(session_id="20200101-000000-dead"):
    path = paths.data_dir() / "sessions" / f"{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    return path


def test_purge_all_takes_unreadable_sessions(capsys):
    readable = _session(name="ok")
    corrupt = _corrupt_session()
    assert _codebud("sessions", "--purge", "--yes") == 0
    assert not corrupt.exists()
    assert readable not in _ids()


def test_purge_by_age_keeps_unreadable_sessions(capsys):
    corrupt = _corrupt_session()
    _session(name="old", days_old=40)
    assert _codebud("sessions", "--purge", "--older-than", "30", "--yes") == 0
    assert corrupt.exists()
    assert "age is unknown" in capsys.readouterr().out


def test_a_mismatched_id_session_is_unreadable_and_purged():
    path = paths.data_dir() / "sessions" / "20200101-000000-beef.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": "../../elsewhere"}))
    assert sessions.unreadable_sessions() == ["20200101-000000-beef"]
    assert _codebud("sessions", "--purge", "--yes") == 0
    assert not path.exists()
