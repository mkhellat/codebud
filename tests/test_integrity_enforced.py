"""
tests/test_integrity_enforced.py

While an integrity manifest exists, run / plan / chat check it before
anything else and refuse on any mismatch; so does a model switch (D14h).
Ollama's digests, PATH and the agent are stand-ins.
"""

import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from codebud import integrity

DIGESTS = {"chat:7b": "c1", "all-minilm:latest": "e1", "other:1": "o1"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("git", "grep"):
        (bindir / name).write_text(f"#!/bin/sh\necho {name}\n")
        (bindir / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("OLLAMA_MODEL", "chat:7b")
    monkeypatch.delenv("EMBED_MODEL", raising=False)
    # --no-rag writes CODEBUD_RAG into os.environ; setting it here lets
    # monkeypatch restore it afterwards.
    monkeypatch.setenv("CODEBUD_RAG", "1")
    digests = dict(DIGESTS)
    monkeypatch.setattr("codebud.llm_stub.model_digests", lambda: digests)
    started = []  # chat itself is replaced: these tests are about getting there
    monkeypatch.setattr("codebud.cli.main.cmd_chat", lambda args: started.append(args))
    return {"bin": bindir, "digests": digests, "started": started}


def _pin(env, models=("chat:7b", "all-minilm:latest"), programs=("git", "grep")):
    manifest, _ = integrity.build(
        {m: env["digests"][m] for m in models}, programs, path=str(env["bin"])
    )
    integrity.save(manifest)


def _chat(*extra, before=()):
    from codebud.cli import main as cli

    with patch.object(sys, "argv", ["codebud", *before, "chat", *extra]):
        try:
            cli.main()
        except SystemExit as exc:
            return exc.code or 0
    return 0


def test_no_manifest_changes_nothing(env):
    assert _chat() == 0
    assert env["started"]


def test_matching_manifest_lets_chat_start(env):
    _pin(env)
    assert _chat() == 0
    assert env["started"]


def test_changed_binary_refuses_before_the_agent_starts(env, capsys):
    _pin(env)
    (env["bin"] / "grep").write_text("#!/bin/sh\necho evil\n")
    assert _chat() == 1
    assert not env["started"]
    err = capsys.readouterr().err
    assert "grep: contents changed" in err
    assert "codebud integrity pin" in err


def test_changed_model_refuses(env, capsys):
    _pin(env)
    env["digests"]["chat:7b"] = "c2"
    assert _chat() == 1
    assert "model chat:7b: digest changed" in capsys.readouterr().err


def test_an_unpinned_model_asked_for_with_model_flag_is_refused(env, capsys):
    _pin(env)
    assert _chat("--model", "other:1") == 1
    assert "model other:1: not pinned" in capsys.readouterr().err


def test_ollama_unreachable_is_refused_while_pinned(env, monkeypatch, capsys):
    _pin(env)
    monkeypatch.setattr("codebud.llm_stub.model_digests", lambda: None)
    assert _chat() == 1
    assert "cannot reach Ollama" in capsys.readouterr().err


def test_unpinned_embedding_model_matters_only_with_project_search(env, capsys):
    _pin(env, models=("chat:7b",))
    assert _chat() == 1
    assert "model all-minilm:latest: not pinned" in capsys.readouterr().err
    assert _chat("--no-rag") == 0


def test_damaged_manifest_is_refused(env, capsys):
    integrity.manifest_path().parent.mkdir(parents=True, exist_ok=True)
    integrity.manifest_path().write_text("{")
    assert _chat() == 1
    assert "integrity.json" in capsys.readouterr().err


def test_a_profile_can_require_a_manifest(env, capsys):
    from codebud import paths

    profiles = paths.config_dir() / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "strict.toml").write_text("integrity = true\n")
    assert _chat(before=("--profile", "strict")) == 1
    assert "requires a pinned manifest" in capsys.readouterr().err
    _pin(env)
    assert _chat(before=("--profile", "strict")) == 0


def test_profile_integrity_must_be_a_boolean(env, capsys):
    from codebud import paths

    profiles = paths.config_dir() / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "bad.toml").write_text('integrity = "yes"\n')
    assert _chat(before=("--profile", "bad")) == 2
    assert "'integrity' must be true or false" in capsys.readouterr().err


def test_model_switch_to_an_unpinned_model_is_refused(env, monkeypatch):
    from codebud.core import AgentCore

    _pin(env)
    monkeypatch.setattr("codebud.core.list_models", lambda: list(DIGESTS))
    warmed = []
    monkeypatch.setattr("codebud.core.warm_model_sync", lambda m, on_chunk=None: warmed.append(m))
    # set_model uses no agent state; a bare stand-in avoids building one.
    ok, message = AgentCore.set_model(SimpleNamespace(), "other:1")
    assert not ok
    assert "not pinned" in message
    assert warmed == []
    import os

    assert os.environ["OLLAMA_MODEL"] == "chat:7b"
