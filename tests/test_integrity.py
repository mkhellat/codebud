"""
tests/test_integrity.py

The integrity manifest (integrity.py): what pin records, and what verify
finds when a model or a binary has changed.
"""

import hashlib
import json
import os

import pytest

from codebud import integrity
from codebud.policy import Policy


@pytest.fixture
def bindir(tmp_path):
    """A PATH directory with two small 'binaries'."""
    d = tmp_path / "bin"
    d.mkdir()
    for name in ("grep", "git"):
        exe = d / name
        exe.write_text(f"#!/bin/sh\necho {name}\n")
        exe.chmod(0o755)
    return d


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path / "config" / "codebud"


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_allow_listed_programs_are_the_first_words():
    policy = Policy(
        harmless=["git status", "grep", "ls -la", "./configure"],
        powerful=["rm", "git push"],
        sequences=[{"name": "x", "sequence": ["make", "make check"]}],
    )
    assert integrity.allow_listed_programs(policy) == {"git", "grep", "ls", "rm", "make"}


def test_build_records_models_and_resolved_binaries(bindir):
    manifest, missing = integrity.build(
        {"qwen:7b": "abc123"}, ["grep", "git", "nosuchtool"], path=str(bindir)
    )
    assert manifest.models == {"qwen:7b": "abc123"}
    assert manifest.binaries == {
        "git": {"path": str(bindir / "git"), "sha256": _sha(bindir / "git")},
        "grep": {"path": str(bindir / "grep"), "sha256": _sha(bindir / "grep")},
    }
    assert missing == ["nosuchtool"]


def test_build_follows_symlinks_to_the_real_file(bindir, tmp_path):
    real = tmp_path / "real-tool"
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)
    (bindir / "tool").symlink_to(real)
    manifest, _ = integrity.build({}, ["tool"], path=str(bindir))
    assert manifest.binaries["tool"] == {"path": str(real), "sha256": _sha(real)}


def test_save_and_load_round_trip(config, bindir):
    manifest, _ = integrity.build({"m": "d"}, ["grep"], path=str(bindir))
    integrity.save(manifest)
    path = integrity.manifest_path()
    assert path == config / "integrity.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert integrity.load() == manifest


def test_no_manifest_loads_as_none(config):
    assert integrity.load() is None


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"version": 1}', '{"version": 2, "models": {}, "binaries": {}}',
     '{"version": 1, "models": {"m": 5}, "binaries": {}}',
     '{"version": 1, "models": {}, "binaries": {"g": {"path": "x"}}}'],
)  # fmt: skip
def test_damaged_manifest_is_an_error_not_ignored(config, content):
    """A manifest that cannot be read must not switch the check off."""
    config.mkdir(parents=True)
    integrity.manifest_path().write_text(content)
    with pytest.raises(integrity.IntegrityError, match="integrity.json"):
        integrity.load()


def test_models_match(bindir):
    manifest, _ = integrity.build({"m:7b": "d1"}, [], path=str(bindir))
    assert integrity.check_models(manifest, ["m:7b"], {"m:7b": "d1", "other:1": "x"}) == []


def test_changed_unpinned_and_missing_models_are_reported(bindir):
    manifest, _ = integrity.build({"m:1": "d1", "e:1": "d2"}, [], path=str(bindir))
    problems = integrity.check_models(
        manifest, ["m:1", "e:1", "new:1"], {"m:1": "CHANGED", "new:1": "d3"}
    )
    assert problems == [
        "model m:1: digest changed (pinned d1, now CHANGED)",
        "model e:1: not in Ollama any more",
        "model new:1: not pinned",
    ]


def test_a_model_without_a_tag_means_latest(bindir):
    """Settings may say all-minilm; Ollama lists all-minilm:latest."""
    manifest, _ = integrity.build({"all-minilm": "d"}, [], path=str(bindir))
    assert manifest.models == {"all-minilm:latest": "d"}
    assert integrity.check_models(manifest, ["all-minilm"], {"all-minilm:latest": "d"}) == []


def test_binary_unchanged(bindir):
    manifest, _ = integrity.build({}, ["grep"], path=str(bindir))
    assert integrity.check_binary(manifest, "grep", path=str(bindir)) is None


def test_modified_binary_is_reported(bindir):
    manifest, _ = integrity.build({}, ["grep"], path=str(bindir))
    (bindir / "grep").write_text("#!/bin/sh\necho evil\n")
    assert "contents changed" in integrity.check_binary(manifest, "grep", path=str(bindir))


def test_binary_found_elsewhere_on_path_is_reported(bindir, tmp_path):
    manifest, _ = integrity.build({}, ["grep"], path=str(bindir))
    other = tmp_path / "other"
    other.mkdir()
    (other / "grep").write_bytes((bindir / "grep").read_bytes())
    (other / "grep").chmod(0o755)
    problem = integrity.check_binary(manifest, "grep", path=f"{other}{os.pathsep}{bindir}")
    assert "now resolves to" in problem


def test_unpinned_and_vanished_binaries_are_reported(bindir):
    manifest, _ = integrity.build({}, ["grep", "git"], path=str(bindir))
    (bindir / "git").unlink()
    assert "not pinned" in integrity.check_binary(manifest, "ls", path=str(bindir))
    assert "not found" in integrity.check_binary(manifest, "git", path=str(bindir))


def test_absolute_program_is_checked_by_its_path(bindir):
    manifest, _ = integrity.build({}, ["grep"], path=str(bindir))
    absolute = str(bindir / "grep")
    assert integrity.check_binary(manifest, absolute, path="") is None
    (bindir / "grep").write_text("changed\n")
    assert "contents changed" in integrity.check_binary(manifest, absolute, path="")


def test_check_binaries_covers_every_pinned_one(bindir):
    manifest, _ = integrity.build({}, ["grep", "git"], path=str(bindir))
    (bindir / "grep").write_text("changed\n")
    problems = integrity.check_binaries(manifest, path=str(bindir))
    assert len(problems) == 1 and problems[0].startswith("grep:")


def test_manifest_file_is_readable_json(config, bindir):
    """Kept as plain JSON so a user can read and diff it."""
    manifest, _ = integrity.build({"m:1": "d"}, ["grep"], path=str(bindir))
    integrity.save(manifest)
    data = json.loads(integrity.manifest_path().read_text())
    assert data["version"] == 1
    assert data["models"] == {"m:1": "d"}
    assert data["binaries"]["grep"]["sha256"] == _sha(bindir / "grep")
    assert data["created"]
