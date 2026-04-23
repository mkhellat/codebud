"""
Tests for SafetyEngine, Executor, and PatchTool.

These cover the three seams where a misbehaving LLM plan could do real
damage: the safety policy gate, the executor dispatch loop, and the
file-patching tool.
"""

import types
import pytest

from agent.safety import SafetyEngine
from agent.executor import Executor
from agent.tools.patcher import PatchTool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_safety(harmless=None, powerful=None, trusted=None):
    """Build a SafetyEngine with injected policy lists (no file I/O)."""
    engine = SafetyEngine.__new__(SafetyEngine)
    engine.harmless_commands = harmless or ["echo", "ls", "cat", "pwd", "python3"]
    engine.powerful_commands = powerful or ["grep", "cp", "mv"]
    engine.trusted_sequences = trusted or []
    return engine


def _make_executor(tools=None):
    """Build an Executor with a lightweight fake registry, sandbox, and memory."""

    class FakeRegistry:
        def __init__(self, tools):
            self._tools = tools or {}

        def get(self, name):
            return self._tools.get(name)

        def has(self, name):
            return name in self._tools

    class FakeSandbox:
        pass

    class FakeMemory:
        def add_snapshot(self, step, result):
            pass

    return Executor(FakeRegistry(tools or {}), FakeSandbox(), FakeMemory())


def _make_tool(stdout="ok", stderr="", returncode=0, raises=None):
    """Build a minimal fake tool whose run() returns the given values."""

    class FakeTool:
        def run(self, args):
            if raises:
                raise raises
            return {"stdout": stdout, "stderr": stderr, "returncode": returncode}

    return FakeTool()


# ---------------------------------------------------------------------------
# SafetyEngine — command validation
# ---------------------------------------------------------------------------


class TestSafetyEngineCommands:
    def test_harmless_command_allowed(self):
        engine = _make_safety()
        step = {"tool": "command", "args": {"cmd": "ls -la"}}
        assert engine.validate_step(step) is True

    def test_harmless_echo_allowed(self):
        engine = _make_safety()
        step = {"tool": "command", "args": {"cmd": "echo hello"}}
        assert engine.validate_step(step) is True

    def test_powerful_command_allowed(self):
        engine = _make_safety()
        step = {"tool": "command", "args": {"cmd": "grep -r foo ."}}
        assert engine.validate_step(step) is True

    def test_dangerous_command_rejected(self):
        engine = _make_safety()
        for cmd in ["rm -rf /", "sudo rm -rf /", "dd if=/dev/zero", ":(){:|:&};:"]:
            step = {"tool": "command", "args": {"cmd": cmd}}
            assert engine.validate_step(step) is False, f"should reject: {cmd!r}"

    def test_empty_command_rejected(self):
        engine = _make_safety()
        step = {"tool": "command", "args": {"cmd": ""}}
        assert engine.validate_step(step) is False

    def test_partial_match_does_not_bypass(self):
        # "echo" is harmless but "echofoo" with a different binary should not match
        engine = _make_safety(harmless=["echo "])  # trailing space required
        step = {"tool": "command", "args": {"cmd": "echofoo bar"}}
        assert engine.validate_step(step) is False


# ---------------------------------------------------------------------------
# SafetyEngine — non-command tools
# ---------------------------------------------------------------------------


class TestSafetyEngineNonCommand:
    def test_file_read_step_always_valid(self):
        engine = _make_safety()
        step = {"tool": "file_read", "args": {"path": "/etc/passwd"}}
        assert engine.validate_step(step) is True

    def test_file_write_step_always_valid(self):
        engine = _make_safety()
        step = {"tool": "file_write", "args": {"path": "out.txt", "content": "x"}}
        assert engine.validate_step(step) is True

    def test_patch_step_always_valid(self):
        engine = _make_safety()
        step = {"tool": "patch", "args": {"patch": "--- a/f\n+++ b/f\n@@\n+x"}}
        assert engine.validate_step(step) is True

    def test_describe_rules_returns_nonempty_string(self):
        engine = _make_safety()
        rules = engine.describe_rules()
        assert isinstance(rules, str)
        assert len(rules) > 0


