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


# --- a profile that needs an engagement (step 5 plan, PR 5) ------------------------


@pytest.fixture
def engagement_profile(monkeypatch):
    from codebud import profiles

    monkeypatch.setattr(profiles, "_active", profiles.Profile("pentest", engagement=True))


def test_an_engagement_profile_refuses_network_tools_without_one(engagement_profile):
    """Without the scope check of an open engagement, nothing reads which
    hosts a step reaches: commands and web searches are refused."""
    engine = _engine()
    for step in (_step("nmap 10.0.0.1"), _step("ls"), {"tool": "web_search", "args": {}}):
        reason = engine.refusal_reason(step)
        assert reason is not None and "engagement" in reason, step
    assert engine.refusal_reason({"tool": "file_read", "args": {"path": "notes.txt"}}) is None


def test_a_check_that_provides_the_engagement_lifts_it(engagement_profile):
    def scope(step):
        return None

    safety.add_step_check(scope, provides="engagement")
    assert _engine().refusal_reason(_step("nmap 10.0.0.1")) is None
    safety.remove_step_check(scope)
    assert _engine().refusal_reason(_step("nmap 10.0.0.1")) is not None


def test_another_check_does_not_lift_it(engagement_profile):
    safety.add_step_check(lambda step: None)
    safety.add_step_check(lambda step: None, provides="something else")
    assert _engine().refusal_reason(_step("nmap 10.0.0.1")) is not None


def test_other_profiles_need_no_engagement():
    assert _engine().refusal_reason(_step("nmap 10.0.0.1")) is None


def test_the_profile_key_is_read(tmp_path):
    from codebud import profiles

    (tmp_path / "p.toml").write_text("engagement = true\n")
    assert profiles._load_file("p", tmp_path / "p.toml").engagement is True
    (tmp_path / "q.toml").write_text('engagement = "yes"\n')
    with pytest.raises(profiles.ProfileError):
        profiles._load_file("q", tmp_path / "q.toml")


# --- step recorders (step 5 plan, PR 7: the evidence log) -----------------------------


def _executor(tool=None):
    from codebud.executor import Executor

    class Registry:
        def get(self, name):
            return tool

    class Memory:
        def add_snapshot(self, step, result):
            pass

    return Executor(Registry(), None, Memory(), safety=_engine())


class _Tool:
    def __init__(self, returncode=0):
        self.ran = 0
        self.returncode = returncode

    def run(self, args):
        self.ran += 1
        return {"stdout": "out", "stderr": "", "returncode": self.returncode}


def _plan(cmd):
    return {"plan": [{"id": "step_0", "tool": "command", "description": "d", "args": {"cmd": cmd}}]}


@pytest.fixture
def recorded(monkeypatch):
    events = []
    monkeypatch.setattr(safety, "_STEP_RECORDERS", [])
    safety.add_step_recorder(
        lambda event, step, result: events.append((event, step["args"]["cmd"], result))
    )
    return events


@pytest.mark.parametrize("returncode", [0, 1])
def test_a_step_is_recorded_before_and_after_it_runs(recorded, returncode):
    tool = _Tool(returncode)
    _executor(tool).execute_plan(_plan("ls"))
    assert [e[0] for e in recorded] == ["start", "done"]
    assert recorded[1][2]["returncode"] == returncode and recorded[1][2]["stdout"] == "out"


def test_a_refused_step_is_recorded(recorded):
    _executor(_Tool()).execute_plan(_plan("rm -rf /"))
    assert [e[0] for e in recorded] == ["refused"]
    assert recorded[0][2]["error"]


def test_a_step_that_cannot_be_recorded_does_not_run(monkeypatch):
    monkeypatch.setattr(safety, "_STEP_RECORDERS", [])

    def broken(event, step, result):
        raise OSError("disk full")

    safety.add_step_recorder(broken)
    tool = _Tool()
    result = _executor(tool).execute_plan(_plan("ls"))
    assert tool.ran == 0
    assert result["status"] == "step_refused" and "disk full" in result["error"]


def test_a_crashing_tool_is_recorded(recorded):
    class Crash:
        def run(self, args):
            raise RuntimeError("boom")

    _executor(Crash()).execute_plan(_plan("ls"))
    assert [e[0] for e in recorded] == ["start", "done"] and "boom" in recorded[1][2]["error"]
