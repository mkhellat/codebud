"""
tests/test_safety_policy.py

Command policy tightening (2026-10-25). Commands run without a shell
(shlex.split + subprocess), so the policy is judged on the parsed words:
a list entry must match whole leading words, never a string prefix.
"""

import pytest

from codebud.safety import SafetyEngine


def _engine(harmless=None, powerful=None):
    engine = SafetyEngine.__new__(SafetyEngine)
    engine.harmless_commands = (
        harmless if harmless is not None else ["cat", "ls", "echo", "git log"]
    )
    engine.powerful_commands = powerful if powerful is not None else ["cp"]
    engine.trusted_sequences = []
    return engine


def _ok(engine, cmd):
    return engine.validate_step({"tool": "command", "args": {"cmd": cmd}})


@pytest.mark.parametrize(
    "cmd",
    ["cat README.md", "ls", "ls -la src", "git log -n 3", "git log", "cp a b"],
)
def test_whole_command_words_are_allowed(cmd):
    assert _ok(_engine(), cmd) is True


@pytest.mark.parametrize(
    "cmd",
    [
        "catastrophe",  # was allowed: starts with "cat"
        "lsblk",  # was allowed: starts with "ls"
        "git logfoo",  # was allowed: starts with "git log"
        "git push",
        "git",
        "cpio -o",  # was allowed: starts with "cp"
        "/bin/ls",
        "echo 'unterminated",  # cannot be parsed
    ],
)
def test_prefix_lookalikes_and_unparsable_commands_are_rejected(cmd):
    assert _ok(_engine(), cmd) is False


def _dev_engine():
    return _engine(
        harmless=["find", "python", "python3", "git log", "git diff", "git show", "ls"],
        powerful=[],
    )


@pytest.mark.parametrize(
    "cmd",
    [
        "find . -name '*.pyc' -delete",
        "find . -exec rm {} ;",
        "find . -execdir sh -c 'x' ;",
        "find . -ok rm {} ;",
        "find . -okdir rm {} ;",
        "find . -fprint out.txt",
        "find . -fls listing.txt",
        "python -c 'import os'",
        "python3 -c print(1)",
        "python3 -cprint(1)",
        "python3 -Ic 'import os'",
        "python3 -W ignore -c 'import os'",
        "git log --output=/tmp/x",
        "git diff --output /tmp/x",
        "git show --ext-diff HEAD",
    ],
)
def test_arguments_that_run_code_or_destroy_are_refused(cmd):
    assert _ok(_dev_engine(), cmd) is False


@pytest.mark.parametrize(
    "cmd",
    [
        "find . -name '*.py'",
        "find src -type f -newer setup.py",
        "python script.py",
        "python3 -m pytest -q",
        "python3 script.py -c config.toml",  # -c belongs to the script, not python
        "python3 -W ignore script.py",
        "git log -n 3 --oneline",
        "git diff HEAD~1",
    ],
)
def test_ordinary_uses_stay_allowed(cmd):
    assert _ok(_dev_engine(), cmd) is True


# --- powerful commands (need an explicit y in auto mode) ---


def test_is_powerful_for_listed_commands_only():
    engine = _engine(harmless=["ls"], powerful=["pytest", "cp"])
    assert engine.is_powerful({"tool": "command", "args": {"cmd": "pytest -q"}}) is True
    assert engine.is_powerful({"tool": "command", "args": {"cmd": "cp a b"}}) is True
    assert engine.is_powerful({"tool": "command", "args": {"cmd": "ls -la"}}) is False
    assert engine.is_powerful({"tool": "file_write", "args": {"path": "a"}}) is False


def test_default_policy_treats_code_runners_as_powerful():
    engine = SafetyEngine()
    for cmd in ["python script.py", "python3 -m pytest", "pytest -q", "ruff check --fix ."]:
        step = {"tool": "command", "args": {"cmd": cmd}}
        assert engine.validate_step(step) is True, cmd
        assert engine.is_powerful(step) is True, cmd
    for cmd in ["ls -la", "cat README.md", "git status"]:
        assert engine.is_powerful({"tool": "command", "args": {"cmd": cmd}}) is False, cmd


def test_malformed_policy_file_is_reported(tmp_path, monkeypatch, caplog):
    """A broken JSON file silently became an empty list; now it warns."""
    (tmp_path / ".codebud").mkdir()
    (tmp_path / ".codebud" / "harmless_commands.json").write_text("[not json")
    monkeypatch.chdir(tmp_path)
    caplog.set_level("WARNING")

    engine = SafetyEngine()

    assert engine.harmless_commands == []
    assert "harmless_commands.json" in caplog.text


# --- why a step is refused (open product issue 1) ----------------------------


def _reason(engine, cmd):
    return engine.refusal_reason({"tool": "command", "args": {"cmd": cmd}})


