"""
tests/test_steps.py

The one step loop shared by run, plan --execute and chat (cli/steps.py):
three approval policies, and the first failure stops the run.
"""

import pytest

from codebud.cli.steps import policy_for_mode, run_steps


def _step(n, cmd="ls", tool="command"):
    return {"id": f"step_{n}", "description": f"step {n}", "tool": tool, "args": {"cmd": cmd}}


class _Agent:
    def __init__(self, fail_on=None):
        self.ran = []
        self.fail_on = fail_on

        agent = self

        class Executor:
            def execute_plan(self, plan):
                step = plan["plan"][0]
                agent.ran.append(step["id"])
                if step["id"] == agent.fail_on:
                    return {"status": "step_error", "error": "boom"}
                return {"status": "ok", "results": {step["id"]: {"stdout": "", "returncode": 0}}}

        self.executor = Executor()

    def is_powerful(self, step):
        return step["args"].get("cmd", "").startswith("pytest")

    def confirmation_reason(self, step):
        return "Powerful command." if self.is_powerful(step) else None


@pytest.fixture
def answers(monkeypatch):
    """Queue answers to prompts; an empty queue means end of input."""
    queue, prompts = [], []

    def fake_input(prompt=""):
        prompts.append(prompt)
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)
    return queue, prompts


def test_policy_for_mode():
    assert [policy_for_mode(m) for m in ("ask", "edit", "auto", "full-auto")] == [
        "ask",
        "ask",
        "auto",
        "full-auto",
    ]


def test_full_auto_asks_nothing(answers):
    agent = _Agent()
    outcome = run_steps(agent, [_step(1), _step(2, "pytest")], "full-auto")
    assert agent.ran == ["step_1", "step_2"] and answers[1] == []
    assert (outcome.ran, outcome.changed_files) == (2, True)


def test_auto_asks_only_for_steps_with_a_reason(answers):
    queue, prompts = answers
    queue.append("n")
    agent = _Agent()
    outcome = run_steps(agent, [_step(1), _step(2, "pytest")], "auto")
    assert agent.ran == ["step_1"]
    assert prompts == ["  Run it? [y/N] "]
    assert outcome.unconfirmed == 0


def test_auto_without_anyone_to_answer_counts_unconfirmed(answers, capsys):
    agent = _Agent()
    outcome = run_steps(agent, [_step(1, "pytest")], "auto")
    assert agent.ran == [] and outcome.unconfirmed == 1
    assert "no one to confirm" in capsys.readouterr().err


def test_ask_a_runs_the_rest_without_repeating_the_header(answers, capsys):
    queue, prompts = answers
    queue.append("a")
    agent = _Agent()
    run_steps(agent, [_step(1), _step(2), _step(3, "pytest")], "ask")
    assert agent.ran == ["step_1", "step_2", "step_3"]
    assert len(prompts) == 1
    assert capsys.readouterr().err.count("Step 1") == 1


def test_ask_q_and_end_of_input_abort(answers):
    queue, _prompts = answers
    queue.append("q")
    agent = _Agent()
    run_steps(agent, [_step(1), _step(2)], "ask")
    assert agent.ran == []
    run_steps(agent, [_step(1)], "ask")  # queue empty: end of input
    assert agent.ran == []


def test_first_failure_stops_the_run_even_after_a(answers):
    """Chat's 'a' used to carry on after a failed step."""
    queue, _prompts = answers
    queue.append("a")
    agent = _Agent(fail_on="step_2")
    outcome = run_steps(agent, [_step(1), _step(2), _step(3)], "ask")
    assert agent.ran == ["step_1", "step_2"]
    assert outcome.failed and outcome.ran == 1


def test_unknown_policy_is_an_error():
    with pytest.raises(ValueError):
        run_steps(_Agent(), [], "yolo")


# --- "a(ll)" in ask mode still confirms egress (security review, low) -------


def _search(n, query="python docs"):
    return {
        "id": f"step_{n}",
        "description": "search",
        "tool": "web_search",
        "args": {"query": query},
    }


def test_ask_all_still_confirms_a_later_web_search(answers, monkeypatch):
    monkeypatch.setenv("CODEBUD_PRIVACY", "private")
    queue, prompts = answers
    queue.extend(["a", "n"])  # approve all at step 1; refuse the search
    agent = _Agent()
    run_steps(agent, [_step(1), _search(2), _step(3)], "ask")
    assert agent.ran == ["step_1", "step_3"]
    assert any("Send it?" in p for p in prompts)


def test_ask_all_still_confirms_a_network_step(answers, monkeypatch):
    monkeypatch.setenv("CODEBUD_PRIVACY", "private")
    queue, prompts = answers
    queue.extend(["a", "y"])
    agent = _Agent()
    net = {"id": "step_2", "description": "x", "tool": "command",
           "args": {"cmd": "pip download x", "network": True}}  # fmt: skip
    run_steps(agent, [_step(1), net], "ask")
    assert agent.ran == ["step_1", "step_2"]
    assert sum("Send it?" in p for p in prompts) == 1


def test_ask_all_at_trusted_asks_nothing_more(answers, monkeypatch):
    monkeypatch.setenv("CODEBUD_PRIVACY", "trusted")
    queue, prompts = answers
    queue.append("a")
    agent = _Agent()
    run_steps(agent, [_step(1), _search(2)], "ask")
    assert agent.ran == ["step_1", "step_2"] and len(prompts) == 1


def test_no_answer_to_an_egress_prompt_is_not_a_yes(answers, monkeypatch):
    monkeypatch.setenv("CODEBUD_PRIVACY", "private")
    queue, _ = answers
    queue.append("a")  # then end of input at the egress prompt
    agent = _Agent()
    outcome = run_steps(agent, [_step(1), _search(2)], "ask")
    assert agent.ran == ["step_1"] and outcome.unconfirmed == 1
