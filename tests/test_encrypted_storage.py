"""
tests/test_encrypted_storage.py

Encryption at rest for sessions and the step history (phase 10, D14g).
With `encrypt` on, sessions are age files and history lines are
age:<base64>. Saving needs only the public key -- running steps and
saving a chat never ask for the passphrase -- while reading unlocks the
key once. Plaintext written before stays readable. Encryption on without
a usable key refuses to start rather than store plaintext.
"""

import json
import sys
from unittest.mock import patch

import pytest

pytest.importorskip("pyrage")

from codebud import crypto, memory, paths, profiles, sessions  # noqa: E402
from codebud.cli import doctor  # noqa: E402

pytestmark = pytest.mark.usefixtures("fast_passphrase")  # see conftest.py

PASSPHRASE = "s3cret"


@pytest.fixture
def no_prompt(monkeypatch):
    """Fail the test if anything asks for the passphrase."""
    monkeypatch.setattr("getpass.getpass", lambda _p="": pytest.fail("asked for the passphrase"))


@pytest.fixture
def typed(monkeypatch):
    """Answer passphrase prompts; records how often it was asked."""
    asked = []

    def ask(_prompt=""):
        asked.append(_prompt)
        return PASSPHRASE

    monkeypatch.setattr("getpass.getpass", ask)
    return asked


@pytest.fixture
def encrypted(monkeypatch):
    """A key exists, encryption is on, and the key is locked."""
    crypto.create_key(PASSPHRASE)
    crypto.lock()
    monkeypatch.setenv("CODEBUD_ENCRYPT", "1")


def _codebud(*argv):
    from codebud.cli import main as cli

    with patch.object(sys, "argv", ["codebud", *argv]):
        try:
            cli.main()
        except SystemExit as exc:
            return exc.code or 0
    return 0


def _session_path(session_id):
    return paths.data_dir() / "sessions" / f"{session_id}.json"


def _history_lines():
    return (paths.data_dir() / "memory" / "entries.jsonl").read_text().splitlines()


# --- sessions ---------------------------------------------------------------


def test_session_is_saved_encrypted_without_the_passphrase(encrypted, no_prompt):
    with sessions.Session.create() as s:
        s.save([{"role": "user", "content": "refactor the parser"}], model="m")
    raw = _session_path(s.id).read_bytes()
    assert crypto.is_encrypted(raw)
    assert b"refactor the parser" not in raw


def test_encrypted_session_reopens_after_one_prompt(encrypted, typed):
    with sessions.Session.create() as s:
        s.save([{"role": "user", "content": "hi"}], model="m")
    with sessions.Session.open(s.id) as again:
        assert again.history == [{"role": "user", "content": "hi"}]
    assert len(sessions.list_sessions()) == 1
    assert len(typed) == 1  # unlocked once for the whole run


def test_plaintext_sessions_stay_readable(typed, monkeypatch):
    with sessions.Session.create(name="old") as s:
        s.save([{"role": "user", "content": "before"}], model="m")
    assert not crypto.is_encrypted(_session_path(s.id).read_bytes())
    crypto.create_key(PASSPHRASE)
    crypto.lock()
    monkeypatch.setenv("CODEBUD_ENCRYPT", "1")
    with sessions.Session.create() as new:
        new.save([{"role": "user", "content": "after"}], model="m")
    firsts = sorted(x["first_request"] for x in sessions.list_sessions())
    assert firsts == ["after", "before"]


def test_a_session_that_cannot_be_decrypted_is_skipped(encrypted, typed, caplog):
    from pyrage import x25519

    other = x25519.Identity.generate().to_public()
    import pyrage

    _session_path("20260101-000000-dead").parent.mkdir(parents=True, exist_ok=True)
    _session_path("20260101-000000-dead").write_bytes(pyrage.encrypt(b"{}", [other]))
    assert sessions.list_sessions() == []
    assert "Skipping session" in caplog.text


def test_failing_to_unlock_stops_instead_of_skipping(encrypted, monkeypatch):
    with sessions.Session.create() as s:
        s.save([{"role": "user", "content": "hi"}], model="m")
    monkeypatch.setattr("getpass.getpass", lambda _p="": "wrong")
    with pytest.raises(crypto.CryptoError, match="wrong passphrase"):
        sessions.list_sessions()


# --- step history -----------------------------------------------------------


def test_history_lines_are_encrypted_without_the_passphrase(encrypted, no_prompt):
    store = memory.MemoryStore()  # constructing it reads nothing
    store.add_snapshot({"tool": "command", "cmd": "cat .env"}, {"output": "x"})
    (line,) = _history_lines()
    assert crypto.is_encrypted_line(line)
    assert "cat .env" not in line


def test_history_reads_encrypted_and_plaintext_lines(typed, monkeypatch):
    memory.MemoryStore().add_snapshot({"cmd": "before"}, {})
    crypto.create_key(PASSPHRASE)
    crypto.lock()
    monkeypatch.setenv("CODEBUD_ENCRYPT", "1")
    memory.MemoryStore().add_snapshot({"cmd": "after"}, {})
    assert not crypto.is_encrypted_line(_history_lines()[0])
    assert crypto.is_encrypted_line(_history_lines()[1])
    cmds = [e["data"]["step"]["cmd"] for e in memory.MemoryStore().history]
    assert cmds == ["before", "after"]
    assert len(typed) == 1


