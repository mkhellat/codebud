"""
tests/test_settings.py

The configuration file (phase 8, decision D12a): every codebud setting can
be set in $XDG_CONFIG_HOME/codebud/config.toml or a project's
.codebud/config.toml; environment variables override both; a project file
may not set anything that makes data leave the machine.
"""

import logging

import pytest

from codebud import paths, settings


@pytest.fixture
def env(monkeypatch):
    """A clean environment: none of codebud's variables set.

    setenv-then-delenv makes monkeypatch restore each variable afterwards,
    including ones settings.apply() writes during the test.
    """
    for s in settings.SETTINGS:
        monkeypatch.setenv(s.env, "x")
        monkeypatch.delenv(s.env)
    monkeypatch.setattr(settings, "_applied", {})
    monkeypatch.setattr(settings, "_warned", set())
    return monkeypatch


def _user_config(text):
    path = paths.config_dir() / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _project_config(root, text):
    path = root / ".codebud" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_defaults_without_files_or_environment(env, tmp_path):
    env.chdir(tmp_path)
    resolved = settings.resolve()
    assert resolved["OLLAMA_BASE_URL"] == ("http://localhost:11434", "default")
    assert resolved["OLLAMA_MODEL"] == (None, "default")


def test_user_config_is_applied_to_the_environment(env, tmp_path):
    import os

    env.chdir(tmp_path)
    _user_config('model = "qwen2.5-coder:7b"\nnum_ctx = 4096\nrag = false\n')
    settings.apply()
    assert os.environ["OLLAMA_MODEL"] == "qwen2.5-coder:7b"
    assert os.environ["CODEBUD_NUM_CTX"] == "4096"
    assert os.environ["CODEBUD_RAG"] == "0"
    assert settings.resolve()["OLLAMA_MODEL"] == ("qwen2.5-coder:7b", "user config")


def test_environment_overrides_the_files(env, tmp_path):
    import os

    env.chdir(tmp_path)
    env.setenv("OLLAMA_MODEL", "from-env")
    _user_config('model = "from-file"\n')
    settings.apply()
    assert os.environ["OLLAMA_MODEL"] == "from-env"
    assert settings.resolve()["OLLAMA_MODEL"] == ("from-env", "environment")


def test_project_config_overrides_user_config(env, tmp_path):
    env.chdir(tmp_path)
    _user_config('model = "user"\nembed_model = "nomic-embed-text"\n')
    _project_config(tmp_path, 'model = "project"\n')
    resolved = settings.resolve()
    assert resolved["OLLAMA_MODEL"] == ("project", "project config")
    assert resolved["EMBED_MODEL"] == ("nomic-embed-text", "user config")


@pytest.mark.parametrize(
    "line",
    [
        'backend = "openai"',
        'ollama_base_url = "http://evil.example:11434"',
        'openai_api_key = "sk-x"',
        "web_search = true",
    ],
)
def test_project_config_may_not_set_egress(env, tmp_path, caplog, line):
    env.chdir(tmp_path)
    _project_config(tmp_path, line + "\n")
    with caplog.at_level(logging.WARNING):
        resolved = settings.resolve()
    key = line.split()[0]
    setting = settings.BY_KEY[key]
    assert resolved[setting.env][1] == "default"
    assert "can only be set" in caplog.text and key in caplog.text


def test_user_config_may_set_egress(env, tmp_path):
    env.chdir(tmp_path)
    _user_config('ollama_base_url = "https://gpu-box:11434"\n')
    assert settings.resolve()["OLLAMA_BASE_URL"] == ("https://gpu-box:11434", "user config")


def test_bad_entries_are_ignored_with_a_warning(env, tmp_path, caplog):
    env.chdir(tmp_path)
    _user_config('modle = "typo"\nnum_ctx = "lots"\nrag = "yes"\nmodel = "ok"\n')
    with caplog.at_level(logging.WARNING):
        resolved = settings.resolve()
    assert "unknown setting 'modle'" in caplog.text
    assert "num_ctx" in caplog.text and "rag" in caplog.text
    assert resolved["CODEBUD_NUM_CTX"][1] == "default"
    assert resolved["OLLAMA_MODEL"] == ("ok", "user config")


def test_invalid_toml_is_ignored_with_a_warning(env, tmp_path, caplog):
    env.chdir(tmp_path)
    _user_config("model = \n")
    with caplog.at_level(logging.WARNING):
        resolved = settings.resolve()
    assert "config.toml" in caplog.text
    assert resolved["OLLAMA_MODEL"] == (None, "default")


def test_secrets_are_masked_for_display(env, tmp_path):
    env.chdir(tmp_path)
    env.setenv("OPENAI_API_KEY", "sk-secret")
    assert settings.display_value("OPENAI_API_KEY", "sk-secret") == "(set)"
    assert settings.display_value("OPENAI_API_KEY", None) == "(not set)"


