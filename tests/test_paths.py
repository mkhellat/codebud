"""
tests/test_paths.py

Where codebud keeps its files (decision D5b). Nothing may depend on the
directory codebud is started from:

- runtime data (memory, sessions, embeddings) in $XDG_DATA_HOME/codebud;
- policy files looked up in a project's .codebud/ (found by walking up
  from the current directory, like .git), then $XDG_CONFIG_HOME/codebud,
  then the defaults shipped with codebud;
- data left in the old checkout-relative data/ is moved once.
"""

import json
from pathlib import Path

import pytest

from codebud import paths


def test_data_dir_follows_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    assert paths.data_dir() == tmp_path / "d" / "codebud"


def test_data_dir_defaults_to_local_share(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.data_dir() == tmp_path / ".local" / "share" / "codebud"


def test_config_dir_follows_xdg_and_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    assert paths.config_dir() == tmp_path / "c" / "codebud"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.config_dir() == tmp_path / ".config" / "codebud"


def test_project_dir_is_found_within_the_git_work_tree(tmp_path):
    (tmp_path / "proj" / ".git").mkdir(parents=True)
    (tmp_path / "proj" / ".codebud").mkdir()
    deep = tmp_path / "proj" / "src" / "pkg"
    deep.mkdir(parents=True)
    assert paths.project_config_dir(deep) == tmp_path / "proj" / ".codebud"
    assert paths.project_config_dir(tmp_path) is None


def test_the_search_stops_at_the_git_work_tree(tmp_path):
    # A .codebud/ above the repository (e.g. /tmp/.codebud) does not apply.
    (tmp_path / ".codebud").mkdir()
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    assert paths.project_config_dir(tmp_path / "repo") is None


def test_outside_git_only_the_directory_itself_counts(tmp_path):
    (tmp_path / ".codebud").mkdir()
    (tmp_path / "sub").mkdir()
    assert paths.project_config_dir(tmp_path) == tmp_path / ".codebud"
    assert paths.project_config_dir(tmp_path / "sub") is None


def test_home_is_never_a_project(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".codebud").mkdir()
    assert paths.project_config_dir(tmp_path) is None


def test_a_codebud_dir_owned_by_someone_else_is_ignored(tmp_path, monkeypatch, caplog):
    (tmp_path / ".codebud").mkdir()
    monkeypatch.setattr(paths.os, "getuid", lambda: 4242)
    assert paths.project_config_dir(tmp_path) is None
    assert "another user" in caplog.text


def test_policy_file_lookup(tmp_path, monkeypatch):
    name = "harmless_commands.json"
    assert paths.user_policy_file(name) == paths.PACKAGED_POLICY_DIR / name

    user = paths.config_dir()
    user.mkdir(parents=True)
    (user / name).write_text("[]")
    assert paths.user_policy_file(name) == user / name

    project = tmp_path / "proj" / ".codebud"
    project.mkdir(parents=True)
    (project / name).write_text("[]")
    assert paths.project_policy_file(name, start=tmp_path / "proj") == project / name
    assert paths.user_policy_file(name) == user / name  # a project never replaces yours


def test_policy_loads_from_any_working_directory(tmp_path, monkeypatch):
    """The bug: run outside the checkout, the policy loaded empty."""
    from codebud.safety import SafetyEngine

    monkeypatch.chdir(tmp_path)
    engine = SafetyEngine()
    assert "ls" in engine.harmless_commands
    assert "pytest" in engine.powerful_commands


def test_project_policy_narrows_until_trusted(tmp_path, monkeypatch):
    from codebud import policy
    from codebud.safety import SafetyEngine

    (tmp_path / ".codebud").mkdir()
    (tmp_path / ".codebud" / "harmless_commands.json").write_text(json.dumps(["ls", "tree"]))
    monkeypatch.chdir(tmp_path)
    engine = SafetyEngine()
    assert engine.harmless_commands == ["ls"]  # narrowed; "tree" waits for trust
    assert "pytest" in engine.powerful_commands  # other files still yours
    policy.trust(tmp_path / ".codebud")
    assert SafetyEngine().harmless_commands == ["ls", "tree"]


def test_memory_and_sessions_live_in_the_data_dir(tmp_path, monkeypatch):
    from codebud import memory, sessions

    monkeypatch.chdir(tmp_path)
    memory.MemoryStore().add_snapshot({"tool": "command"}, {"returncode": 0})
    s = sessions.Session.create()
    s.close()

    assert (paths.data_dir() / "memory" / "entries.jsonl").exists()
    assert (paths.data_dir() / "sessions" / f"{s.id}.json").exists()
    assert not (tmp_path / "data").exists()


def test_legacy_data_is_copied_once(tmp_path, monkeypatch):
    legacy = tmp_path / "old-checkout" / "data"
    (legacy / "memory").mkdir(parents=True)
    (legacy / "memory" / "entries.jsonl").write_text('{"x": 1}\n')
    (legacy / "sessions").mkdir()
    (legacy / "sessions" / "20261025-000000-abcd.json").write_text("{}")
    monkeypatch.setattr(paths, "LEGACY_DATA_DIR", legacy)

    copied = paths.migrate_legacy_data()

    new = paths.data_dir()
    assert (new / "memory" / "entries.jsonl").read_text() == '{"x": 1}\n'
    assert (new / "sessions" / "20261025-000000-abcd.json").exists()
    assert len(copied) == 2
    assert paths.migrate_legacy_data() == []  # already copied


def test_legacy_migration_never_overwrites(tmp_path, monkeypatch):
    legacy = tmp_path / "old" / "data"
    (legacy / "memory").mkdir(parents=True)
    (legacy / "memory" / "entries.jsonl").write_text("old\n")
    monkeypatch.setattr(paths, "LEGACY_DATA_DIR", legacy)
    target = paths.data_dir() / "memory"
    target.mkdir(parents=True)
    (target / "entries.jsonl").write_text("new\n")

    assert paths.migrate_legacy_data() == []
    assert (target / "entries.jsonl").read_text() == "new\n"
    assert (legacy / "memory" / "entries.jsonl").read_text() == "old\n"


def test_packaged_policy_dir_has_the_default_files():
    for name in ["harmless_commands.json", "powerful_commands.json", "trusted_sequences.json"]:
        assert (Path(paths.PACKAGED_POLICY_DIR) / name).is_file()


def test_config_subcommand_shows_data_dir_and_policy_source(tmp_path, monkeypatch, capsys):
    import sys

    from codebud.cli import main as run_agent

    (tmp_path / ".codebud").mkdir()
    (tmp_path / ".codebud" / "harmless_commands.json").write_text('["ls"]')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["codebud", "config"])
    run_agent.main()
    out = capsys.readouterr().out
    assert str(paths.data_dir()) in out
    assert str(tmp_path / ".codebud" / "harmless_commands.json") in out


