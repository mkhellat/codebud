"""
agent/core.py

This module defines the AgentCore class, the central orchestrator of the
Cursor-style coding agent. It coordinates:

- LLM-driven planning
- Step execution
- Safety validation
- Memory timeline snapshots
- Tool registry access
- Sandbox execution
- Regeneration logic

The AgentCore exposes two public methods:

1. handle_user_message(message: str)
   → returns a plan or a plan_error

2. regenerate(payload: dict)
   → regenerates a plan based on the same user message

This file contains NO UI logic and NO OpenClaw logic.
It is purely the internal agent brain.
"""

from typing import Callable, Dict, Any, Optional

from .planner import LLMPlanner
from .executor import Executor
from .memory import MemoryStore
from .safety import SafetyEngine
from .sandbox import Sandbox
from .tools.tool_registry import ToolRegistry


class AgentCore:
    """
    The central orchestrator of the agent.

    Responsibilities:
    - Receive user messages
    - Ask the LLM to generate a plan
    - Validate the plan
    - Execute steps (when requested)
    - Store memory snapshots
    - Regenerate plans
    """

    def __init__(self):
        # Initialize subsystems
        self.sandbox = Sandbox()
        self.tool_registry = ToolRegistry(self.sandbox)
        self.safety = SafetyEngine()
        self.memory = MemoryStore()
        self.planner = LLMPlanner(self.tool_registry, self.safety)
        self.executor = Executor(self.tool_registry, self.sandbox, self.memory)

        # Store last user message for regeneration
        self.last_user_message = None

    # ----------------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------------

    def handle_user_message(
        self,
        message: str,
        on_chunk: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """Called when the user sends a message. Returns a plan or plan_error.

        ``on_chunk`` is forwarded to the LLM backend to drive a live progress
        indicator in the CLI without coupling display logic to this class.
        """
        self.last_user_message = message

        plan_output = self.planner.generate_plan(message, on_chunk=on_chunk)

        # If planner failed, return error
        if plan_output.get("status") != "ok":
            return {
                "status": "plan_error",
                "error": plan_output.get("error", "Unknown planning error")
            }

        # Validate plan structure
        if not self._validate_plan(plan_output):
            return {
                "status": "plan_error",
                "error": "Planner returned malformed plan"
            }

        return plan_output

    def regenerate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Called when OpenClaw requests plan regeneration.

        Steps:
        1. Use the last user message
        2. Ask the LLM planner for a new plan
        3. Validate and return it
        """
        if not self.last_user_message:
            return {
                "status": "plan_error",
                "error": "No previous user message to regenerate from"
            }

        plan_output = self.planner.generate_plan(self.last_user_message)

        if plan_output.get("status") != "ok":
            return {
                "status": "plan_error",
                "error": plan_output.get("error", "Unknown planning error")
            }

        if not self._validate_plan(plan_output):
            return {
                "status": "plan_error",
                "error": "Planner returned malformed plan"
            }

        return plan_output

    # ----------------------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------------------

    def _validate_plan(self, plan_output: Dict[str, Any]) -> bool:
        """
        Ensures the plan follows the Cursor-style contract:

        {
            "status": "ok",
            "plan": [
                {
                    "id": "step_0",
                    "description": "...",
                    "tool": "file_write",
                    "args": {...}
                },
                ...
            ]
        }
        """
        plan = plan_output.get("plan")
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
            if not self.safety.validate_step(step):
                return False

        return True
