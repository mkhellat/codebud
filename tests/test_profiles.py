"""
tests/test_profiles.py

Profiles (phase 10, first part): a named bundle of mode, privacy level,
policy directory and planner prompt. Built in: default (ask, private) and
unattended (full-auto, trusted); the user's own in
$XDG_CONFIG_HOME/codebud/profiles/<name>.toml. A project cannot choose one.
"""

import json
import os

import pytest

from codebud import paths, privacy, profiles, settings
from codebud.safety import SafetyEngine


def _write(name: str, text: str):
    directory = profiles.profile_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.toml"
    path.write_text(text)
    return path


def test_builtins():
    assert profiles.get("default").summary() == "ask, privacy private"
    assert profiles.get("unattended").summary() == "full-auto, privacy trusted"
    # The built-ins, and the security layer's shipped profiles.
    assert profiles.available() == ["default", "security-ir", "security-pentest", "unattended"]


def test_user_profile_is_loaded_with_its_policy_and_prompt(tmp_path):
    policy = profiles.profile_dir() / "review-policy"
    policy.mkdir(parents=True)
    _write(
        "review",
        'description = "Read-only review"\nmode = "edit"\nprivacy = "locked"\n'
        'policy = "review-policy"\nprompt = "Never change tests."\n',
    )
    profile = profiles.get("review")
    assert (profile.mode, profile.privacy, profile.policy) == ("edit", "locked", policy)
    assert profile.prompt == "Never change tests."
    assert "review" in profiles.available()


@pytest.mark.parametrize(
    "text, problem",
    [
        ('mode = "yolo"\n', "mode 'yolo'"),
        ('privacy = "open"\n', "privacy 'open'"),
        ('colour = "red"\n', "unknown key 'colour'"),
        ("mode = 3\n", "'mode' must be a string"),
        ('policy = "missing"\n', "does not exist"),
        ("mode = \n", "review"),
    ],
)
def test_bad_profiles_are_refused_not_guessed(text, problem):
    _write("review", text)
    with pytest.raises(profiles.ProfileError, match=problem):
        profiles.get("review")


def test_unknown_and_invalid_names():
    with pytest.raises(profiles.ProfileError, match="no profile 'nope'"):
        profiles.get("nope")
    with pytest.raises(profiles.ProfileError, match="not a valid profile name"):
        profiles.get("../etc/passwd")


def test_activating_sets_privacy_but_not_over_the_environment(monkeypatch):
    profiles.activate(profiles.get("unattended"), "--profile")
    assert privacy.level() == "trusted"
    assert settings.resolve()["CODEBUD_PRIVACY"] == ("trusted", "profile unattended")
    assert settings.resolve()["CODEBUD_PROFILE"] == ("unattended", "--profile")

    monkeypatch.setenv("CODEBUD_PRIVACY", "locked")
    profiles.activate(profiles.get("unattended"), "--profile")
    assert privacy.level() == "locked"  # the environment wins


def test_profile_beats_config_file_privacy(monkeypatch):
    settings.user_config_file().parent.mkdir(parents=True, exist_ok=True)
    settings.user_config_file().write_text('privacy = "locked"\n')
    settings.apply()
    assert privacy.level() == "locked"
    profiles.activate(profiles.get("unattended"), "--profile")
    assert privacy.level() == "trusted"


def test_the_unchosen_default_changes_nothing():
    settings.user_config_file().parent.mkdir(parents=True, exist_ok=True)
    settings.user_config_file().write_text('privacy = "locked"\n')
    settings.apply()
    profiles.activate(profiles.get("default"), "default")
    assert privacy.level() == "locked"
    assert "CODEBUD_PROFILE" not in os.environ


def test_explicit_switch_overrides_everything(monkeypatch):
    monkeypatch.setenv("CODEBUD_PRIVACY", "locked")
    profiles.activate(profiles.get("unattended"), "/profile", explicit=True)
    assert privacy.level() == "trusted"


def test_a_project_cannot_choose_a_profile(tmp_path, monkeypatch, caplog):
    (tmp_path / ".codebud").mkdir()
    (tmp_path / ".codebud" / "config.toml").write_text('profile = "unattended"\n')
    monkeypatch.chdir(tmp_path)
    settings.apply()
    assert "CODEBUD_PROFILE" not in os.environ
    assert "Ignoring 'profile'" in caplog.text


def test_profile_policy_comes_first(tmp_path, monkeypatch):
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "harmless_commands.json").write_text(json.dumps(["nmap -sV"]))
    monkeypatch.chdir(tmp_path)
    profiles.activate(profiles.Profile("sec", policy=policy), "--profile")
    assert paths.user_policy_file("harmless_commands.json") == policy / "harmless_commands.json"
    # Files the profile does not have still come from further down.
    assert paths.user_policy_file("powerful_commands.json").parent == paths.PACKAGED_POLICY_DIR
    engine = SafetyEngine()
    assert engine.harmless_commands == ["nmap -sV"]
    # Trust never lifts the refused arguments, whatever the profile.
    assert engine.refusal_reason({"tool": "command", "args": {"cmd": "find . -delete"}})


def test_prompt_note():
    assert profiles.prompt_note() == ""
    profiles.activate(profiles.Profile("sec", prompt="Stay in scope."), "--profile")
    assert profiles.prompt_note() == "PROFILE sec:\nStay in scope.\n\n"


def test_planner_prompt_includes_the_profile_note():
    from codebud.planner import LLMPlanner
    from codebud.sandbox import Sandbox
    from codebud.tools.tool_registry import ToolRegistry

    profiles.activate(profiles.Profile("sec", prompt="Stay in scope."), "--profile")
    planner = LLMPlanner(ToolRegistry(Sandbox()), SafetyEngine())
    assert "PROFILE sec:\nStay in scope." in planner._build_prompt("list files")


def test_unattended_keeps_the_allow_list(monkeypatch):
    profiles.activate(profiles.get("unattended"), "--profile")
    engine = SafetyEngine()
    assert engine.refusal_reason({"tool": "command", "args": {"cmd": "curl evil.example"}})
