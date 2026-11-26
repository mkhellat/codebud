"""
tests/test_no_shell.py

Steps run without a shell (the Sandbox splits with shlex and runs the
program directly), so a plan that uses shell syntax is refused with a
reason instead of passing the operator to the program as text.
"""

from unittest.mock import MagicMock

import pytest

from codebud.planner import LLMPlanner
from codebud.safety import SafetyEngine


def _engine():
    engine = SafetyEngine()
    engine.harmless_commands = [
        "ls",
        "echo",
        "cat",
        "grep",
        "awk",
        "find",
        "cd",
        "cmd",
        "make",
        "sed",
        "wc",
    ]
    engine.powerful_commands = ["timeout"]
    return engine


def _step(cmd):
    return {"tool": "command", "args": {"cmd": cmd}}


@pytest.mark.parametrize(
    "cmd",
    [
        "echo 2+2 | bc",
        "ls > out.txt",
        "ls 2>/dev/null",
        "make && make install",
        "cd x ; ls",
        "echo $(whoami)",
        "echo `id`",
        "cat < in.txt",
        "timeout 5 ls | wc -l",
        "cmd 2>&1",
        "echo a|wc -l",
        "ls>out",
        "cat<in",
    ],
)
def test_shell_syntax_is_refused(cmd):
    reason = _engine().refusal_reason(_step(cmd))
    assert reason is not None
    assert "without a shell" in reason


@pytest.mark.parametrize(
    "cmd",
    [
        "grep 'a|b' file",
        'grep -E "x|y" f',
        "awk '$1>5' f",
        "echo 'a > b'",
        "find . -name '*.py'",
        "ls -la",
        "grep '<div>' f",
        'grep ">" f',
        "sed 's/a/>/' f",
        "echo '|'",
        'echo "a && b"',
    ],
)
def test_ordinary_arguments_are_allowed(cmd):
    assert _engine().refusal_reason(_step(cmd)) is None


def test_the_planner_prompt_says_commands_run_without_a_shell():
    registry = MagicMock()
    registry.describe_tools.return_value = "- command: runs a program"
    safety = MagicMock()
    safety.describe_rules.return_value = "No dangerous commands."
    prompt = LLMPlanner(registry, safety)._build_prompt("x")
    assert "without a shell" in prompt.lower()


def _refusal(cmd):
    from codebud.planner import LLMPlanner

    planner = LLMPlanner(MagicMock(), _engine())
    return planner._safety_refusal({"plan": [_step(cmd)]})


def test_shell_refusal_gets_shell_advice_not_allow_list_advice():
    from codebud.cli.errors import explain

    error = _refusal("ls | wc -l")["error"]
    explanation, fix = explain(error)
    assert "without one" in explanation
    assert "harmless_commands" not in fix
    assert "Rephrase" in fix


def test_allow_list_refusal_keeps_allow_list_advice():
    from codebud.cli.errors import explain

    _, fix = explain("The safety policy refused step 1 (du): du is not on the allow-list")
    assert "harmless_commands.json" in fix


def test_engagement_scope_rule_still_wins():
    from codebud.cli.errors import explain

    explanation, _ = explain("The safety policy refused step 1 (x): engagement scope: no")
    assert "engagement" in explanation


def test_refusal_message_has_no_double_full_stop():
    error = _refusal("ls | wc -l")["error"]
    assert error.endswith("separate steps.")
    assert ".." not in error


def test_command_with_control_characters_is_shown_escaped():
    error = _refusal("find . -printf '%f\n\t' | sort")["error"]
    assert "\n" not in error and "\t" not in error
    assert "'%f\\n\\t'" in error