def test_history_cli_with_the_environment_passphrase(encrypted, no_prompt, monkeypatch, capsys):
    memory.MemoryStore().add_snapshot({"tool": "command", "cmd": "ls"}, {"success": True})
    monkeypatch.setenv("CODEBUD_PASSPHRASE", PASSPHRASE)
    assert _codebud("history", "--json") == 0
    entries = json.loads(capsys.readouterr().out)
    assert entries[0]["data"]["step"]["cmd"] == "ls"


def test_history_cli_reports_a_failed_unlock(encrypted, monkeypatch, capsys):
    memory.MemoryStore().add_snapshot({"cmd": "ls"}, {})
    monkeypatch.setattr("getpass.getpass", lambda _p="": "wrong")
    assert _codebud("history") == 1
    assert "wrong passphrase" in capsys.readouterr().err


def test_no_terminal_refuses_and_never_reads_stdin(encrypted, no_prompt, monkeypatch):
    # getpass without a terminal reads stdin with echo; codebud must not call it.
    memory.MemoryStore().add_snapshot({"cmd": "ls"}, {})
    monkeypatch.setattr(crypto, "_terminal_available", lambda: False)
    with pytest.raises(crypto.CryptoError, match="CODEBUD_PASSPHRASE"):
        _ = memory.MemoryStore().history


def test_input_ending_at_the_prompt(encrypted, monkeypatch):
    memory.MemoryStore().add_snapshot({"cmd": "ls"}, {})

    def eof(_p=""):
        raise EOFError

    monkeypatch.setattr("getpass.getpass", eof)
    with pytest.raises(crypto.CryptoError, match="CODEBUD_PASSPHRASE"):
        _ = memory.MemoryStore().history


def test_key_init_without_a_terminal(no_prompt, monkeypatch, capsys):
    monkeypatch.setattr(crypto, "_terminal_available", lambda: False)
    assert _codebud("key", "init") == 1
    assert "no terminal" in capsys.readouterr().err
    assert not crypto.has_key()


# --- the switch -------------------------------------------------------------


def test_on_without_a_key_refuses_to_start(monkeypatch, capsys):
    monkeypatch.setenv("CODEBUD_ENCRYPT", "1")
    assert _codebud("history") == 1
    assert "codebud key init" in capsys.readouterr().err
    assert not (paths.data_dir() / "memory" / "entries.jsonl").exists()


def test_on_without_a_key_fails_doctor(monkeypatch):
    monkeypatch.setenv("CODEBUD_ENCRYPT", "1")
    passed, label, hint = doctor.check_encryption()
    assert passed is False and "codebud key init" in hint


def test_doctor_when_on(encrypted):
    passed, label, _ = doctor.check_encryption()
    assert passed is True and "on (sessions, step history" in label


def test_config_file_turns_it_on(tmp_path):
    from codebud import settings

    settings.user_config_file().parent.mkdir(parents=True, exist_ok=True)
    settings.user_config_file().write_text("encrypt = true\n")
    settings.apply()
    assert crypto.enabled()


def test_a_project_file_cannot_turn_it_off(tmp_path, monkeypatch, caplog):
    from codebud import settings

    project = tmp_path / "proj" / ".codebud"
    project.mkdir(parents=True)
    (project / "config.toml").write_text("encrypt = false\n")
    monkeypatch.chdir(tmp_path / "proj")
    assert "CODEBUD_ENCRYPT" not in settings._file_values()
    assert "protects your stored data" in caplog.text


def test_a_profile_turns_it_on(monkeypatch):
    profiles.profile_dir().mkdir(parents=True)
    (profiles.profile_dir() / "sealed.toml").write_text("encrypt = true\n")
    profile = profiles.get("sealed")
    assert profile.encrypt and "encrypted" in profile.summary()
    monkeypatch.setenv("CODEBUD_ENCRYPT", "0")
    profiles.activate(profile, "--profile")
    assert crypto.enabled()  # a profile can only turn it on, and that wins


def test_a_profile_encrypt_must_be_a_boolean():
    profiles.profile_dir().mkdir(parents=True)
    (profiles.profile_dir() / "bad.toml").write_text('encrypt = "yes"\n')
    with pytest.raises(profiles.ProfileError, match="true or false"):
        profiles.get("bad")


def test_legacy_history_is_migrated_encrypted(encrypted, no_prompt):
    # Audit finding L4: the one-time entries.json migration wrote plaintext.
    legacy = paths.data_dir() / "memory" / "entries.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    # A plaintext with spaces: base64 ciphertext never contains a space, so
    # it cannot match by chance (a bare "old" once did: "...q6oldYI").
    secret = "ls the old migrated logs"
    legacy.write_text(json.dumps([{"timestamp": "t", "data": {"step": {"cmd": secret}}}]))
    memory.MemoryStore()
    (line,) = _history_lines()
    assert crypto.is_encrypted_line(line) and secret not in line
    assert (legacy.parent / "entries.json.migrated").exists()  # the original is kept