def test_legacy_migration_never_deletes_the_originals(tmp_path, monkeypatch):
    """User data must survive even if the copy goes somewhere unexpected."""
    legacy = tmp_path / "old" / "data"
    (legacy / "memory").mkdir(parents=True)
    (legacy / "memory" / "entries.jsonl").write_text("precious\n")
    monkeypatch.setattr(paths, "LEGACY_DATA_DIR", legacy)

    paths.migrate_legacy_data()

    assert (legacy / "memory" / "entries.jsonl").read_text() == "precious\n"


def test_child_processes_see_the_isolated_legacy_dir(tmp_path):
    """Regression: a test that ran the real `codebud` in a subprocess
    inherited the temporary XDG dirs but not the in-process override of the
    legacy dir, and its migration carried the checkout's real data/ away."""
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-c", "from codebud import paths; print(paths.legacy_data_dir())"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == str(paths.legacy_data_dir())
    assert out != str(paths._SOURCE_ROOT / "data")


def test_python_dash_m_codebud_runs_the_cli():
    """`python -m codebud` replaces the old `python run_agent.py` entry."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "codebud", "--help"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "usage: codebud" in result.stdout


def _pyproject_version():
    import re

    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    return re.search(r'^version = "(.+)"$', text, re.M).group(1)


def test_version_matches_pyproject(capsys, monkeypatch):
    """One source of truth: pyproject's version, installed or not."""
    import sys

    import codebud
    from codebud.cli import main as run_agent

    monkeypatch.setattr(sys, "argv", ["codebud", "version"])
    run_agent.main()
    assert codebud.__version__ == _pyproject_version()
    assert f"codebud  {_pyproject_version()}" in capsys.readouterr().out


