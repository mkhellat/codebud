"""
tests/test_reply_or_plan.py

The planner may answer directly ("reply") when a request needs no step;
a plan made only of echo/printf steps, or refused only for shell syntax,
goes back to the model once with the reason.
"""

import json
import sys
from unittest.mock import patch

import pytest

from codebud import planner as planner_mod
from codebud.planner import LLMPlanner
from codebud.safety import SafetyEngine
from codebud.tools.tool_registry import ToolRegistry


def _cmd(cmd, n=0):
    return {"id": f"step_{n}", "description": "d", "tool": "command", "args": {"cmd": cmd}}


def _plan(*cmds):
    return {"status": "ok", "plan": [_cmd(c, i) for i, c in enumerate(cmds)]}


def _reply(text):
    return {"status": "reply", "reply": text}


@pytest.fixture
def planner():
    safety = SafetyEngine.__new__(SafetyEngine)
    safety.mode = "ask"
    safety.harmless_commands = ["ls", "echo", "grep", "wc"]
    safety.powerful_commands = []
    safety.trusted_sequences = []
    safety.refused = {}
    return LLMPlanner(ToolRegistry(sandbox=None), safety)


@pytest.fixture
def answers(monkeypatch):
    """Queue model answers (dicts, dumped as JSON); record the prompts."""
    state = {"queue": [], "prompts": []}

    def fake_call(prompt, on_chunk=None):
        state["prompts"].append(prompt)
        return json.dumps(state["queue"].pop(0))

    monkeypatch.setattr(planner_mod, "call_llm", fake_call)
    return state


# -- replies ------------------------------------------------------------------


def test_reply_is_returned_as_reply(planner, answers):
    answers["queue"] = [_reply("  Run du -sh DIR.  ")]
    assert planner.generate_plan("how big is a dir?") == {
        "status": "reply",
        "reply": "Run du -sh DIR.",
    }
    assert len(answers["prompts"]) == 1


@pytest.mark.parametrize("bad", ["", "   ", None, 5])
def test_empty_or_invalid_reply_takes_the_json_retry_path(planner, answers, bad):
    answers["queue"] = [{"status": "reply", "reply": bad}, _reply("hello")]
    assert planner.generate_plan("hi") == {"status": "reply", "reply": "hello"}
    assert len(answers["prompts"]) == 2
    assert '"status":"reply"' in answers["prompts"][1]  # the retry prompt knows the form


def test_empty_reply_twice_is_a_plan_error(planner, answers):
    answers["queue"] = [_reply(""), _reply("")]
    assert planner.generate_plan("hi")["status"] == "plan_error"


# -- echo-only plans ----------------------------------------------------------


def test_echo_only_plan_is_retried_with_the_note(planner, answers):
    answers["queue"] = [_plan("echo 'it is 4'"), _reply("2+2 is 4.")]
    result = planner.generate_plan("what is 2+2?")
    assert result == {"status": "reply", "reply": "2+2 is 4."}
    assert len(answers["prompts"]) == 2
    assert "YOUR PREVIOUS PLAN WAS INVALID" not in answers["prompts"][0]
    assert "echo or printf" in answers["prompts"][1]
    assert "reply" in answers["prompts"][1].split("INVALID")[-1]


def test_echo_through_a_wrapper_and_printf_count(planner, answers):
    answers["queue"] = [_plan("sudo echo hi", "printf 'x'"), _reply("ok")]
    assert planner.generate_plan("x")["status"] == "reply"


def test_echo_only_twice_is_plan_invalid(planner, answers):
    answers["queue"] = [_plan("echo a"), _plan("echo b")]
    result = planner.generate_plan("x")
    assert result["status"] == "plan_invalid"
    assert "problems" not in result
    assert len(answers["prompts"]) == 2


def test_echo_mixed_with_other_steps_stays_a_plan(planner, answers):
    answers["queue"] = [_plan("echo start", "ls")]
    result = planner.generate_plan("x")
    assert result["status"] == "ok"
    assert len(answers["prompts"]) == 1


# -- shell syntax -------------------------------------------------------------


def test_pipe_plan_is_retried_with_the_shell_note(planner, answers):
    answers["queue"] = [_plan("ls | wc -l"), _plan("ls")]
    result = planner.generate_plan("count")
    assert result["status"] == "ok"
    assert len(answers["prompts"]) == 2
    note = answers["prompts"][1].split("YOUR PREVIOUS PLAN WAS INVALID")[-1]
    assert "step 1:" in note and "`|`" in note and "without a shell, or reply" in note


