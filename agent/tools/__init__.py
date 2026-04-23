"""
Tool implementations used by the Codebud agent.

Each tool follows a common contract:

    {
        "stdout": "...",
        "stderr": "...",
        "returncode": 0
    }

The most important tools are:

- FileWriteTool / FileReadTool: basic file I/O.
- PatchTool: apply unified diff patches.
- CommandTool: execute shell commands via the sandbox.
- WebSearchTool: stubbed web search.
- EmbedTool / EmbeddingSearchTool: simple embedding + vector search.
"""

from .commands import CommandTool
from .embeddings import EmbeddingSearchTool, EmbedTool
from .file_io import FileReadTool, FileWriteTool
from .patcher import PatchTool
from .tool_registry import ToolRegistry
from .web_search import WebSearchTool

__all__ = [
    "FileWriteTool",
    "FileReadTool",
    "PatchTool",
    "CommandTool",
    "WebSearchTool",
    "EmbedTool",
    "EmbeddingSearchTool",
    "ToolRegistry",
]