@pytest.mark.parametrize(
    "cmd, words",
    [
        ("rm -rf build", ["rm", "allow-list"]),
        ("find . -delete", ["find", "-delete", "never allowed"]),
        ("python3 -c 'print(1)'", ["python3", "-c", "never allowed"]),
        ("git log --output=x", ["git", "--output=x", "never allowed"]),
        ('echo "unterminated', ["could not be parsed"]),
        ("", ["empty"]),
    ],
)
def test_refusal_reason_says_what_is_wrong(cmd, words):
    engine = _engine(harmless=["ls", "echo", "git log", "find"], powerful=["python3"])
    reason = _reason(engine, cmd)
    for word in words:
        assert word in reason


def test_allowed_steps_have_no_refusal_reason():
    engine = _engine()
    assert _reason(engine, "ls -la") is None
    assert engine.refusal_reason({"tool": "file_read", "args": {"path": "x"}}) is None


def test_refusal_reason_names_the_mode():
    engine = _engine()
    engine.mode = "edit"
    assert "edit mode" in _reason(engine, "ls")


@pytest.mark.parametrize("cmd", ["ls", "rm -rf x", "find . -delete", 'echo "x', ""])
def test_validate_step_agrees_with_refusal_reason(cmd):
    engine = _engine(harmless=["ls", "echo", "find"])
    assert _ok(engine, cmd) is (_reason(engine, cmd) is None)


def test_may_execute_gives_the_reason():
    allowed, message = _engine().may_execute({"tool": "command", "args": {"cmd": "rm -rf x"}})
    assert allowed is False
    assert "rm" in message and "allow-list" in message


def test_planner_reports_a_refused_step_instead_of_chatting(monkeypatch):
    import json
    from unittest.mock import MagicMock

    from codebud import planner as planner_mod

    registry = MagicMock()
    registry.has.return_value = True
    registry.describe_tools.return_value = "- command: run a program"
    plan = {
        "status": "ok",
        "plan": [
            {"id": "step_0", "description": "list", "tool": "command", "args": {"cmd": "ls"}},
            {
                "id": "step_1",
                "description": "clean",
                "tool": "command",
                "args": {"cmd": "rm -rf build"},
            },
        ],
    }
    monkeypatch.setattr(planner_mod, "call_llm", lambda prompt, on_chunk=None: json.dumps(plan))
    result = planner_mod.LLMPlanner(registry, _engine()).generate_plan("clean up")
    assert result["status"] == "plan_refused"
    assert "step 2" in result["error"]
    assert "rm -rf build" in result["error"]
    assert "allow-list" in result["error"]


def test_plan_with_non_object_args_is_malformed(monkeypatch):
    import json
    from unittest.mock import MagicMock

    from codebud import planner as planner_mod

    registry = MagicMock()
    registry.has.return_value = True
    plan = {
        "status": "ok",
        "plan": [{"id": "s", "description": "d", "tool": "command", "args": "rm -rf /"}],
    }
    monkeypatch.setattr(planner_mod, "call_llm", lambda prompt, on_chunk=None: json.dumps(plan))
    result = planner_mod.LLMPlanner(registry, _engine())._attempt("prompt")
    assert result == {"status": "plan_error", "error": "LLM returned malformed plan"}


def test_confirmation_reasons():
    from codebud.safety import POWERFUL_REASON

    engine = _engine(harmless=["ls"], powerful=["pytest"])
    search = {"tool": "web_search", "args": {"query": "codebud releases"}}
    assert engine.confirmation_reason(search) == (
        'Web search: sends "codebud releases" to DuckDuckGo.'
    )
    assert engine.confirmation_reason({"tool": "command", "args": {"cmd": "pytest"}}) == (
        POWERFUL_REASON
    )
    assert engine.confirmation_reason({"tool": "command", "args": {"cmd": "ls"}}) is None
    assert engine.confirmation_reason({"tool": "file_read", "args": {"path": "a"}}) is None


@pytest.mark.parametrize(
    "cmd",
    [
        "timeout 5 python3 -c 'print(1)'",
        "env X=1 python3 -c 'print(1)'",
        "nice find . -exec rm {} ;",
        "/usr/bin/find . -exec id ;",
        "/usr/bin/python3 -c 'print(1)'",
    ],
)
def test_refused_arguments_hold_behind_wrappers_and_full_paths(cmd):
    """They used to judge only the first word: an allowed wrapper (timeout,
    env, nice) or a program given by its full path got past find -exec and
    python -c."""
    from codebud.safety import SafetyEngine

    engine = SafetyEngine()
    engine.harmless_commands = []
    engine.powerful_commands = ["timeout", "env", "nice", "/usr/bin/find", "/usr/bin/python3"]
    reason = engine.refusal_reason({"tool": "command", "args": {"cmd": cmd}})
    assert reason is not None and "is never allowed" in reason, cmd
