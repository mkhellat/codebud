import os
import sys
import types

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
    monkeypatch.setenv("OLLAMA_MODEL", "foo-model")
    monkeypatch.setattr(llm_stub, "_ollama_available", lambda: True)
    called = {}

    def fake_call(model, prompt, timeout, on_chunk=None):
        called["model"] = model
        called["prompt"] = prompt
        return "ollama-output"

    monkeypatch.setattr(llm_stub, "_call_ollama", fake_call)

    result = llm_stub.call_llm("my prompt")
    assert result == "ollama-output"
    assert called["model"] == "foo-model"
    assert called["prompt"] == "my prompt"


def test_openai_fallback(monkeypatch):
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "testkey")

    # Build a minimal mock that matches the openai v1 client interface.
    choice = types.SimpleNamespace(message=types.SimpleNamespace(content="resp-text"))
    completion = types.SimpleNamespace(choices=[choice])

    class FakeCompletions:
        def create(self, **kwargs):
            return completion

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = lambda: FakeClient()

    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    result = llm_stub.call_llm("prompt for openai")
    assert result == "resp-text"


def test_ollama_unavailable_falls_through(monkeypatch, caplog):
    """If OLLAMA_MODEL is set but server is down, fall through to warning."""
    monkeypatch.setenv("OLLAMA_MODEL", "some-model")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(llm_stub, "_ollama_available", lambda: False)

    caplog.set_level("WARNING")
    result = llm_stub.call_llm("hello")
    assert result == ""
    assert "No LLM backend configured" in caplog.text
