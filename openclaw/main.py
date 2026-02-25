"""
openclaw/main.py

This module provides a simple CLI entrypoint for running the OpenClaw skill
outside of the UI. It is primarily for debugging and local testing.

Usage:
    python -m openclaw.main "your message"
"""

import sys
import json
from .register import register


def main():
    # Load skills
    skills = register()
    skill = skills["local_coding_agent"]

    # Read message from CLI
    if len(sys.argv) < 2:
        print("Usage: python -m openclaw.main \"your message\"")
        sys.exit(1)

    message = sys.argv[1]

    # Run the skill
    result = skill.run({"message": message})

    # Pretty-print result
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
