"""
tests/test_shipped_profiles.py

Profiles shipped with the security layer (phase 10, step 5 plan, PR 3;
decision D14i, packaging). The core finds codebud_sec's profiles
directory by its exact package name without importing it, and only when
it is installed alongside codebud itself; a profile of yours cannot take
a shipped profile's name, so a stray file cannot weaken it.
"""

import importlib.machinery
import importlib.util

import pytest

from codebud import paths, profiles


@pytest.fixture
def shipped(tmp_path, monkeypatch):
    directory = tmp_path / "shipped"
    (directory / "triage").mkdir(parents=True)
    (directory / "triage.toml").write_text(
        'description = "Shipped triage"\nmode = "plan"\npolicy = "triage"\n'
    )
    (directory / "triage" / "harmless_commands.json").write_text('["ps"]')
    monkeypatch.setattr(profiles, "shipped_profile_dir", lambda: directory)
    return directory


def test_shipped_profiles_are_listed_and_loaded(shipped):
    assert "triage" in profiles.available()
    profile = profiles.get("triage")
    assert profile.description == "Shipped triage"
    assert profile.mode == "plan"
    assert profile.policy == shipped / "triage"
    assert profile.source == str(shipped / "triage.toml")


def test_your_file_cannot_take_a_shipped_name(shipped):
    mine = paths.config_dir() / "profiles"
    mine.mkdir(parents=True)
    (mine / "triage.toml").write_text('mode = "full-auto"\n')
    with pytest.raises(profiles.ProfileError, match="ships with codebud"):
        profiles.get("triage")


def test_your_own_names_still_work(shipped):
    mine = paths.config_dir() / "profiles"
    mine.mkdir(parents=True)
    (mine / "my-triage.toml").write_text('mode = "ask"\n')
    assert profiles.get("my-triage").mode == "ask"
    assert {"triage", "my-triage"} <= set(profiles.available())


def test_no_security_layer_no_shipped_profiles(monkeypatch):
    monkeypatch.setattr(profiles, "shipped_profile_dir", lambda: None)
    with pytest.raises(profiles.ProfileError, match="no profile"):
        profiles.get("triage")


def test_a_codebud_sec_elsewhere_is_ignored(tmp_path, monkeypatch):
    """Found by name, but only beside codebud itself: another package of
    that name somewhere on sys.path is not the security layer."""
    fake = tmp_path / "elsewhere" / "codebud_sec"
    (fake / "data" / "profiles").mkdir(parents=True)
    (fake / "__init__.py").write_text("")
    spec = importlib.machinery.ModuleSpec("codebud_sec", None, origin=str(fake / "__init__.py"))
    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a: spec if name == "codebud_sec" else real(name, *a),
    )
    assert profiles.shipped_profile_dir() is None


def test_finding_it_does_not_import_it(monkeypatch):
    import sys

    monkeypatch.delitem(sys.modules, "codebud_sec", raising=False)
    profiles.shipped_profile_dir()
    assert "codebud_sec" not in sys.modules
