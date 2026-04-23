"""
agent/tools/file_io.py

This module defines two tools:

1. FileWriteTool  (tool name: "file_write")
   - Creates or overwrites a file with the given content.

2. FileReadTool   (tool name: "file_read")
   - Reads the content of a file and returns it in stdout.

Both tools follow the Cursor-style tool contract:
{
    "stdout": "...",
    "stderr": "...",
    "returncode": 0
}
"""

import os
from typing import Any


class FileWriteTool:
    """
    Create or overwrite a file.

    Args:
    {
        "path": "path/to/file",
        "content": "file content"
    }
    """

    description = "Create or overwrite a file with the given content."
    usage_hint = (
        "Use when you need to create a new file or replace a file's entire content. "
        'Required args: "path" (string), "content" (string). '
        'Example: {"path": "hello.py", "content": "print(\'Hello, world\')\\n"}'
    )

    def run(self, args: dict[str, Any]) -> dict[str, Any]:
        path = args.get("path")
        content = args.get("content", "")

        if not path:
            return {"stdout": "", "stderr": "Missing required argument: path", "returncode": 1}

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)

            return {
                "stdout": f"Wrote {len(content)} bytes to {path}",
                "stderr": "",
                "returncode": 0,
            }

        except Exception as e:
            return {"stdout": "", "stderr": f"FileWriteTool error: {e}", "returncode": 1}


class FileReadTool:
    """
    Read a file and return its content in stdout.

    Args:
    {
        "path": "path/to/file"
    }
    """

    description = "Read a file and return its content."
    usage_hint = (
        "Use when you need to read the contents of a SPECIFIC named file. "
        'Do NOT use to list directory contents — use "command" with "cmd": "ls" for that. '
        'Required args: "path" (string, must be a file path, not a directory). '
        'Example: {"path": "README.md"}'
    )

    def run(self, args: dict[str, Any]) -> dict[str, Any]:
        path = args.get("path")

        if not path:
            return {"stdout": "", "stderr": "Missing required argument: path", "returncode": 1}

        if not os.path.exists(path):
            return {"stdout": "", "stderr": f"File not found: {path}", "returncode": 1}

        try:
            with open(path) as f:
                content = f.read()

            return {"stdout": content, "stderr": "", "returncode": 0}

        except Exception as e:
            return {"stdout": "", "stderr": f"FileReadTool error: {e}", "returncode": 1}
