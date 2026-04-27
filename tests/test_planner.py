"""
tests/test_planner.py

Tests for LLMPlanner: prompt content, retry logic, and JSON parsing.
"""

import json
from unittest.mock import MagicMock, patch

from agent.planner import LLMPlanner

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_registry(tool_names=None):
    """Minimal ToolRegistry substitute."""
    if tool_names is None:
        tool_names = ["file_write", "file_read", "command", "patch"]

    class FakeTool:
        description = "a tool"
        usage_hint = "use me"

    reg = MagicMock()
    reg.tools = {n: FakeTool() for n in tool_names}
    reg.describe_tools.return_value = "\n".join(
        f"- {n}: a tool\n  When to use: use me" for n in tool_names
    )
    reg.has.side_effect = lambda n: n in tool_names
    return reg


def _make_safety():
    safety = MagicMock()
    safety.describe_rules.return_value = "No dangerous commands."
    safety.validate_step.return_value = True
    return safety


def _good_plan(tool="command"):
    return {
        "status": "ok",
        "plan": [
            {
                "id": "step_0",
                "description": "do something",
                "tool": tool,
                "args": {"cmd": "ls"},
            }
        ],
    }


# ---------------------------------------------------------------------------
# Prompt content tests
# ---------------------------------------------------------------------------


class TestPromptContent:
    def test_few_shot_examples_in_prompt(self):
        planner = LLMPlanner(_make_registry(), _make_safety())
        prompt = planner._build_prompt("list files")
        assert "Example 1" in prompt
        assert "Example 2" in prompt
        assert "file_read" in prompt
        assert "command" in prompt

    def test_tool_descriptions_in_prompt(self):
        planner = LLMPlanner(_make_registry(), _make_safety())
        prompt = planner._build_prompt("do something")
        assert "file_write" in prompt
        assert "file_read" in prompt
        assert "command" in prompt

    def test_safety_rules_in_prompt(self):
        planner = LLMPlanner(_make_registry(), _make_safety())
        prompt = planner._build_prompt("do something")
        assert "No dangerous commands" in prompt

    def test_user_message_in_prompt(self):
        planner = LLMPlanner(_make_registry(), _make_safety())
        prompt = planner._build_prompt("my unique request xyz")
        assert "my unique request xyz" in prompt

    def test_critical_rule_file_read_vs_command(self):
        """The prompt must warn that file_read is not for listing directories."""
        planner = LLMPlanner(_make_registry(), _make_safety())
        prompt = planner._build_prompt("any request")
        assert "file_read" in prompt
        assert "command" in prompt
        # The key disambiguation hint must be present
        assert "list directory" in prompt or "directory contents" in prompt

    def test_critical_rule_pytest(self):
        """The prompt must show pytest as the way to run tests."""
        planner = LLMPlanner(_make_registry(), _make_safety())
        prompt = planner._build_prompt("any request")
        assert "pytest" in prompt

    def test_critical_rule_no_shell_vars_in_file_write(self):
        """The prompt must forbid shell variable substitution in file_write content."""
        planner = LLMPlanner(_make_registry(), _make_safety())
        prompt = planner._build_prompt("any request")
        assert "file_write" in prompt
        assert "shell" in prompt or "command substitution" in prompt or "variables" in prompt

    def test_patch_example_in_prompt(self):
        """A patch-based example must appear in the few-shot section."""
        planner = LLMPlanner(_make_registry(), _make_safety())
        prompt = planner._build_prompt("any request")
        assert '"tool": "patch"' in prompt or "'tool': 'patch'" in prompt

    def test_usage_hints_in_tool_descriptions(self):
        """describe_tools() must include usage_hint lines."""
        registry = _make_registry()
        result = registry.describe_tools()
        assert "When to use" in result

    def test_retry_prompt_contains_tool_names(self):
        planner = LLMPlanner(_make_registry(["file_write", "command"]), _make_safety())
        prompt = planner._build_retry_prompt("do something")
        assert "file_write" in prompt
        assert "command" in prompt
        assert "JSON" in prompt


# ---------------------------------------------------------------------------
# Retry logic tests
# ---------------------------------------------------------------------------


