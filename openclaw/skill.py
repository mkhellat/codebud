"""
openclaw/skill.py

This module defines the OpenClawSkill class, which exposes the agent to the
OpenClaw UI. It acts as a thin wrapper around AgentCore.

Responsibilities:
- Receive user messages from OpenClaw
- Call AgentCore.handle_user_message()
- Return the plan to the UI
- Handle regeneration requests
- Provide metadata for the UI
"""

from typing import Dict, Any
from agent.core import AgentCore


class OpenClawSkill:
    """
    OpenClaw skill wrapper for the coding agent.

    Methods required by OpenClaw:
    - run(payload)
    - regenerate(payload)
    - describe()
    """

    def __init__(self):
        self.agent = AgentCore()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Called when the user sends a new message.

        Payload format:
        {
            "message": "user text"
        }
        """
        message = payload.get("message", "")

        if not message:
            return {
                "status": "error",
                "error": "Missing 'message' in payload"
            }

        return self.agent.handle_user_message(message)

    def regenerate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Called when the user requests plan regeneration.
        """
        return self.agent.regenerate(payload)

    def describe(self) -> Dict[str, Any]:
        """
        Metadata for the OpenClaw UI.
        """
        return {
            "name": "Local Coding Agent",
            "description": "A Cursor-style coding agent with LLM-driven planning.",
            "version": "1.0.0",
            "capabilities": ["planning", "file editing", "command execution"],
            "icon": "🛠️"
        }
