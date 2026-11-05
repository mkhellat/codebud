import sys
import types

import pytest

from codebud import llm_stub


def _fake_openai(monkeypatch, text="resp-text"):
    """Install a minimal stand-in for the openai v1 client; record calls."""
    calls = []
    choice = types.SimpleNamespace(message=types.SimpleNamespace(content=text))
    completion = types.SimpleNamespace(choices=[choice])

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return completion

    class FakeClient:
        chat = types.SimpleNamespace(completions=FakeCompletions())

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = lambda: FakeClient()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    return calls


def test_no_model_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    with pytest.raises(llm_stub.LLMUnavailable, match="OLLAMA_MODEL is not set"):
        llm_stub.call_llm("hello")


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


@pytest.mark.parametrize("ollama_model", [None, "some-model"])
def test_openai_is_never_a_fallback(monkeypatch, ollama_model):
    """With OPENAI_API_KEY set, prompts used to go to OpenAI whenever Ollama
    was unset or down -- silently (phase 8, A1)."""
    if ollama_model:
        monkeypatch.setenv("OLLAMA_MODEL", ollama_model)
    else:
        monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "testkey")
    monkeypatch.setattr(llm_stub, "_ollama_available", lambda: False)
    calls = _fake_openai(monkeypatch)
    with pytest.raises(llm_stub.LLMUnavailable, match="CODEBUD_BACKEND=openai"):
        llm_stub.call_llm("secret prompt")
    assert calls == []


def test_unreachable_ollama_names_the_url(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "some-model")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(llm_stub, "_ollama_available", lambda: False)
    with pytest.raises(llm_stub.LLMUnavailable, match="not reachable at http://127.0.0.1:9"):
        llm_stub.call_llm("hello")


def test_openai_when_chosen(monkeypatch):
    monkeypatch.setenv("CODEBUD_BACKEND", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "testkey")
    monkeypatch.setenv("OLLAMA_MODEL", "local-model")
    monkeypatch.setattr(llm_stub, "_ollama_available", lambda: True)
    monkeypatch.setattr(llm_stub, "_call_ollama", lambda *a, **k: pytest.fail("used Ollama"))
    calls = _fake_openai(monkeypatch)
    assert llm_stub.call_llm("prompt for openai") == "resp-text"
    assert calls[0]["model"] == "gpt-3.5-turbo"


def test_openai_backend_needs_a_key(monkeypatch):
    monkeypatch.setenv("CODEBUD_BACKEND", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(llm_stub.LLMUnavailable, match="OPENAI_API_KEY is not set"):
        llm_stub.call_llm("hello")


def test_openai_backend_without_the_package_says_how_to_install(monkeypatch):
    """openai is an optional extra (codebud[openai]); say so if it's missing."""
    monkeypatch.setenv("CODEBUD_BACKEND", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "testkey")
    monkeypatch.setitem(sys.modules, "openai", None)  # import openai -> ImportError
    with pytest.raises(llm_stub.LLMUnavailable, match=r"pip install 'codebud\[openai\]'"):
        llm_stub.call_llm("hello")


def test_unknown_backend_is_an_error(monkeypatch):
    monkeypatch.setenv("CODEBUD_BACKEND", "anthropic")
    with pytest.raises(llm_stub.LLMUnavailable, match="ollama or openai"):
        llm_stub.call_llm("hello")


def test_backend_description(monkeypatch):
    monkeypatch.delenv("CODEBUD_BACKEND", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    assert llm_stub.backend_description() == "ollama at http://localhost:11434 (local)"
    monkeypatch.setenv("CODEBUD_BACKEND", "openai")
    assert "OpenAI" in llm_stub.backend_description()
    assert "leave this machine" in llm_stub.backend_description()


def test_list_models_returns_names(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"name": "a"}, {"name": "b"}]}

    monkeypatch.setattr(llm_stub.requests, "get", lambda *a, **kw: FakeResponse())
    assert llm_stub.list_models() == ["a", "b"]


def test_model_digests_maps_names_to_digests(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"name": "a:latest", "digest": "d1"}, {"name": "b"}]}

    monkeypatch.setattr(llm_stub.requests, "get", lambda *a, **kw: FakeResponse())
    assert llm_stub.model_digests() == {"a:latest": "d1"}


def test_model_digests_none_when_ollama_is_unreachable(monkeypatch):
    def boom(*a, **kw):
        raise ConnectionError("no server")

    monkeypatch.setattr(llm_stub.requests, "get", boom)
    assert llm_stub.model_digests() is None


def test_list_models_empty_on_failure(monkeypatch):
    def boom(*a, **kw):
        raise ConnectionError("no server")

    monkeypatch.setattr(llm_stub.requests, "get", boom)
    assert llm_stub.list_models() == []


def test_warm_model_sync_success(monkeypatch):
    monkeypatch.setattr(llm_stub, "_ollama_available", lambda: True)

    class FakeResponse:
        def raise_for_status(self):
            pass

        def iter_lines(self):
            import json as _json

            yield _json.dumps({"response": "h", "done": False}).encode()
            yield _json.dumps({"response": "i", "done": True}).encode()

    monkeypatch.setattr(llm_stub.requests, "post", lambda *a, **kw: FakeResponse())

    chunks = []
    ok = llm_stub.warm_model_sync("some-model", on_chunk=chunks.append)
    assert ok is True
    assert chunks == ["h", "i"]


def test_warm_model_sync_unreachable_server(monkeypatch):
    monkeypatch.setattr(llm_stub, "_ollama_available", lambda: False)
    assert llm_stub.warm_model_sync("some-model") is False


def test_warm_model_sync_request_failure(monkeypatch):
    monkeypatch.setattr(llm_stub, "_ollama_available", lambda: True)

    def boom(*a, **kw):
        raise ConnectionError("dropped")

    monkeypatch.setattr(llm_stub.requests, "post", boom)
    assert llm_stub.warm_model_sync("some-model") is False


def test_summarize_without_a_backend_returns_nothing(monkeypatch):
    def unavailable(prompt, timeout=600.0, on_chunk=None):
        raise llm_stub.LLMUnavailable("Ollama is not reachable")

    monkeypatch.setattr(llm_stub, "call_llm", unavailable)
    assert llm_stub.summarize_conversation([{"role": "user", "content": "x"}]) == ""


def test_summarize_conversation_sends_turns_and_strips_reply(monkeypatch):
    captured = {}

    def fake_call_llm(prompt, timeout=600.0, on_chunk=None):
        captured["prompt"] = prompt
        return "  A short summary.  \n"

    monkeypatch.setattr(llm_stub, "call_llm", fake_call_llm)
    turns = [
        {"role": "user", "content": "list the python files"},
        {"role": "assistant", "content": "planned: command(list python files)"},
    ]
    assert llm_stub.summarize_conversation(turns) == "A short summary."
    assert "User: list the python files" in captured["prompt"]
    assert "Assistant: planned: command(list python files)" in captured["prompt"]
