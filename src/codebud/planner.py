"""
codebud/planner.py

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
import logging
from collections.abc import Callable
from typing import Any

from . import profiles
from .llm_stub import LLMUnavailable, call_llm, num_ctx
from .modes import prompt_note, step_allowed

logger = logging.getLogger(__name__)

# Tokens kept free in the context window for the model's JSON answer.
_OUTPUT_RESERVE_TOKENS = 1024

# Share of the context window retrieved project context may use (D13e).
CONTEXT_SHARE = 0.25

# Conservative characters-per-token ratio for estimating prompt size
# (measured ~3.5 for planner prompts on qwen2.5-coder); erring low means
# history is trimmed slightly early rather than the prompt overflowing.
_CHARS_PER_TOKEN = 3


def estimate_tokens(text: str) -> int:
    """Return a conservative estimate of the token count of ``text``."""
    return len(text) // _CHARS_PER_TOKEN + 1


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
        on_chunk: Callable[[str], None] | None = None,
        history: list[dict[str, str]] | None = None,
        context_hits: list | None = None,
    ) -> dict[str, Any]:
        """Generate a plan using the LLM.

        Makes one primary attempt and, if parsing fails, one automatic retry
        with a stricter JSON-only prompt.  ``on_chunk`` is forwarded to the
        LLM backend to drive a live progress indicator.

        ``history`` is an optional list of prior turns in the form
        ``[{"role": "user"|"assistant", "content": str}, ...]``.  When
        provided, the last N turns are injected into the prompt so the model
        can see what was said and done before this request.

        ``context_hits`` are search hits from the project index
        (codebud.rag.context.retrieve); they are shown to the model as
        reference material, within a quarter of the context window.
        """

        prompt = self._build_prompt(user_message, history=history, context_hits=context_hits)
        result = self._attempt(prompt, on_chunk=on_chunk)
        if result is not None and result.get("status") == "plan_invalid":
            # Steps miss required arguments: ask once more, saying what was
            # wrong. The note is appended, so the cached prompt prefix holds.
            retry = self._attempt(
                prompt + self._invalid_plan_note(result.pop("problems")), on_chunk=on_chunk
            )
            if retry is not None:
                retry.pop("problems", None)
                return retry
            return result
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
        on_chunk: Callable[[str], None] | None = None,
    ) -> dict[str, Any] | None:
        """Call the LLM once and return a valid plan dict, or None on failure."""
        try:
            llm_output = call_llm(prompt, on_chunk=on_chunk)
        except LLMUnavailable as exc:
            return {"status": "llm_unavailable", "error": str(exc)}
        if not llm_output:
            return {"status": "plan_error", "error": "LLM returned empty output"}

        llm_output = self._strip_code_fence(llm_output)

        try:
            plan_json = json.loads(llm_output)
        except Exception:
            return None  # signal caller to retry

        if not self._validate_plan_structure(plan_json):
            return {"status": "plan_error", "error": "LLM returned malformed plan"}

        problems = self._arg_problems(plan_json)
        if problems:
            return {
                "status": "plan_invalid",
                "problems": problems,
                "error": "The model's plan is invalid: " + "; ".join(problems) + ".",
            }

        # A well-formed plan the mode or the safety policy forbids is
        # reported as such, not turned into a chat reply that hides why.
        refused = self._mode_refusal(plan_json) or self._safety_refusal(plan_json)
        if refused is not None:
            return refused

        return plan_json

    # ------------------------------------------------------------------
    # Prompt Construction
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Prompt Construction
    # ------------------------------------------------------------------

    _FEW_SHOT_EXAMPLES = """\
EXAMPLES:

Example 1:
User: "list the files in the current directory"
{"status": "ok", "plan": [{"id": "step_0", "description": "list files", "tool": "command", "args": {"cmd": "ls -la"}}]}

Example 2:
User: "run the tests and tell me which ones fail"
{"status": "ok", "plan": [{"id": "step_0", "description": "run pytest", "tool": "command", "args": {"cmd": "pytest -q"}}]}

Example 3:
User: "create a file called hello.py that prints Hello, world"
{"status": "ok", "plan": [{"id": "step_0", "description": "write hello.py", "tool": "file_write", "args": {"path": "hello.py", "content": "print('Hello, world')\\n"}}]}

