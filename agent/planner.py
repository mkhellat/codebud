"""
agent/planner.py

This module defines the LLMPlanner class, which generates a Cursor-style
linear plan using an LLM. The planner:

1. Builds a structured prompt describing:
   - available tools
   - required JSON format
   - safety rules
   - examples

2. Calls the LLM to generate a plan

3. Parses and validates the JSON output

4. Returns:
   {
       "status": "ok",
       "plan": [...]
   }
   or:
   {
       "status": "plan_error",
       "error": "..."
   }

The planner NEVER exposes chain-of-thought. It only returns the final plan.
"""

import json
from typing import Callable, Dict, Any, Optional

from .llm_stub import call_llm


class LLMPlanner:
    """
    LLM-driven planner that produces a linear plan in JSON format.

    The planner uses:
    - tool_registry: to list available tools
    - safety_engine: to validate tool usage
    """

    def __init__(self, tool_registry, safety_engine):
        self.tool_registry = tool_registry
        self.safety_engine = safety_engine

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_plan(
        self,
        user_message: str,
        on_chunk: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """Generate a plan using the LLM.

        Makes one primary attempt and, if parsing fails, one automatic retry
        with a stricter JSON-only prompt.  ``on_chunk`` is forwarded to the
        LLM backend to drive a live progress indicator.
        """

        prompt = self._build_prompt(user_message)
        result = self._attempt(prompt, on_chunk=on_chunk)
        if result is not None:
            return result

        # First attempt failed to produce valid JSON — retry once with a
        # minimal no-preamble prompt to overcome fence-wrapping or prose.
        retry_prompt = self._build_retry_prompt(user_message)
        result = self._attempt(retry_prompt, on_chunk=on_chunk)
        if result is not None:
            return result

        return {"status": "plan_error", "error": "LLM returned invalid JSON"}

    def _attempt(
        self,
        prompt: str,
        on_chunk: Optional[Callable[[str], None]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Call the LLM once and return a valid plan dict, or None on failure."""
        llm_output = call_llm(prompt, on_chunk=on_chunk)
        if not llm_output:
            return {"status": "plan_error", "error": "LLM returned empty output"}

        llm_output = self._strip_code_fence(llm_output)

        try:
            plan_json = json.loads(llm_output)
        except Exception:
            return None  # signal caller to retry

        if not self._validate_plan_structure(plan_json):
            return {"status": "plan_error", "error": "LLM returned malformed plan"}

        return plan_json

    # ------------------------------------------------------------------
    # Prompt Construction
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Prompt Construction
    # ------------------------------------------------------------------

    _FEW_SHOT_EXAMPLES = '''\
EXAMPLES (study these carefully before responding):

Example 1 — list files in the current directory:
User: "list the files in the current directory"
Response:
{"status": "ok", "plan": [{"id": "step_0", "description": "list files", "tool": "command", "args": {"cmd": "ls -la"}}]}

Example 2 — read a specific file:
User: "show me the contents of README.md"
Response:
{"status": "ok", "plan": [{"id": "step_0", "description": "read README.md", "tool": "file_read", "args": {"path": "README.md"}}]}

Example 3 — run the test suite:
User: "run the tests"
Response:
{"status": "ok", "plan": [{"id": "step_0", "description": "run pytest", "tool": "command", "args": {"cmd": "pytest -q"}}]}

Example 4 — create a new file:
User: "create a file called hello.py that prints Hello, world"
Response:
{"status": "ok", "plan": [{"id": "step_0", "description": "write hello.py", "tool": "file_write", "args": {"path": "hello.py", "content": "print('Hello, world')\\n"}}]}

Example 5 — multi-step task:
User: "read config.json and then run the tests"
Response:
{"status": "ok", "plan": [{"id": "step_0", "description": "read config.json", "tool": "file_read", "args": {"path": "config.json"}}, {"id": "step_1", "description": "run pytest", "tool": "command", "args": {"cmd": "pytest -q"}}]}

Example 6 — cannot help:
User: "order me a pizza"
Response:
{"status": "plan_error", "error": "This request cannot be fulfilled with the available tools."}
'''

    def _build_prompt(self, user_message: str) -> str:
        """Build the full planning prompt with tools, rules, examples, and request."""

        tool_descriptions = self.tool_registry.describe_tools()
        safety_rules = self.safety_engine.describe_rules()

        return f"""\
You are a precise coding-agent planner. Your ONLY job is to output a JSON plan.

CRITICAL RULES:
1. Output ONLY valid JSON. No prose, no markdown, no code fences, no explanation.
2. Every step must use one of the tools listed below.
3. Use "command" (not "file_read") to list directory contents, search files, or run programs.
4. "file_read" is only for reading a specific named file.
5. Step ids must be sequential: "step_0", "step_1", ...

AVAILABLE TOOLS:
{tool_descriptions}

SAFETY RULES (commands violating these will be rejected):
{safety_rules}

RESPONSE FORMAT:
{{"status": "ok", "plan": [{{"id": "step_0", "description": "...", "tool": "...", "args": {{...}}}}]}}

On failure:
{{"status": "plan_error", "error": "reason"}}

{self._FEW_SHOT_EXAMPLES}
Now respond to this request. Output JSON only.

USER REQUEST: {user_message}
"""

    def _build_retry_prompt(self, user_message: str) -> str:
        """Minimal retry prompt used when the first attempt returned non-JSON."""
        tool_names = list(self.tool_registry.tools.keys())
        return f"""\
OUTPUT ONLY VALID JSON. No markdown. No code fences. No text before or after the JSON.

Tools available: {tool_names}

Produce a plan for: {user_message}

Format: {{"status":"ok","plan":[{{"id":"step_0","description":"...","tool":"...","args":{{...}}}}]}}
"""

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_plan_structure(self, plan_json: Dict[str, Any]) -> bool:
        """
        Validate the structure of the LLM-generated plan.
        """

        if plan_json.get("status") != "ok":
            return False

        plan = plan_json.get("plan")
        if not isinstance(plan, list):
            return False

        for step in plan:
            if not isinstance(step, dict):
                return False
            if "id" not in step:
                return False
            if "description" not in step:
                return False
            if "tool" not in step:
                return False
            if "args" not in step:
                return False

            # Validate tool exists
            if not self.tool_registry.has(step["tool"]):
                return False

            # Validate safety
            if not self.safety_engine.validate_step(step):
                return False

        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        """Remove markdown code fences from LLM output.

        Models often wrap JSON in ```json ... ``` or ``` ... ```.
        This strips the fence lines and returns only the inner content.
        """
        text = text.strip()
        if text.startswith("```"):
            # Drop the opening fence line (e.g. ```json or ```)
            text = text[text.index("\n") + 1:] if "\n" in text else text[3:]
            # Drop the closing fence if present
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
        return text.strip()
