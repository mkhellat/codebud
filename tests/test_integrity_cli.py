"""
tests/test_integrity_cli.py

`codebud integrity pin|verify|show` (phase 10, D14h), with Ollama's
model digests, PATH and the policy replaced by stand-ins.
"""

import json
import sys
from unittest.mock import patch

import pytest

from codebud import integrity
from codebud.policy import Policy

DIGESTS = {"chat:7b": "c1", "all-minilm:latest": "e1", "other:1": "o1"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """PATH with two tools, a chat model, Ollama answering with DIGESTS,
    an allow-list of git, grep and a tool that is not installed."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("git", "grep", "extra"):
        (bindir / name).write_text(f"#!/bin/sh\necho {name}\n")
        (bindir / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("OLLAMA_MODEL", "chat:7b")
    monkeypatch.delenv("EMBED_MODEL", raising=False)
    digests = dict(DIGESTS)
    monkeypatch.setattr("codebud.llm_stub.model_digests", lambda: digests)
    monkeypatch.setattr(
        "codebud.policy.effective",
        lambda start=None: Policy(["git status", "grep", "nosuchtool"], [], []),
    )
    monkeypatch.setattr("codebud.isolation.available", lambda: None)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    return {"bin": bindir, "digests": digests}


def _codebud(*argv):
    from codebud.cli import main as cli

    with patch.object(sys, "argv", ["codebud", *argv]):
        try:
            cli.main()
        except SystemExit as exc:
            return exc.code or 0
    return 0


def _pinned():
    return json.loads(integrity.manifest_path().read_text())


def test_pin_records_models_and_allow_listed_binaries(env, capsys):
    assert _codebud("integrity", "pin") == 0
    data = _pinned()
    assert data["models"] == {"chat:7b": "c1", "all-minilm:latest": "e1"}
    assert sorted(data["binaries"]) == ["git", "grep"]
    out = capsys.readouterr()
    assert "2 models, 2 binaries" in out.out
    assert "nosuchtool" in out.err  # on the allow-list but not installed


def test_pin_adds_named_models_and_commands(env):
    assert _codebud("integrity", "pin", "--model", "other:1", "--command", "extra") == 0
    data = _pinned()
    assert "other:1" in data["models"]
    assert "extra" in data["binaries"]


def test_pin_includes_the_isolation_tool(env, monkeypatch):
    (env["bin"] / "bwrap").write_text("#!/bin/sh\n")
    (env["bin"] / "bwrap").chmod(0o755)
    monkeypatch.setattr("codebud.isolation.available", lambda: "bwrap")
    assert _codebud("integrity", "pin") == 0
    assert "bwrap" in _pinned()["binaries"]


def test_pin_refuses_when_ollama_is_unreachable(env, monkeypatch, capsys):
    monkeypatch.setattr("codebud.llm_stub.model_digests", lambda: None)
    assert _codebud("integrity", "pin") == 1
    assert "cannot reach Ollama" in capsys.readouterr().err
    assert not integrity.manifest_path().exists()


def test_pin_refuses_without_a_chat_model(env, monkeypatch, capsys):
    monkeypatch.delenv("OLLAMA_MODEL")
    assert _codebud("integrity", "pin") == 1
    assert "OLLAMA_MODEL" in capsys.readouterr().err
    assert not integrity.manifest_path().exists()


def test_pin_refuses_a_chat_model_that_is_not_downloaded(env, monkeypatch, capsys):
    monkeypatch.setenv("OLLAMA_MODEL", "missing:1")
    assert _codebud("integrity", "pin") == 1
    assert "missing:1" in capsys.readouterr().err
    assert not integrity.manifest_path().exists()


def test_pin_warns_when_the_embedding_model_is_not_downloaded(env, capsys):
    del env["digests"]["all-minilm:latest"]
    assert _codebud("integrity", "pin") == 0
    assert _pinned()["models"] == {"chat:7b": "c1"}
    assert "all-minilm" in capsys.readouterr().err


def test_replacing_a_manifest_needs_yes(env, capsys):
    assert _codebud("integrity", "pin") == 0
    before = integrity.manifest_path().read_text()
    (env["bin"] / "git").write_text("#!/bin/sh\necho changed\n")
    assert _codebud("integrity", "pin") == 1  # no terminal, no --yes
    assert integrity.manifest_path().read_text() == before
    assert "--yes" in capsys.readouterr().err
    assert _codebud("integrity", "pin", "--yes") == 0
    assert integrity.manifest_path().read_text() != before


def test_pin_replaces_a_damaged_manifest_with_yes(env):
    integrity.manifest_path().parent.mkdir(parents=True, exist_ok=True)
    integrity.manifest_path().write_text("not json")
    assert _codebud("integrity", "pin", "--yes") == 0
    assert _pinned()["version"] == 1


def test_verify_passes_when_nothing_changed(env, capsys):
    _codebud("integrity", "pin")
    capsys.readouterr()
    assert _codebud("integrity", "verify") == 0
    assert "matches" in capsys.readouterr().out


def test_verify_reports_a_changed_binary_and_model(env, capsys):
    _codebud("integrity", "pin")
    (env["bin"] / "grep").write_text("#!/bin/sh\necho evil\n")
    env["digests"]["chat:7b"] = "c2"
    capsys.readouterr()
    assert _codebud("integrity", "verify") == 1
    err = capsys.readouterr().err
    assert "grep: contents changed" in err
    assert "model chat:7b: digest changed" in err


def test_verify_reports_an_unpinned_chat_model(env, monkeypatch, capsys):
    _codebud("integrity", "pin")
    monkeypatch.setenv("OLLAMA_MODEL", "other:1")
    assert _codebud("integrity", "verify") == 1
    assert "model other:1: not pinned" in capsys.readouterr().err


def test_verify_without_a_manifest_fails(env, capsys):
    assert _codebud("integrity", "verify") == 1
    assert "nothing is pinned" in capsys.readouterr().err


def test_verify_with_a_damaged_manifest_fails(env, capsys):
    integrity.manifest_path().parent.mkdir(parents=True, exist_ok=True)
    integrity.manifest_path().write_text("{}")
    assert _codebud("integrity", "verify") == 1
    assert "integrity.json" in capsys.readouterr().err


def test_verify_fails_when_ollama_is_unreachable(env, monkeypatch, capsys):
    _codebud("integrity", "pin")
    monkeypatch.setattr("codebud.llm_stub.model_digests", lambda: None)
    assert _codebud("integrity", "verify") == 1
    assert "cannot reach Ollama" in capsys.readouterr().err


def test_show_lists_what_is_pinned(env, capsys):
    _codebud("integrity", "pin")
    capsys.readouterr()
    assert _codebud("integrity", "show") == 0
    out = capsys.readouterr().out
    assert "chat:7b" in out and "c1" in out
    assert str(env["bin"] / "git") in out


def test_every_subcommand_is_known():
    """A subcommand missing from KNOWN_SUBCOMMANDS is run as `codebud run
    <name> ...` -- `codebud integrity pin` once was."""
    import argparse

    from codebud.cli.parser import KNOWN_SUBCOMMANDS, build_parser

    action = next(a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    assert set(action.choices) == KNOWN_SUBCOMMANDS


def test_show_without_a_manifest(env, capsys):
    assert _codebud("integrity", "show") == 0
    assert "Nothing is pinned" in capsys.readouterr().out


@pytest.fixture
def shipped(tmp_path, monkeypatch):
    """A shipped profile whose policy allows `extra`."""
    from codebud import profiles

    directory = tmp_path / "shipped"
    (directory / "triage").mkdir(parents=True)
    (directory / "triage.toml").write_text('policy = "triage"\n')
    (directory / "triage" / "harmless_commands.json").write_text('["extra --version"]')
    monkeypatch.setattr(profiles, "shipped_profile_dir", lambda: directory)
    return directory


def test_pin_covers_shipped_profiles_and_their_programs(env, shipped):
    """Pinned under the default profile, the security profiles' own tools
    must be pinned too, or every one of their steps would be refused."""
    assert _codebud("integrity", "pin") == 0
    data = _pinned()
    assert "extra" in data["binaries"]
    assert str(shipped / "triage.toml") in data["files"]
    assert str(shipped / "triage" / "harmless_commands.json") in data["files"]


def test_verify_reports_a_changed_shipped_profile(env, shipped, capsys):
    _codebud("integrity", "pin")
    (shipped / "triage.toml").write_text('policy = "triage"\nmode = "full-auto"\n')
    capsys.readouterr()
    assert _codebud("integrity", "verify") == 1
    assert "triage.toml: contents changed" in capsys.readouterr().err