def test_base_url_is_read_when_used_not_when_imported(env):
    from codebud import llm_stub
    from codebud.cli import doctor
    from codebud.tools import embeddings

    env.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9999")
    assert llm_stub.ollama_base() == "http://127.0.0.1:9999"
    assert doctor.ollama_base() == "http://127.0.0.1:9999"
    assert embeddings.ollama_base() == "http://127.0.0.1:9999"


def test_web_search_switch_is_read_when_used(env):
    from codebud.tools import web_search

    assert web_search.enabled() is False
    env.setenv("WEB_SEARCH_ENABLED", "1")
    assert web_search.enabled() is True


def test_config_shows_where_each_value_comes_from(env, tmp_path, capsys):
    from codebud.cli.admin import cmd_config

    env.chdir(tmp_path)
    env.setenv("OLLAMA_MODEL", "m")
    _user_config("num_ctx = 4096\n")
    settings.apply()
    cmd_config(None)
    out = capsys.readouterr().out
    model = next(ln for ln in out.splitlines() if "OLLAMA_MODEL" in ln)
    num_ctx = next(ln for ln in out.splitlines() if "CODEBUD_NUM_CTX" in ln)
    base = next(ln for ln in out.splitlines() if "OLLAMA_BASE_URL" in ln)
    assert "(environment)" in model
    assert "4096" in num_ctx and "(user config)" in num_ctx
    assert "(default)" in base
    assert "config.toml" in out


def test_each_problem_is_reported_once(env, tmp_path, caplog):
    env.chdir(tmp_path)
    _project_config(tmp_path, "web_search = true\n")
    with caplog.at_level(logging.WARNING):
        settings.apply()
        settings.resolve()
        settings.resolve()
    assert caplog.text.count("Ignoring 'web_search'") == 1


# --- one boolean parser for every switch (security review, low) -------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " True "])
def test_flag_true_values(monkeypatch, value):
    monkeypatch.setenv("CODEBUD_TEST_FLAG", value)
    assert settings.flag("CODEBUD_TEST_FLAG", False) is True


@pytest.mark.parametrize("value", ["0", "false", "No", "off"])
def test_flag_false_values(monkeypatch, value):
    monkeypatch.setenv("CODEBUD_TEST_FLAG", value)
    assert settings.flag("CODEBUD_TEST_FLAG", True) is False


def test_flag_unset_or_empty_gives_the_default(monkeypatch):
    monkeypatch.delenv("CODEBUD_TEST_FLAG", raising=False)
    assert settings.flag("CODEBUD_TEST_FLAG", True) is True
    monkeypatch.setenv("CODEBUD_TEST_FLAG", "")
    assert settings.flag("CODEBUD_TEST_FLAG", False) is False


def test_flag_unknown_warns_and_takes_the_safe_side(monkeypatch, caplog):
    monkeypatch.setattr(settings, "_warned", set())
    monkeypatch.setenv("CODEBUD_TEST_FLAG", "enabled")
    assert settings.flag("CODEBUD_TEST_FLAG", False, unknown=True) is True
    assert "not a yes/no value" in caplog.text


def test_encrypt_true_means_on(monkeypatch):
    # The bug: CODEBUD_ENCRYPT=true used to mean "off".
    from codebud import crypto

    monkeypatch.setenv("CODEBUD_ENCRYPT", "true")
    assert crypto.enabled()
    monkeypatch.setenv("CODEBUD_ENCRYPT", "maybe")  # unclear: keep data encrypted
    assert crypto.enabled()


def test_masking_stays_on_unless_clearly_off(monkeypatch):
    from codebud import redact

    monkeypatch.setenv("CODEBUD_MASK_SECRETS", "nope")
    assert redact.enabled()
    monkeypatch.setenv("CODEBUD_MASK_SECRETS", "off")
    assert not redact.enabled()


def test_egress_switches_stay_off_unless_clearly_on(monkeypatch):
    from codebud.llm_stub import remote_http_refusal
    from codebud.tools import web_search

    monkeypatch.setenv("WEB_SEARCH_ENABLED", "sure")
    assert not web_search.enabled()
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "on")
    monkeypatch.setenv("CODEBUD_PRIVACY", "private")
    assert web_search.enabled()
    monkeypatch.setenv("CODEBUD_ALLOW_REMOTE_HTTP", "probably")
    assert remote_http_refusal("http://10.0.0.5:11434")
    monkeypatch.setenv("CODEBUD_ALLOW_REMOTE_HTTP", "yes")
    assert remote_http_refusal("http://10.0.0.5:11434") is None
