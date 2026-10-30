"""
tests/test_skill.py

codebud ships SKILL.md variants (package data) that teach other agents
(Claude Code, OpenClaw, Agensi, ...) when and how to call the codebud CLI.
`codebud skill` prints a variant or installs the default where an agent
looks for skills.
"""

import sys

import pytest

from codebud import skill


def _cli(argv, monkeypatch):
    from codebud.cli import main as run_agent

    monkeypatch.setattr(sys, "argv", ["codebud", *argv])
    try:
        run_agent.main()
    except SystemExit as exc:
        return exc.code
    return 0


# --- content ---


def test_skill_has_frontmatter_name_and_description():
    text = skill.skill_text()
    assert text.startswith("---\n")
    front = text.split("---")[1]
    assert "name: codebud" in front
    assert "description:" in front


@pytest.mark.parametrize(
    "needle",
    [
        "codebud run",
        "codebud plan",
        "codebud chat",
        "codebud doctor",
        "codebud sessions",
        "--resume",
        "--mode",
        "--yes",
        "--no-progress",
        "OLLAMA_MODEL",
        "CODEBUD_NUM_CTX",
        "codebud index",
        "--no-rag",
        "all-minilm",
    ],
)
def test_skill_documents_current_cli(needle):
    assert needle in skill.skill_text()


def test_skill_has_no_stale_source_paths():
    text = skill.skill_text()
    assert "agent/" not in text
    assert "run_agent" not in text


def test_agensi_skill_is_a_hand_off_listing():
    text = skill.skill_text("agensi")
    assert text.startswith("---\n")
    front = text.split("---")[1]
    assert "name: codebud-local" in front
    assert "github.com/mkhellat/codebud" in text
    assert "codeberg.org/mkhellat/codebud" in text
    assert "Agensi cannot" in text
    assert "codebud --no-progress run" in text
    assert "agent/" not in text


@pytest.mark.parametrize(
    "needle",
    [
        "codebud run",
        "codebud plan",
        "codebud chat",
        "codebud doctor",
        "codebud sessions",
        "--resume",
        "--mode",
        "--yes",
        "--no-progress",
        "OLLAMA_MODEL",
        "CODEBUD_NUM_CTX",
        "git clone",
        "codebud index",
        "--no-rag",
        "all-minilm",
    ],
)
def test_agensi_skill_documents_install_and_cli(needle):
    assert needle in skill.skill_text("agensi")


def test_kind_for_show_maps_agents():
    assert skill.kind_for_show(None) == "default"
    assert skill.kind_for_show("claude") == "default"
    assert skill.kind_for_show("openclaw") == "default"
    assert skill.kind_for_show("agensi") == "agensi"
    with pytest.raises(skill.SkillError, match="agensi"):
        skill.kind_for_show("vim")


def test_skill_text_rejects_unknown_kind():
    with pytest.raises(skill.SkillError, match="default, agensi"):
        skill.skill_text("vim")


def test_install_always_writes_the_default_skill(tmp_path):
    path = skill.install(tmp_path / "skills" / "codebud", kind="default")
    assert path.read_text() == skill.skill_text("default")
    assert path.read_text() != skill.skill_text("agensi")


# --- install ---


def test_target_dirs_for_known_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert skill.target_dir("claude") == tmp_path / ".claude" / "skills" / "codebud"
    assert skill.target_dir("openclaw") == tmp_path / ".openclaw" / "skills" / "codebud"
    with pytest.raises(skill.SkillError, match="claude, openclaw"):
        skill.target_dir("vim")


def test_install_writes_the_skill_and_is_idempotent(tmp_path):
    path = skill.install(tmp_path / "skills" / "codebud")
    assert path == tmp_path / "skills" / "codebud" / "SKILL.md"
    assert path.read_text() == skill.skill_text()
    assert skill.install(tmp_path / "skills" / "codebud") == path  # same content: fine


def test_install_never_overwrites_a_changed_copy_without_force(tmp_path):
    target = tmp_path / "codebud"
    target.mkdir()
    (target / "SKILL.md").write_text("my own edits\n")

    with pytest.raises(skill.SkillError, match="--force"):
        skill.install(target)
    assert (target / "SKILL.md").read_text() == "my own edits\n"

    skill.install(target, force=True)
    assert (target / "SKILL.md").read_text() == skill.skill_text()


# --- CLI ---


def test_cli_skill_show_prints_it(monkeypatch, capsys):
    assert _cli(["skill", "show"], monkeypatch) in (0, None)
    assert capsys.readouterr().out == skill.skill_text()


def test_cli_skill_show_for_agensi(monkeypatch, capsys):
    assert _cli(["skill", "show", "--for", "agensi"], monkeypatch) in (0, None)
    assert capsys.readouterr().out == skill.skill_text("agensi")


def test_cli_skill_install_for_agent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _cli(["skill", "install", "--for", "claude"], monkeypatch) in (0, None)
    installed = tmp_path / ".claude" / "skills" / "codebud" / "SKILL.md"
    assert installed.read_text() == skill.skill_text()
    assert str(installed) in capsys.readouterr().out


def test_cli_skill_install_to_dir(tmp_path, monkeypatch):
    assert _cli(["skill", "install", "--dir", str(tmp_path / "x")], monkeypatch) in (0, None)
    assert (tmp_path / "x" / "SKILL.md").exists()


def test_cli_skill_install_refuses_changed_copy(tmp_path, monkeypatch, capsys):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "SKILL.md").write_text("mine\n")
    assert _cli(["skill", "install", "--dir", str(tmp_path / "x")], monkeypatch) == 1
    assert "--force" in capsys.readouterr().err
    assert (tmp_path / "x" / "SKILL.md").read_text() == "mine\n"


# --- never through a symlink, never half-written (security review, low) ------


def test_install_refuses_a_symlinked_skill_file(tmp_path):
    target = tmp_path / "elsewhere.txt"
    target.write_text("keep me")
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "SKILL.md").symlink_to(target)
    with pytest.raises(skill.SkillError, match="symbolic link"):
        skill.install(tmp_path / "skills", force=True)
    assert target.read_text() == "keep me"


def test_install_refuses_a_dangling_symlink(tmp_path):
    (tmp_path / "skills").mkdir()
    missing = tmp_path / "would-be-created.txt"
    (tmp_path / "skills" / "SKILL.md").symlink_to(missing)
    with pytest.raises(skill.SkillError, match="symbolic link"):
        skill.install(tmp_path / "skills")
    assert not missing.exists()


def test_an_interrupted_install_leaves_the_old_copy(tmp_path, monkeypatch):
    from codebud import fsutil

    (tmp_path / "SKILL.md").write_text("old copy")

    def die(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(fsutil.os, "replace", die)
    with pytest.raises(KeyboardInterrupt):
        skill.install(tmp_path, force=True)
    assert (tmp_path / "SKILL.md").read_text() == "old copy"
