"""
agent/tools/commands.py

This module defines the CommandTool, which executes shell commands inside
the sandbox environment.

Tool name: "command"

Args:
{
    "cmd": "shell command string"
}

The tool:
- Delegates execution to sandbox.run_command(cmd)
- Returns stdout/stderr/returncode
"""

from typing import Dict, Any


class CommandTool:
    """
    Execute a shell command inside the sandbox.

    Args:
    {
        "cmd": "shell command"
    }
    """

    description = "Execute a shell command inside the sandbox."

    def __init__(self, sandbox):
        self.sandbox = sandbox

    def run(self, args: Dict[str, Any]) -> Dict[str, Any]:
        cmd = args.get("cmd")

        if not cmd:
            return {
                "stdout": "",
                "stderr": "Missing required argument: cmd",
                "returncode": 1
            }

        # Delegate to sandbox
        return self.sandbox.run_command(cmd)