Example 4:
User: "add a docstring at the top of src/codebud/planner.py"
{"status": "ok", "plan": [{"id": "step_0", "description": "read planner.py", "tool": "file_read", "args": {"path": "src/codebud/planner.py"}}, {"id": "step_1", "description": "patch in docstring", "tool": "patch", "args": {"patch": "--- a/src/codebud/planner.py\n+++ b/src/codebud/planner.py\n@@ -0,0 +1,2 @@\n+\"\"\"LLM-driven planner.\"\"\"\n+\n"}}]}

Example 5:
User: "order me a pizza"
{"status": "plan_error", "error": "This request cannot be fulfilled with the available tools."}
"""

    # Maximum number of prior turns to inject into the prompt.
    _HISTORY_WINDOW = 6

    def _build_prompt(
        self,
        user_message: str,
        history: list[dict[str, str]] | None = None,
        context_hits: list | None = None,
    ) -> str:
        """Build the full planning prompt with tools, rules, examples, and request."""

        # Keep the prompt inside the context window: if it would overflow,
        # drop retrieved chunks (worst first), then the oldest history turns
        # (D13e: conversation continuity wins). Ollama would otherwise cut
        # the *start* of the prompt, i.e. these instructions.
        window = list(history[-self._HISTORY_WINDOW :]) if history else []
        hits = list(context_hits or [])
        budget = num_ctx() - _OUTPUT_RESERVE_TOKENS
        context_budget = int(num_ctx() * CONTEXT_SHARE)

        def render() -> str:
            block = ""
            if hits:
                # Imported here: codebud.rag.context imports this module.
                from codebud.rag.context import format_context

                block = format_context(hits, context_budget)
            return self._render_prompt(user_message, window, block)

        prompt = render()
        while hits and estimate_tokens(prompt) > budget:
            hits.pop()
            prompt = render()
        while window and estimate_tokens(prompt) > budget:
            window.pop(0)
            prompt = render()
        if estimate_tokens(prompt) > budget:
            logger.warning(
                "Planner prompt (~%d tokens) exceeds the context budget (%d); "
                "raise CODEBUD_NUM_CTX or shorten the request",
                estimate_tokens(prompt),
                budget,
            )
        return prompt

    def estimate_prompt_tokens(
        self, history: list[dict[str, str]] | None = None, context_hits: list | None = None
    ) -> int:
        """Estimate the size of the next planning prompt for ``history``.

        Uses the same trimming as a real request, so the number reflects
        what would actually be sent (minus the next user message).
        ``context_hits`` stands in for the context the next request will
        retrieve (callers pass the last request's).
        """
        return estimate_tokens(self._build_prompt("", history=history, context_hits=context_hits))

    def _render_prompt(
        self, user_message: str, history: list[dict[str, str]], context_block: str = ""
    ) -> str:
        """Render the planning prompt for ``user_message`` with ``history``.

        ``context_block`` (retrieved project context) goes after the history,
        right before the request: everything above it stays the same from
        one turn to the next, so Ollama's prompt cache still covers it.
        """
        tool_descriptions = self.tool_registry.describe_tools()
        safety_rules = self.safety_engine.describe_rules()
        history_block = self._format_history(history)
        mode_block = prompt_note(getattr(self.safety_engine, "mode", "")) + profiles.prompt_note()

        return f"""\
You are a precise coding-agent planner. Your ONLY job is to output a JSON plan.

CRITICAL RULES:
1. Output ONLY valid JSON. No prose, no markdown, no code fences, no explanation.
2. Every step must use one of the tools listed below.
3. Use "command" (not "file_read") to list directory contents, search files, or run programs.
4. "file_read" is only for reading a specific named file whose path you already know.
5. Step ids must be sequential: "step_0", "step_1", ...
6. To run tests use: {{"tool": "command", "args": {{"cmd": "pytest -q"}}}}
7. To change a few lines in a file use "patch", not "file_write". Never put shell variables or command substitution inside "file_write" content.
8. Work carefully but get the job done: keep changes inside the project, read a file before changing it, and never delete or overwrite anything the request did not ask for.

AVAILABLE TOOLS:
{tool_descriptions}

SAFETY RULES (commands violating these will be rejected):
{safety_rules}

RESPONSE FORMAT:
{{"status": "ok", "plan": [{{"id": "step_0", "description": "...", "tool": "...", "args": {{...}}}}]}}

On failure:
{{"status": "plan_error", "error": "reason"}}

{self._FEW_SHOT_EXAMPLES}{mode_block}{history_block}{context_block}Now respond to this request. Output JSON only.

USER REQUEST: {user_message}
"""

    def _format_history(self, history: list[dict[str, str]] | None) -> str:
        """Render the last _HISTORY_WINDOW turns as a prompt block.

        Returns an empty string when there is no history so the prompt is
        byte-for-byte identical to the pre-history version on the first turn.
        """
        if not history:
            return ""

        window = history[-self._HISTORY_WINDOW :]
        lines = ["CONVERSATION HISTORY (most recent last):\n"]
        for turn in window:
            role = turn.get("role", "user").upper()
            content = turn.get("content", "").strip()
            lines.append(f"{role}: {content}")
        lines.append("")  # blank line before "Now respond"
        return "\n".join(lines) + "\n"

    def _build_retry_prompt(self, user_message: str) -> str:
        """Minimal retry prompt used when the first attempt returned non-JSON."""
        tool_names = self.tool_registry.names()
        return f"""\
OUTPUT ONLY VALID JSON. No markdown. No code fences. No text before or after the JSON.

Tools available: {tool_names}

Produce a plan for: {user_message}

Format: {{"status":"ok","plan":[{{"id":"step_0","description":"...","tool":"...","args":{{...}}}}]}}
"""

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _mode_refusal(self, plan_json: dict[str, Any]) -> dict[str, Any] | None:
        """Return a plan_refused result if the plan breaks the active mode."""
        mode = getattr(self.safety_engine, "mode", "")
        plan = plan_json.get("plan")
        if not isinstance(plan, list):
            return None
        blocked = sorted(
            {
                str(step.get("tool"))
                for step in plan
                if isinstance(step, dict) and not step_allowed(mode, step)
            }
        )
        if not blocked:
            return None
        return {
            "status": "plan_refused",
            "error": f"The plan uses tools not allowed in {mode} mode: {', '.join(blocked)}.",
        }

    def _validate_plan_structure(self, plan_json: dict[str, Any]) -> bool:
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
            if not isinstance(step.get("args"), dict):
                return False

            # Validate tool exists
            if not self.tool_registry.has(step["tool"]):
                return False

        return True

    def _arg_problems(self, plan_json: dict[str, Any]) -> list[str]:
        """Describe steps that miss an argument their tool requires.

        Extra arguments alone are tolerated (models add harmless keys);
        they are named only to explain a missing one.
        """
        problems = []
        for number, step in enumerate(plan_json.get("plan", []), 1):
            tool = self.tool_registry.get(step.get("tool"))
            args = step.get("args", {})
            missing = [a for a in getattr(tool, "required_args", ()) if a not in args]
            if missing:
                given = ", ".join(args) or "no arguments"
                needs = ", ".join(f'"{a}"' for a in missing)
                problems.append(f"step {number} ({step['tool']}) needs {needs}; it gave {given}")
        return problems

    @staticmethod
    def _invalid_plan_note(problems: list[str]) -> str:
        lines = "\n".join(f"- {p}" for p in problems)
        return (
            f"\nYOUR PREVIOUS PLAN WAS INVALID:\n{lines}\n"
            "Use exactly the argument names listed under AVAILABLE TOOLS, and real paths. "
            "Output the corrected JSON plan only.\n"
        )

    def _safety_refusal(self, plan_json: dict[str, Any]) -> dict[str, Any] | None:
        """Return a plan_refused result naming the first step the policy refuses."""
        for number, step in enumerate(plan_json.get("plan", []), 1):
            if self.safety_engine.validate_step(step):
                continue
            reason = self.safety_engine.refusal_reason(step) or "refused by the safety policy"
            what = step.get("args", {}).get("cmd") or step.get("tool")
            return {
                "status": "plan_refused",
                "error": f"The safety policy refused step {number} ({what}): {reason}.",
            }
        return None

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
            text = text[text.index("\n") + 1 :] if "\n" in text else text[3:]
            # Drop the closing fence if present
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
        return text.strip()
