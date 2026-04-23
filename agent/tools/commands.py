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

from typing import Any


class CommandTool:
    """
    Execute a shell command inside the sandbox.

    Args:
    {
        "cmd": "shell command"
    }
    """

    description = "Execute a shell command inside the sandbox."
    usage_hint = (
        "Use for any shell operation: listing files, running tests, searching code, "
        'checking git status, etc. Required args: "cmd" (string, a shell command). '
        'Examples: {"cmd": "ls -la"}, {"cmd": "pytest -q"}, {"cmd": "grep -r TODO ."}'
    )

    def __init__(self, sandbox):
        self.sandbox = sandbox

    def run(self, args: dict[str, Any]) -> dict[str, Any]:
        cmd = args.get("cmd")

        if not cmd:
            return {"stdout": "", "stderr": "Missing required argument: cmd", "returncode": 1}

        # Delegate to sandbox
        return self.sandbox.run_command(cmd)
