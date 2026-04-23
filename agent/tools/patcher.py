"""
agent/tools/patcher.py

This module defines the PatchTool, which applies unified diff patches to files.

Tool name: "patch"

Args:
{
    "patch": "unified diff text"
}

The tool:
- Parses the diff
- Applies it line-by-line
- Writes the updated file
- Returns stdout/stderr/returncode

This is a simplified patcher suitable for coding agents.
"""

import os
from typing import Any


class PatchTool:
    """
    Apply a unified diff patch to a file.

    Args:
    {
        "patch": "unified diff text"
    }
    """

    description = "Apply a unified diff patch to a file."
    usage_hint = (
        "Use to make targeted edits to an existing file using a unified diff. "
        "Prefer this over file_write when you only want to change a few lines. "
        'Required args: "patch" (string, a standard unified diff with --- / +++ headers). '
        'Example: {"patch": "--- a/foo.py\\n+++ b/foo.py\\n@@ -1,3 +1,3 @@\\n-old\\n+new\\n"}'
    )

    def run(self, args: dict[str, Any]) -> dict[str, Any]:
        patch_text = args.get("patch")

        if not patch_text:
            return {"stdout": "", "stderr": "Missing required argument: patch", "returncode": 1}

        try:
            result = self._apply_patch(patch_text)
            return {"stdout": result, "stderr": "", "returncode": 0}

        except Exception as e:
            return {"stdout": "", "stderr": f"PatchTool error: {e}", "returncode": 1}

    # ------------------------------------------------------------------
    # Internal patch logic
    # ------------------------------------------------------------------

    def _apply_patch(self, patch_text: str) -> str:
        """
        Apply a unified diff patch.

        This is a minimal patcher:
        - Supports single-file patches
        - Supports @@ hunk headers
        - Applies line additions/removals
        """

        lines = patch_text.split("\n")

        # Extract target file from --- a/file and +++ b/file
        new_file = None

        for line in lines:
            if line.startswith("--- "):
                pass  # source path — unused; target path comes from +++ line
            elif line.startswith("+++ "):
                new_file = line[4:].strip()

        if not new_file:
            raise ValueError("Could not determine target file from patch")

        # Normalize path (strip a/ or b/)
        if new_file.startswith("a/") or new_file.startswith("b/"):
            target_path = new_file[2:]
        else:
            target_path = new_file

        if not os.path.exists(target_path):
            raise FileNotFoundError(f"Target file not found: {target_path}")

        # Load original file
        with open(target_path) as f:
            original_lines = f.readlines()

        patched_lines = []
        j = 0

        # Apply hunks
        for line in lines:
            # Skip diff file headers and hunk markers
            if line.startswith("---") or line.startswith("+++") or line.startswith("@@"):
                continue

            if line.startswith("-"):
                # Remove line — advance source pointer, do not emit
                j += 1
                continue

            if line.startswith("+"):
                # Add line — emit without the leading '+'
                patched_lines.append(line[1:] + "\n")
                continue

            if line.startswith(" "):
                # Context line — copy from original
                patched_lines.append(original_lines[j])
                j += 1
                continue

        # Write patched file
        with open(target_path, "w") as f:
            f.writelines(patched_lines)

        return f"Patch applied to {target_path}"