# ---------------------------------------------------------------------------
# Executor — dispatch
# ---------------------------------------------------------------------------


class TestExecutorDispatch:
    def test_unknown_tool_returns_step_error(self):
        ex = _make_executor(tools={})
        plan = {
            "status": "ok",
            "plan": [{"id": "s0", "description": "x", "tool": "nonexistent", "args": {}}],
        }
        result = ex.execute_plan(plan)
        assert result["status"] == "step_error"
        assert "nonexistent" in result["error"]

    def test_happy_path_single_step(self):
        ex = _make_executor(tools={"mytool": _make_tool(stdout="hello")})
        plan = {
            "status": "ok",
            "plan": [{"id": "s0", "description": "do it", "tool": "mytool", "args": {}}],
        }
        result = ex.execute_plan(plan)
        assert result["status"] == "ok"
        assert result["results"]["s0"]["stdout"] == "hello"

    def test_nonzero_returncode_stops_execution(self):
        tools = {
            "fail": _make_tool(returncode=1, stderr="oops"),
            "next": _make_tool(stdout="should not run"),
        }
        ex = _make_executor(tools=tools)
        plan = {
            "status": "ok",
            "plan": [
                {"id": "s0", "description": "fail", "tool": "fail", "args": {}},
                {"id": "s1", "description": "next", "tool": "next", "args": {}},
            ],
        }
        result = ex.execute_plan(plan)
        assert result["status"] == "step_error"
        assert "s1" not in result.get("results", {})

    def test_tool_crash_returns_step_error(self):
        tools = {"boom": _make_tool(raises=RuntimeError("kaboom"))}
        ex = _make_executor(tools=tools)
        plan = {
            "status": "ok",
            "plan": [{"id": "s0", "description": "x", "tool": "boom", "args": {}}],
        }
        result = ex.execute_plan(plan)
        assert result["status"] == "step_error"
        assert "kaboom" in result["error"]

    def test_malformed_tool_result_returns_step_error(self):
        class BadTool:
            def run(self, args):
                return {"only_stdout": "missing required keys"}

        ex = _make_executor(tools={"bad": BadTool()})
        plan = {
            "status": "ok",
            "plan": [{"id": "s0", "description": "x", "tool": "bad", "args": {}}],
        }
        result = ex.execute_plan(plan)
        assert result["status"] == "step_error"

    def test_empty_plan_returns_ok(self):
        ex = _make_executor()
        result = ex.execute_plan({"status": "ok", "plan": []})
        assert result["status"] == "ok"
        assert result["results"] == {}


# ---------------------------------------------------------------------------
# PatchTool — round-trip
# ---------------------------------------------------------------------------


class TestPatchTool:
    def test_missing_patch_arg(self):
        result = PatchTool().run({})
        assert result["returncode"] == 1
        assert "Missing" in result["stderr"]

    def test_round_trip_simple_addition(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "hello.txt"
        target.write_text("line1\nline2\n")

        patch = (
            "--- a/hello.txt\n"
            "+++ b/hello.txt\n"
            "@@ -1,2 +1,3 @@\n"
            " line1\n"
            " line2\n"
            "+line3\n"
        )
        result = PatchTool().run({"patch": patch})
        assert result["returncode"] == 0
        assert target.read_text() == "line1\nline2\nline3\n"

    def test_round_trip_line_removal(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "hello.txt"
        target.write_text("keep\nremove\n")

        patch = (
            "--- a/hello.txt\n"
            "+++ b/hello.txt\n"
            "@@ -1,2 +1,1 @@\n"
            " keep\n"
            "-remove\n"
        )
        result = PatchTool().run({"patch": patch})
        assert result["returncode"] == 0
        assert target.read_text() == "keep\n"

    def test_file_not_found(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        patch = (
            "--- a/ghost.txt\n"
            "+++ b/ghost.txt\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        result = PatchTool().run({"patch": patch})
        assert result["returncode"] == 1
        assert "ghost.txt" in result["stderr"]
