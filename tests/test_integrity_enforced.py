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


def test_doctor_without_a_manifest_passes_and_says_how_to_pin(env):
    from codebud.cli import doctor

    passed, label, _hint = doctor.check_integrity()
    assert passed is True
    assert "nothing pinned" in label and "codebud integrity pin" in label


def test_doctor_with_a_matching_manifest(env):
    from codebud.cli import doctor

    _pin(env)
    passed, label, _hint = doctor.check_integrity()
    assert passed is True
    assert "2 models, 2 binaries" in label and "all match" in label


def test_doctor_fails_on_a_mismatch_the_next_run_would_refuse(env):
    from codebud.cli import doctor

    _pin(env)
    (env["bin"] / "git").write_text("#!/bin/sh\necho evil\n")
    passed, label, hint = doctor.check_integrity()
    assert passed is False
    assert "1 mismatch" in label
    assert "git: contents changed" in hint and "codebud integrity verify" in hint


def test_doctor_fails_on_a_damaged_manifest(env):
    from codebud.cli import doctor

    integrity.manifest_path().parent.mkdir(parents=True, exist_ok=True)
    integrity.manifest_path().write_text("nope")
    passed, _label, hint = doctor.check_integrity()
    assert passed is False
    assert "integrity.json" in hint


def test_doctor_fails_when_the_profile_requires_a_manifest(env, monkeypatch):
    from codebud import profiles
    from codebud.cli import doctor

    monkeypatch.setattr(profiles, "_active", profiles.Profile("strict", integrity=True))
    passed, label, _hint = doctor.check_integrity()
    assert passed is False
    assert "profile strict requires" in label


class _RagAgent:
    def __init__(self):
        self.rag = False

    def set_rag(self, on):
        self.rag = on

    def get_rag(self):
        return self.rag


def test_rag_on_refuses_an_unpinned_embedding_model(env, capsys):
    """Started with --no-rag (embedding model not checked), /rag on must
    not start using it unchecked."""
    from codebud.cli import chat

    _pin(env, models=("chat:7b",))
    state = SimpleNamespace(agent=_RagAgent())
    chat._slash_rag(state, "on")
    assert state.agent.rag is False
    assert "model all-minilm:latest: not pinned" in capsys.readouterr().err


def test_rag_on_with_a_pinned_embedding_model(env, monkeypatch, capsys):
    from codebud.cli import chat

    _pin(env)
    monkeypatch.setattr("codebud.rag.indexer.index_status", lambda root: None)
    state = SimpleNamespace(agent=_RagAgent())
    chat._slash_rag(state, "on")
    assert state.agent.rag is True


def _index(*argv):
    from codebud.cli import main as cli

    with patch.object(sys, "argv", ["codebud", "index", *argv]):
        try:
            cli.main()
        except SystemExit as exc:
            return exc.code or 0
    return 0


def test_index_refuses_a_changed_embedding_model(env, monkeypatch, capsys):
    _pin(env)
    env["digests"]["all-minilm:latest"] = "e2"
    built = []
    monkeypatch.setattr("codebud.rag.indexer.index_project", lambda *a, **kw: built.append(1))
    assert _index() == 1
    assert not built
    assert "model all-minilm:latest: digest changed" in capsys.readouterr().err


def test_index_status_needs_no_model_check(env, monkeypatch):
    _pin(env)
    env["digests"]["all-minilm:latest"] = "e2"
    monkeypatch.setattr("codebud.rag.indexer.index_status", lambda root: None)
    assert _index("--status") == 0


class _ProfileAgent:
    def __init__(self):
        self.mode = None

    def reload_policy(self):
        pass

    def set_mode(self, mode):
        self.mode = mode


def _strict_profile():
    from codebud import paths

    profiles_dir = paths.config_dir() / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    (profiles_dir / "strict.toml").write_text('integrity = true\nmode = "plan"\n')


def test_switching_to_a_profile_that_requires_a_manifest_is_refused(env, capsys):
    from codebud import profiles
    from codebud.cli import chat

    _strict_profile()
    state = SimpleNamespace(agent=_ProfileAgent())
    chat._slash_profile(state, "strict")
    assert profiles.active().name == "default"
    assert state.agent.mode is None
    assert "requires a pinned manifest" in capsys.readouterr().err


def test_switching_to_it_with_a_manifest_works(env):
    from codebud import profiles
    from codebud.cli import chat

    _strict_profile()
    _pin(env)
    state = SimpleNamespace(agent=_ProfileAgent())
    chat._slash_profile(state, "strict")
    assert profiles.active().name == "strict"
    assert state.agent.mode == "plan"
