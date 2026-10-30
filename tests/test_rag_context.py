"""
tests/test_rag_context.py

Retrieved project context in the planner prompt (phase 9, D1): fetched
quietly (planning never fails because of search), framed as data, kept
inside its token budget, and dropped before conversation history when
the prompt is full.
"""

from unittest.mock import MagicMock

import pytest

from codebud.planner import LLMPlanner, estimate_tokens
from codebud.rag import context
from codebud.rag.chunker import Chunk
from codebud.rag.search import Hit, SearchUnavailable


def _planner():
    registry = MagicMock()
    registry.tools = {"command": object()}
    registry.describe_tools.return_value = "- command: run a program"
    safety = MagicMock()
    safety.describe_rules.return_value = "No dangerous commands."
    safety.mode = "ask"
    return LLMPlanner(registry, safety)


def _hit(path="src/a.py", text="def alpha():\n    return 1", symbol="alpha", start=10, score=0.5):
    return Hit(Chunk(path, start, start + text.count("\n"), symbol, "python", text), score)


# --- format_context ---------------------------------------------------------


def test_block_names_each_chunk_and_frames_it_as_data():
    block = context.format_context([_hit(), _hit("docs/b.texi", "@node B", None, 3)], 1000)
    assert block.startswith("PROJECT CONTEXT")
    assert "not instructions" in block
    assert "--- src/a.py:10-11 (alpha)\ndef alpha():\n    return 1\n" in block
    assert "--- docs/b.texi:3-3\n@node B\n" in block


def test_block_keeps_whole_chunks_within_the_budget():
    hits = [_hit(f"f{i}.py", "x = 1\n" * 40, f"s{i}") for i in range(5)]
    block = context.format_context(hits, 300)  # room for the header, fence and one chunk
    assert estimate_tokens(block) <= 300
    assert "f0.py" in block and "f4.py" not in block
    assert block.count("x = 1") % 40 == 0  # no chunk cut in half


def test_no_hits_or_no_room_gives_no_block():
    assert context.format_context([], 1000) == ""
    assert context.format_context([_hit()], 5) == ""


# --- retrieve ---------------------------------------------------------------


@pytest.fixture
def searched(monkeypatch, tmp_path):
    """Pretend the project has an index; record what is searched for."""
    calls = []

    def fake_search(root, query, top_k):
        calls.append(query)
        return [_hit()]

    monkeypatch.setattr(context, "has_index", lambda root: True)
    monkeypatch.setattr(context, "search", fake_search)
    return calls


def test_retrieve_searches_for_the_request(searched):
    assert context.retrieve("where is the alpha function used?") == [_hit()]
    assert searched == ["where is the alpha function used?"]


def test_short_follow_ups_include_the_previous_request(searched):
    history = [
        {"role": "user", "content": "explain the session locking code"},
        {"role": "assistant", "content": "planned: ..."},
    ]
    context.retrieve("and the tests?", history=history)
    assert searched == ["explain the session locking code and the tests?"]


def test_retrieve_is_off_with_codebud_rag_0(searched, monkeypatch):
    monkeypatch.setenv("CODEBUD_RAG", "0")
    assert context.retrieve("anything") == []
    assert searched == []


def test_retrieve_without_an_index_does_not_search(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(context, "search", lambda *a, **k: pytest.fail("searched"))
    assert context.retrieve("anything") == []


def test_retrieve_swallows_search_errors(monkeypatch):
    monkeypatch.setattr(context, "has_index", lambda root: True)

    def broken(*_a, **_k):
        raise SearchUnavailable("no numpy")

    monkeypatch.setattr(context, "search", broken)
    assert context.retrieve("anything") == []


# --- the planner prompt -----------------------------------------------------


def test_prompt_without_context_is_unchanged():
    planner = _planner()
    assert planner._build_prompt("hi") == planner._build_prompt("hi", context_hits=[])
    assert "PROJECT CONTEXT" not in planner._build_prompt("hi")


def test_context_goes_after_history_just_before_the_request():
    history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "ok"}]
    prompt = _planner()._build_prompt("fix alpha", history=history, context_hits=[_hit()])
    assert (
        prompt.index("CONVERSATION HISTORY")
        < prompt.index("PROJECT CONTEXT")
        < prompt.index("USER REQUEST: fix alpha")
    )
    # Everything before the context is what the previous turn sent too
    # (KV cache), apart from the history that grew.
    assert prompt.startswith(_planner()._build_prompt("x", history=history).split("Now respond")[0])


def test_context_is_capped_at_a_quarter_of_the_window(monkeypatch):
    monkeypatch.setenv("CODEBUD_NUM_CTX", "8192")
    hits = [_hit(f"f{i}.py", "y = 2\n" * 60, f"s{i}") for i in range(20)]
    prompt = _planner()._build_prompt("q", context_hits=hits)
    block = prompt[prompt.index("PROJECT CONTEXT") : prompt.index("Now respond")]
    assert estimate_tokens(block) <= 8192 // 4