def test_pipe_twice_is_refused_with_the_shell_reason(planner, answers):
    answers["queue"] = [_plan("ls | wc -l"), _plan("ls | wc")]
    result = planner.generate_plan("count")
    assert result["status"] == "plan_refused"
    assert "`|`" in result["error"]
    assert len(answers["prompts"]) == 2


def test_pipe_then_reply_is_a_reply(planner, answers):
    answers["queue"] = [_plan("ls | wc -l"), _reply("Use ls -1.")]
    assert planner.generate_plan("count") == {"status": "reply", "reply": "Use ls -1."}


def test_allow_list_refusal_is_not_retried(planner, answers):
    answers["queue"] = [_plan("curl example.org")]
    result = planner.generate_plan("fetch")
    assert result["status"] == "plan_refused"
    assert "allow-list" in result["error"]
    assert len(answers["prompts"]) == 1


def test_at_most_one_corrective_retry(planner, answers):
    # echo-only, then a pipe: the second answer is refused, not retried again
    answers["queue"] = [_plan("echo a"), _plan("ls | wc"), _plan("ls")]
    assert planner.generate_plan("x")["status"] == "plan_refused"
    assert len(answers["prompts"]) == 2


def test_missing_argument_then_pipe_is_refused_after_one_retry(planner, answers):
    bad = {
        "status": "ok",
        "plan": [{"id": "step_0", "description": "d", "tool": "file_read", "args": {}}],
    }
    answers["queue"] = [bad, _plan("ls | wc"), _plan("ls")]
    assert planner.generate_plan("x")["status"] == "plan_refused"
    assert len(answers["prompts"]) == 2


def test_json_retry_then_pipe_is_refused_without_a_third_call(planner, monkeypatch):
    calls = []
    outputs = ["not json", json.dumps(_plan("ls | wc")), json.dumps(_plan("ls"))]

    def fake_call(prompt, on_chunk=None):
        calls.append(prompt)
        return outputs.pop(0)

    monkeypatch.setattr(planner_mod, "call_llm", fake_call)
    assert planner.generate_plan("x")["status"] == "plan_refused"
    assert len(calls) == 2


# -- the prompt ---------------------------------------------------------------


def test_prompt_has_identity_reply_form_and_no_echo_rule(planner):
    prompt = planner._build_prompt("hi")
    assert prompt.startswith("You are Codebud, a local coding agent")
    assert '{"status": "reply", "reply": "<the answer>"}' in prompt
    assert "Never use echo or printf steps to answer or explain: reply instead." in prompt
    assert '"status": "reply", "reply": "Run du -sh' in prompt  # the few-shot reply


def test_prompt_has_no_codebud_source_paths(planner):
    assert "src/codebud" not in planner._build_prompt("hi")
    assert "src/codebud" not in planner._build_retry_prompt("hi")


# -- through AgentCore and the CLI --------------------------------------------


@pytest.fixture
def agent(tmp_path, monkeypatch, answers):
    from codebud import memory as memory_mod
    from codebud.core import AgentCore

    monkeypatch.setattr(memory_mod, "MEMORY_PATH", str(tmp_path / "entries.jsonl"))
    monkeypatch.setattr("codebud.core.prewarm_model", lambda: None)
    monkeypatch.setattr("codebud.core.ModelHeartbeat.start", lambda self: None)
    monkeypatch.setattr("codebud.core.ModelHeartbeat.stop", lambda self: None)
    core = AgentCore()
    yield core
    core.close()


def test_core_turns_a_reply_into_chat_reply_and_records_it(agent, answers):
    answers["queue"] = [_reply("Hello, I am Codebud.")]
    out = agent.handle_user_message("hi")
    assert out == {"status": "chat_reply", "reply": "Hello, I am Codebud."}
    assert agent.get_history() == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Hello, I am Codebud."},
    ]


def test_run_prints_a_direct_reply(agent, answers, monkeypatch, capsys):
    from codebud.cli import main as run_agent

    answers["queue"] = [_reply("2+2 is 4.")]
    monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)
    monkeypatch.setattr("codebud.cli.common.make_agent", lambda: agent)
    with patch.object(sys, "argv", ["codebud", "run", "what is 2+2?"]):
        try:
            run_agent.main()
        except SystemExit as exc:
            assert not exc.code
    assert "2+2 is 4." in capsys.readouterr().out
