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