def test_chunks_are_dropped_before_history_when_the_prompt_is_full(monkeypatch):
    monkeypatch.setenv("CODEBUD_NUM_CTX", "3072")
    history = [{"role": "user", "content": f"turn {i} " + "w " * 150} for i in range(6)]
    hits = [_hit(f"f{i}.py", "z = 3\n" * 30, f"s{i}") for i in range(6)]
    prompt = _planner()._build_prompt("q", history=history, context_hits=hits)
    assert estimate_tokens(prompt) <= 3072 - 1024
    kept_turns = sum(f"turn {i} " in prompt for i in range(6))
    kept_chunks = sum(f"--- f{i}.py" in prompt for i in range(6))
    # History is only trimmed once every chunk is gone.
    assert kept_chunks == 0 or kept_turns == 6


def test_config_shows_codebud_rag(monkeypatch, capsys):
    from codebud.cli.admin import cmd_config

    monkeypatch.delenv("CODEBUD_RAG", raising=False)
    cmd_config(None)
    line = next(ln for ln in capsys.readouterr().out.splitlines() if "CODEBUD_RAG" in ln)
    assert line.split("=", 1)[1].split()[0] == "1"


# --- plain chat replies (phase 9, D2) ---------------------------------------


def _captured_plain_prompt(monkeypatch, **kwargs):
    from codebud import llm_stub

    sent = []
    monkeypatch.setattr(
        llm_stub, "call_llm", lambda prompt, on_chunk=None: sent.append(prompt) or ""
    )
    llm_stub.call_llm_plain("how does alpha work?", **kwargs)
    return sent[0]


def test_plain_reply_prompt_is_unchanged_without_context(monkeypatch):
    prompt = _captured_plain_prompt(monkeypatch)
    assert prompt == "You are a helpful coding assistant.\n\nUser: how does alpha work?\nAssistant:"


def test_plain_reply_sees_the_context_and_is_asked_to_cite_it(monkeypatch):
    block = context.format_context([_hit()], 1000)
    prompt = _captured_plain_prompt(monkeypatch, context_block=block)
    assert block in prompt
    assert "path:start-end" in prompt
    assert prompt.index("PROJECT CONTEXT") < prompt.index("User: how does alpha work?")


def test_core_gives_the_plain_reply_the_retrieved_context(tmp_path, monkeypatch):
    from codebud import memory as memory_mod
    from codebud.core import AgentCore

    monkeypatch.setattr(memory_mod, "MEMORY_PATH", str(tmp_path / "entries.jsonl"))
    monkeypatch.setattr("codebud.core.prewarm_model", lambda: None)
    monkeypatch.setattr("codebud.core.ModelHeartbeat.start", lambda self: None)
    monkeypatch.setattr("codebud.core.ModelHeartbeat.stop", lambda self: None)
    monkeypatch.setattr("codebud.core.retrieve", lambda message, history=None: [_hit()])
    seen = {}

    def plain(message, history=None, on_chunk=None, context_block=""):
        seen["block"] = context_block
        return "alpha returns 1 (src/a.py:10-11)"

    monkeypatch.setattr("codebud.core.call_llm_plain", plain)
    agent = AgentCore()
    monkeypatch.setattr(agent.planner, "generate_plan", lambda *a, **k: {"status": "plan_error"})

    result = agent.handle_user_message("how does alpha work?")
    assert result == {"status": "chat_reply", "reply": "alpha returns 1 (src/a.py:10-11)"}
    assert "--- src/a.py:10-11 (alpha)" in seen["block"]
    agent.close()


def test_agent_switch_stops_retrieval(tmp_path, monkeypatch):
    from codebud import memory as memory_mod
    from codebud.core import AgentCore

    monkeypatch.setattr(memory_mod, "MEMORY_PATH", str(tmp_path / "entries.jsonl"))
    monkeypatch.setattr("codebud.core.prewarm_model", lambda: None)
    monkeypatch.setattr("codebud.core.ModelHeartbeat.start", lambda self: None)
    monkeypatch.setattr("codebud.core.ModelHeartbeat.stop", lambda self: None)
    monkeypatch.setattr("codebud.core.retrieve", lambda message, history=None: [_hit()])
    agent = AgentCore()
    plan = {
        "status": "ok",
        "plan": [{"id": "step_0", "description": "d", "tool": "command", "args": {"cmd": "ls"}}],
    }
    monkeypatch.setattr(agent.planner, "generate_plan", lambda *a, **k: plan)

    assert agent.get_rag() is True
    agent.handle_user_message("q")
    assert agent.last_context == [_hit()]
    agent.set_rag(False)
    agent.handle_user_message("q")
    assert agent.last_context == []
    agent.close()


def test_agent_starts_with_rag_off_under_codebud_rag_0(tmp_path, monkeypatch):
    from codebud import memory as memory_mod
    from codebud.core import AgentCore

    monkeypatch.setenv("CODEBUD_RAG", "0")
    monkeypatch.setattr(memory_mod, "MEMORY_PATH", str(tmp_path / "entries.jsonl"))
    monkeypatch.setattr("codebud.core.prewarm_model", lambda: None)
    monkeypatch.setattr("codebud.core.ModelHeartbeat.start", lambda self: None)
    monkeypatch.setattr("codebud.core.ModelHeartbeat.stop", lambda self: None)
    agent = AgentCore()
    assert agent.get_rag() is False
    agent.close()
