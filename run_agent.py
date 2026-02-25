"""
run_agent.py

Convenience script for running the agent outside OpenClaw.
"""

import json
import sys
from agent.core import AgentCore


def main():
    if len(sys.argv) < 2:
        print("Usage: python run_agent.py \"your message\"")
        sys.exit(1)

    message = sys.argv[1]
    agent = AgentCore()

    result = agent.handle_user_message(message)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
