"""
agent/safety.py

This module defines the SafetyEngine class, which validates LLM-generated
steps before execution. It enforces:

- Harmless command list
- Powerful command list
- Trusted sequences
- Tool-level safety rules

The safety engine is consulted by:
- planner.py (during plan validation)
- core.py (final plan validation)

It does NOT:
- execute commands
- modify plans
- interact with the LLM
"""

import json
import os
from typing import Any

CONFIG_DIR = "config"
HARMLESS_PATH = os.path.join(CONFIG_DIR, "harmless_commands.json")
POWERFUL_PATH = os.path.join(CONFIG_DIR, "powerful_commands.json")
TRUSTED_SEQ_PATH = os.path.join(CONFIG_DIR, "trusted_sequences.json")


class SafetyEngine:
    """
    Validates steps produced by the LLM planner.

    Responsibilities:
    - Load safety policies
    - Validate each step
    - Provide safety descriptions for planner prompt
    """

    def __init__(self):
        self.harmless_commands = self._load_json(HARMLESS_PATH)
        self.powerful_commands = self._load_json(POWERFUL_PATH)
        self.trusted_sequences = self._load_json(TRUSTED_SEQ_PATH)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate_step(self, step: dict[str, Any]) -> bool:
        """
        Validate a single step.

        Checks:
        - Tool exists (handled by tool registry)
        - If tool is 'command', validate command safety
        - Trusted sequence validation (optional)
        """

        tool = step.get("tool")
        args = step.get("args", {})

        # Command safety
        if tool == "command":
            cmd = args.get("cmd", "")
            if not self._validate_command(cmd):
                return False

        # Additional tool-specific safety rules can be added here

        return True

    def describe_rules(self) -> str:
        """
        Return a human-readable description of safety rules for the planner prompt.
        """
        return f"""
- Harmless commands: {self.harmless_commands}
- Powerful commands: {self.powerful_commands}
- Trusted sequences: {self.trusted_sequences}
- Dangerous commands are forbidden.
- Tools must be used responsibly.
"""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_command(self, cmd: str) -> bool:
        """
        Validate shell commands using harmless/powerful lists.
        """

        # Harmless commands: always allowed
        for allowed in self.harmless_commands:
            if cmd.startswith(allowed):
                return True

        # Powerful commands: allowed only if explicitly listed
        for allowed in self.powerful_commands:
            if cmd.startswith(allowed):
                return True

        # Otherwise: reject
        return False

    def _load_json(self, path: str) -> list[str]:
        """Load a JSON list from disk."""
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return []
