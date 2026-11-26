"""
tests/test_sampling.py

CODEBUD_TEMPERATURE and CODEBUD_SEED make runs repeatable (the chat
evaluation uses them). When set they go in the "options" of every model
request; when unset or invalid nothing is added to what was sent before.
The keep-alive heartbeat runs nothing, so it never carries them.
"""

import json
import sys
import types

import pytest

from codebud import llm_stub, settings


class _FakeResponse:
    def raise_for_status(self):
        pass

    def iter_lines(self):
        yield json.dumps({"response": "{}", "done": True}).encode()


@pytest.fixture
def sent(monkeypatch):
    payloads = []

    def fake_post(url, json=None, **kwargs):
        payloads.append(json)
        return _FakeResponse()

    class _InlineThread:
        def __init__(self, target, **kwargs):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setenv("OLLAMA_MODEL", "m")
    monkeypatch.delenv("CODEBUD_TEMPERATURE", raising=False)
    monkeypatch.delenv("CODEBUD_SEED", raising=False)
    monkeypatch.setattr(llm_stub, "_ollama_available", lambda: True)
    monkeypatch.setattr(llm_stub.requests, "post", fake_post)
    monkeypatch.setattr(llm_stub.threading, "Thread", _InlineThread)
    monkeypatch.setattr(llm_stub, "_warned_sampling", set())
    return payloads


def _generating_requests():
    llm_stub.call_llm("plan this")
    llm_stub.call_llm_plain("hello")
    llm_stub.warm_model_sync("m")
    llm_stub.prewarm_model()


def test_set_values_reach_every_generating_request(sent, monkeypatch):
    monkeypatch.setenv("CODEBUD_TEMPERATURE", "0.3")
    monkeypatch.setenv("CODEBUD_SEED", "7")
    _generating_requests()
    assert len(sent) == 4
    for payload in sent:
        assert payload["options"]["temperature"] == 0.3
        assert payload["options"]["seed"] == 7


def test_temperature_zero_is_sent(sent, monkeypatch):
    monkeypatch.setenv("CODEBUD_TEMPERATURE", "0")
    llm_stub.warm_model_sync("m")
    assert sent[0]["options"]["temperature"] == 0.0


def test_unset_sends_no_seed_and_no_new_temperature(sent):
    _generating_requests()
    for payload in sent:
        assert "seed" not in payload["options"]
    # The planner's own call has always asked for temperature 0.
    assert sent[0]["options"]["temperature"] == 0
    assert "temperature" not in sent[2]["options"]


def test_heartbeat_never_carries_them(sent, monkeypatch):
    monkeypatch.setenv("CODEBUD_TEMPERATURE", "0")
    monkeypatch.setenv("CODEBUD_SEED", "1")
    llm_stub.ModelHeartbeat()._ping()
    assert sent[0]["options"] == {"num_ctx": 8192}


@pytest.mark.parametrize(
    ("env", "bad"),
    [
        ("CODEBUD_TEMPERATURE", "hot"),
        ("CODEBUD_TEMPERATURE", "nan"),
        ("CODEBUD_SEED", "abc"),
        ("CODEBUD_SEED", "1.5"),
    ],
)
def test_invalid_values_are_ignored_with_one_warning(sent, monkeypatch, caplog, env, bad):
    monkeypatch.setenv(env, bad)
    with caplog.at_level("WARNING"):
        llm_stub.call_llm("a")
        llm_stub.call_llm("b")
    assert "seed" not in sent[0]["options"]
    assert sent[0]["options"]["temperature"] == 0  # the planner's own default
    assert caplog.text.count(f"Ignoring invalid {env}") == 1


def test_openai_gets_them_too(monkeypatch):
    calls = []
    choice = types.SimpleNamespace(message=types.SimpleNamespace(content="x"))

    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return types.SimpleNamespace(choices=[choice])

    fake = types.ModuleType("openai")
    fake.OpenAI = lambda: types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=Completions())
    )
    monkeypatch.setitem(sys.modules, "openai", fake)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.delenv("CODEBUD_SEED", raising=False)
    monkeypatch.delenv("CODEBUD_TEMPERATURE", raising=False)
    llm_stub._call_openai("p")
    assert calls[0]["temperature"] == 0.0 and "seed" not in calls[0]
    monkeypatch.setenv("CODEBUD_TEMPERATURE", "0.5")
    monkeypatch.setenv("CODEBUD_SEED", "3")
    llm_stub._call_openai("p")
    assert calls[1]["temperature"] == 0.5 and calls[1]["seed"] == 3


def test_config_keys_map_to_the_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    cfg = settings.user_config_file()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("temperature = 0\nseed = 42\n")
    values = settings._file_values()
    assert values["CODEBUD_TEMPERATURE"][0] == "0"
    assert values["CODEBUD_SEED"][0] == "42"
    cfg.write_text('temperature = "hot"\nseed = 1.5\n')
    assert settings._file_values() == {}
