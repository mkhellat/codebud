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
from typing import Dict, Any

# NOTE:
# In your real implementation, replace this with your actual LLM call.
# For now, we simulate an LLM call with a placeholder.
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

    def generate_plan(self, user_message: str) -> Dict[str, Any]:
        """
        Generate a plan using the LLM.

        Steps:
        1. Build prompt
        2. Call LLM
        3. Parse JSON
        4. Validate structure
        """

        prompt = self._build_prompt(user_message)

        llm_output = call_llm(prompt)

        if not llm_output:
            return {
                "status": "plan_error",
                "error": "LLM returned empty output"
            }

        # Strip markdown code fences that some models add around JSON output
        llm_output = self._strip_code_fence(llm_output)

        # Parse JSON
        try:
            plan_json = json.loads(llm_output)
        except Exception:
            return {
                "status": "plan_error",
                "error": "LLM returned invalid JSON"
            }

        # Validate structure
        if not self._validate_plan_structure(plan_json):
            return {
                "status": "plan_error",
                "error": "LLM returned malformed plan"
            }

        return plan_json

    # ------------------------------------------------------------------
    # Prompt Construction
    # ------------------------------------------------------------------

    def _build_prompt(self, user_message: str) -> str:
        """
        Build the LLM prompt describing:
        - tools
        - JSON format
        - safety rules
        - user message
        """

        tool_descriptions = self.tool_registry.describe_tools()

        safety_rules = self.safety_engine.describe_rules()

        prompt = f"""
You are a planning agent. Your job is to convert the user's message into a
linear plan of steps. Each step uses exactly one tool.

TOOLS:
{tool_descriptions}

SAFETY RULES:
{safety_rules}

RESPONSE FORMAT (JSON ONLY, NO EXTRA TEXT):
{{
  "status": "ok",
  "plan": [
    {{
      "id": "step_0",
      "description": "Human-readable description",
      "tool": "tool_name",
      "args": {{ ... }}
    }}
  ]
}}

If you cannot produce a valid plan, respond with:
{{
  "status": "plan_error",
  "error": "Explanation"
}}

USER MESSAGE:
{user_message}

Remember:
- DO NOT include chain-of-thought.
- DO NOT include explanations.
- ONLY return valid JSON.
"""

        return prompt

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