class TestRetryLogic:
    def test_valid_json_first_attempt_no_retry(self):
        """If first call returns valid JSON, the second call is never made."""
        planner = LLMPlanner(_make_registry(), _make_safety())
        call_count = [0]
        good = json.dumps(_good_plan())

        def fake_llm(prompt, timeout=600.0, on_chunk=None):
            call_count[0] += 1
            return good

        with patch("agent.planner.call_llm", fake_llm):
            result = planner.generate_plan("do something")

        assert result["status"] == "ok"
        assert call_count[0] == 1

    def test_prose_first_then_valid_json_retry(self):
        """If first call returns prose, the retry call returns valid JSON."""
        planner = LLMPlanner(_make_registry(), _make_safety())
        responses = [
            "Sure! Here is my plan for you...",  # prose — invalid JSON
            json.dumps(_good_plan()),  # valid JSON on retry
        ]
        call_count = [0]

        def fake_llm(prompt, timeout=600.0, on_chunk=None):
            resp = responses[call_count[0]]
            call_count[0] += 1
            return resp

        with patch("agent.planner.call_llm", fake_llm):
            result = planner.generate_plan("do something")

        assert result["status"] == "ok"
        assert call_count[0] == 2

    def test_invalid_json_both_attempts_returns_plan_error(self):
        """If both attempts return invalid JSON, plan_error is returned."""
        planner = LLMPlanner(_make_registry(), _make_safety())

        def fake_llm(prompt, timeout=600.0, on_chunk=None):
            return "not json at all"

        with patch("agent.planner.call_llm", fake_llm):
            result = planner.generate_plan("do something")

        assert result["status"] == "plan_error"
        assert "invalid JSON" in result["error"]

    def test_empty_output_returns_plan_error_immediately(self):
        """Empty string output returns plan_error without a retry."""
        planner = LLMPlanner(_make_registry(), _make_safety())
        call_count = [0]

        def fake_llm(prompt, timeout=600.0, on_chunk=None):
            call_count[0] += 1
            return ""

        with patch("agent.planner.call_llm", fake_llm):
            result = planner.generate_plan("do something")

        assert result["status"] == "plan_error"
        assert "empty output" in result["error"]
        # Empty output short-circuits; first _attempt returns plan_error directly
        # so the retry is never reached
        assert call_count[0] == 1

    def test_code_fence_stripped_before_parse(self):
        """JSON wrapped in ```json...``` should be parsed successfully."""
        planner = LLMPlanner(_make_registry(), _make_safety())
        fenced = "```json\n" + json.dumps(_good_plan()) + "\n```"

        with patch("agent.planner.call_llm", return_value=fenced):
            result = planner.generate_plan("do something")

        assert result["status"] == "ok"

    def test_on_chunk_forwarded_to_llm(self):
        """on_chunk callback must be forwarded to call_llm."""
        planner = LLMPlanner(_make_registry(), _make_safety())
        received = {}

        def fake_llm(prompt, timeout=600.0, on_chunk=None):
            received["on_chunk"] = on_chunk
            return json.dumps(_good_plan())

        sentinel = lambda t: None  # noqa: E731
        with patch("agent.planner.call_llm", fake_llm):
            planner.generate_plan("do something", on_chunk=sentinel)

        assert received["on_chunk"] is sentinel


# ---------------------------------------------------------------------------
# Validation edge cases
# ---------------------------------------------------------------------------


class TestValidation:
    def test_unknown_tool_returns_plan_error(self):
        """A plan that names a non-existent tool must fail validation."""
        registry = _make_registry(["command"])
        planner = LLMPlanner(registry, _make_safety())
        bad_plan = {
            "status": "ok",
            "plan": [{"id": "step_0", "description": "x", "tool": "nonexistent", "args": {}}],
        }
        with patch("agent.planner.call_llm", return_value=json.dumps(bad_plan)):
            result = planner.generate_plan("do something")
        assert result["status"] == "plan_error"

    def test_missing_required_field_returns_plan_error(self):
        """A step without 'args' must fail validation."""
        planner = LLMPlanner(_make_registry(), _make_safety())
        bad_plan = {
            "status": "ok",
            "plan": [{"id": "step_0", "description": "x", "tool": "command"}],
        }
        with patch("agent.planner.call_llm", return_value=json.dumps(bad_plan)):
            result = planner.generate_plan("do something")
        assert result["status"] == "plan_error"
