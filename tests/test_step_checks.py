"""
tests/test_step_checks.py

The core's hook for extra step checks (phase 10, step 5 plan, PR 4).
The security layer registers its scope check here when an engagement is
active; the core asks every registered check for every step without
knowing what they are, and never imports the layer.
"""

import pytest

from codebud import safety
from codebud.safety import SafetyEngine


@pytest.fixture(autouse=True)
def clean_hooks(monkeypatch):
    monkeypatch.setattr(safety, "_STEP_CHECKS", [])
    monkeypatch.setattr(safety, "_STEP_NOTICES", [])


def _engine():
    engine = SafetyEngine()
    engine.harmless_commands = ["ls"]
    engine.powerful_commands = ["nmap"]
    return engine


def _step(cmd, **args):
    return {"tool": "command", "args": {"cmd": cmd, **args}}


def test_a_registered_check_refuses_steps():
    safety.add_step_check(lambda step: "out of scope" if "10.9" in step["args"]["cmd"] else None)
    engine = _engine()
    assert engine.refusal_reason(_step("nmap 10.9.0.1")) == "out of scope"
    assert engine.refusal_reason(_step("nmap 10.0.0.1")) is None


def test_checks_run_after_the_built_in_rules():
    """A command the allow-list refuses is refused for that reason first."""
    seen = []
    safety.add_step_check(lambda step: seen.append(step) or None)
    reason = _engine().refusal_reason(_step("rm -rf x"))
    assert "allow-list" in reason
    assert seen == []


def test_checks_apply_right_before_running_too():
    safety.add_step_check(lambda step: "window closed")
    allowed, reason = _engine().may_execute(_step("ls"))
    assert not allowed and "window closed" in reason


def test_a_notice_asks_for_confirmation():
    safety.add_step_notice(lambda step: "host is outside the scope")
    assert _engine().confirmation_reason(_step("ls")) == "host is outside the scope"


def test_removing_a_check():
    check = lambda step: "no"  # noqa: E731
    safety.add_step_check(check)
    safety.remove_step_check(check)
    assert _engine().refusal_reason(_step("ls")) is None


def test_a_crashing_check_refuses_rather_than_lets_through():
    def broken(step):
        raise ValueError("bug")

    safety.add_step_check(broken)
    reason = _engine().refusal_reason(_step("ls"))
    assert reason is not None and "check failed" in reason
