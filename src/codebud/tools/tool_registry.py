"""
codebud/tools/tool_registry.py

This module defines the ToolRegistry class, which stores and manages all
available tools. It provides:

- get(tool_name): return tool instance
- has(tool_name): check if tool exists
- describe_tools(): return descriptions for planner prompt

The registry is used by:
- planner.py
- executor.py
- core.py
"""

from typing import Any

from .commands import CommandTool
from .embeddings import EmbeddingSearchTool
from .file_io import FileReadTool, FileWriteTool
from .host_network import HostNetworkTool
from .patcher import PatchTool
from .web_search import WebSearchTool


class ToolRegistry:
    """
    Registry of all tools available to the agent.

    Tools are stored as:
    {
        "tool_name": ToolClass(...)
    }
    """

    def __init__(self, sandbox):
        self.tools: dict[str, Any] = {}

        # Register tools
        self._register("file_write", FileWriteTool())
        self._register("file_read", FileReadTool())
        self._register("patch", PatchTool())
        self._register("command", CommandTool(sandbox))
        self._register("web_search", WebSearchTool())
        self._register("search_embeddings", EmbeddingSearchTool())
        # Offered only where its profile allows it (observer isolation).
        self._register("host_network", HostNetworkTool())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, name: str):
        """Return a tool instance by name (None if absent or not offered now)."""
        tool = self.tools.get(name)
        return tool if tool is not None and _offered(tool) else None

    def has(self, name: str) -> bool:
        """Check if a tool exists and is offered now."""
        return self.get(name) is not None

    def names(self) -> list[str]:
        """The names of the tools offered now."""
        return [name for name, tool in self.tools.items() if _offered(tool)]

    def describe_tools(self) -> str:
        """Return a structured description of all tools for the planner prompt."""
        lines = []
        for name, tool in self.tools.items():
            if not _offered(tool):
                continue
            desc = getattr(tool, "description", "(no description)")
            hint = getattr(tool, "usage_hint", "")
            lines.append(f"- {name}: {desc}")
            if hint:
                lines.append(f"  When to use: {hint}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _register(self, name: str, tool):
        """Register a tool instance."""
        self.tools[name] = tool


def _offered(tool) -> bool:
    """Whether ``tool`` is offered now: a tool with available() only while
    that says so (host_network: under observer isolation)."""
    available = getattr(tool, "available", None)
    return available is None or bool(available())
