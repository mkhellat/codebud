"""
agent/executor.py

This module defines the Executor class, which runs a Cursor-style linear plan.
The executor:

1. Receives a validated plan from the planner
2. Executes each step sequentially
3. Calls the appropriate tool from the tool registry
4. Captures stdout, stderr, returncode, and metadata
5. Stores memory snapshots
6. Stops on the first error
7. Returns a structured result object

The executor does NOT:
- generate plans
- validate safety rules
- interact with the LLM
- handle UI logic
"""

from typing import Dict, Any


class Executor:
    """
    Executes a linear plan step-by-step.

    Dependencies:
    - tool_registry: maps tool names → tool classes
    - sandbox: safe execution environment
    - memory_store: stores timeline snapshots
    """

    def __init__(self, tool_registry, sandbox, memory_store):
        self.tool_registry = tool_registry
        self.sandbox = sandbox
        self.memory = memory_store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute_plan(self, plan_output: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a validated plan.

        Returns:
        {
            "status": "ok",
            "results": {
                "step_0": {
                    "stdout": "...",
                    "stderr": "",
                    "returncode": 0,
                    "metadata": {...}
                },
                ...
            }
        }

        Or on error:
        {
            "status": "step_error",
            "error": "Command failed",
            "step": {...}
        }
        """

        plan = plan_output.get("plan", [])
        results = {}

        for step in plan:
            step_id = step["id"]
            tool_name = step["tool"]
            args = step["args"]

            tool = self.tool_registry.get(tool_name)

            # Execute the tool
            try:
                tool_result = tool.run(args)
            except Exception as e:
                return {
                    "status": "step_error",
                    "error": f"Tool '{tool_name}' crashed: {e}",
                    "step": step
                }

            # Validate tool result structure
            if not self._validate_tool_result(tool_result):
                return {
                    "status": "step_error",
                    "error": f"Tool '{tool_name}' returned malformed result",
                    "step": step
                }

            # Store result
            results[step_id] = {
                "stdout": tool_result.get("stdout", ""),
                "stderr": tool_result.get("stderr", ""),
                "returncode": tool_result.get("returncode", 0),
                "metadata": {
                    "description": step["description"],
                    "tool": tool_name,
                    "args": args
                }
            }

            # Add memory snapshot
            self.memory.add_snapshot(step, results[step_id])

            # Stop on error
            if tool_result.get("returncode", 0) != 0:
                return {
                    "status": "step_error",
                    "error": f"Step '{step_id}' failed",
                    "step": step
                }

        return {
            "status": "ok",
            "results": results
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_tool_result(self, result: Dict[str, Any]) -> bool:
        """
        Ensures tool results follow the contract:

        {
            "stdout": "...",
            "stderr": "...",
            "returncode": 0
        }
        """
        if not isinstance(result, dict):
            return False

        if "stdout" not in result:
            return False
        if "stderr" not in result:
            return False
        if "returncode" not in result:
            return False

        return True
