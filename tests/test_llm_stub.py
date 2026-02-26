import os
import sys
import importlib

import pytest

from agent import llm_stub


def test_no_backend(monkeypatch, caplog):
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    caplog.set_level("WARNING")
    output = llm_stub.call_llm("hello")
    assert output == ""
    assert "No LLM backend configured" in caplog.text


def test_ollama_path(monkeypatch):
    # simulate ollama available and returning a fixed string
    monkeypatch.setenv("OLLAMA_MODEL", "foo-model")
    monkeypatch.setattr(llm_stub, "_ollama_available", lambda: True)
    called = {}

    def fake_call(model, prompt, timeout):
        called['model'] = model
        called['prompt'] = prompt
        return "ollama-output"

    monkeypatch.setattr(llm_stub, "_call_ollama", fake_call)

    result = llm_stub.call_llm("my prompt")
    assert result == "ollama-output"
    assert called['model'] == "foo-model"
    assert called['prompt'] == "my prompt"


def test_openai_fallback(monkeypatch):
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "testkey")

    class DummyResp:
        class Choice:
            def __init__(self):
                self.message = type("o", (), {"content": "resp-text"})

        def __init__(self):
            self.choices = [DummyResp.Choice()]

    class DummyOpenAI:
        ChatCompletion = type("CC", (), {"create": staticmethod(lambda **kwargs: DummyResp())})

    monkeypatch.setitem(sys.modules, "openai", DummyOpenAI)

    result = llm_stub.call_llm("prompt for openai")
    assert result == "resp-text"