def test_version_from_source_tree_without_installed_metadata(monkeypatch):
    """Run from an uninstalled checkout (e.g. make check): read pyproject."""
    import importlib.metadata

    import codebud

    def not_installed(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", not_installed)
    assert codebud._find_version() == _pyproject_version()


@pytest.mark.parametrize(
    "var, getter", [("XDG_DATA_HOME", "data_dir"), ("XDG_CONFIG_HOME", "config_dir")]
)
def test_a_relative_xdg_path_is_ignored(monkeypatch, caplog, var, getter):
    # XDG spec; honouring it put data or keys in the current directory.
    monkeypatch.setenv(var, "relative/dir")
    result = getattr(paths, getter)()
    assert result.is_absolute()
    assert "relative" not in result.parts
    assert "must be an absolute path" in caplog.text


# --- legacy migration: no symlinks, encrypted when encryption is on ---------


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    root = tmp_path / "old" / "data"
    (root / "memory").mkdir(parents=True)
    (root / "sessions").mkdir()
    monkeypatch.setattr(paths, "LEGACY_DATA_DIR", root)
    return root


def test_legacy_migration_skips_symlinks(legacy, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("not codebud's")
    (legacy / "sessions" / "20261025-000000-abcd.json").symlink_to(secret)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.jsonl").write_text("{}\n")
    (legacy / "linked").symlink_to(elsewhere, target_is_directory=True)
    assert paths.migrate_legacy_data() == []
    assert not (paths.data_dir() / "sessions" / "20261025-000000-abcd.json").exists()
    assert not (paths.data_dir() / "linked").exists()


def test_legacy_migration_encrypts_when_encryption_is_on(legacy, monkeypatch, fast_passphrase):
    pytest.importorskip("pyrage")
    from codebud import crypto

    crypto.create_key("migration passphrase")
    crypto.lock()
    monkeypatch.setenv("CODEBUD_ENCRYPT", "1")
    (legacy / "sessions" / "20261025-000000-abcd.json").write_text('{"history": ["SECRET"]}')
    (legacy / "memory" / "entries.json").write_text(json.dumps([{"cmd": "SECRET-1"}]))
    copied = paths.migrate_legacy_data()
    new = paths.data_dir()
    session = (new / "sessions" / "20261025-000000-abcd.json").read_bytes()
    history = (new / "memory" / "entries.jsonl").read_text()
    assert len(copied) == 2
    assert crypto.is_encrypted(session) and b"SECRET" not in session
    assert history.startswith("age:") and "SECRET" not in history
    assert not (new / "memory" / "entries.json").exists()  # converted, not copied
    assert (legacy / "memory" / "entries.json").exists()  # original kept


def test_legacy_migration_waits_when_encryption_cannot_work(legacy, monkeypatch):
    monkeypatch.setenv("CODEBUD_ENCRYPT", "1")  # but no key yet
    (legacy / "sessions" / "20261025-000000-abcd.json").write_text("{}")
    assert paths.migrate_legacy_data() == []
    assert not (paths.data_dir() / "sessions").exists()


def test_a_damaged_legacy_history_is_left_alone(legacy, monkeypatch, fast_passphrase):
    pytest.importorskip("pyrage")
    from codebud import crypto

    crypto.create_key("migration passphrase")
    monkeypatch.setenv("CODEBUD_ENCRYPT", "1")
    (legacy / "memory" / "entries.json").write_text("{damaged")
    assert paths.migrate_legacy_data() == []
    assert (legacy / "memory" / "entries.json").read_text() == "{damaged"
