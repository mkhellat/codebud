"""
run_agent.py

Convenience script for running the agent outside OpenClaw.

This file also supports a very small interactive/text‑UI mode that
walks the user through each plan step and asks for approval before
execution.  The goal is to provide a simple CLI experience similar to
Claude's `agent` command without requiring a browser.
"""

import argparse
import json
import sys

from agent.core import AgentCore


def _interactive_run(agent: AgentCore, plan_output: dict) -> None:
    """Execute a plan one step at a time with user approval."""

    if plan_output.get("status") != "ok":
        print("Plan error:", plan_output.get("error"))
        return

    for step in plan_output["plan"]:
        print(f"\n{step['id']}: {step['description']} (tool={step['tool']})")
        choice = input("Execute this step? [y/N] ").strip().lower()
        if not choice.startswith("y"):
            print("-- skipped")
            continue

        result = agent.executor.execute_plan({"status": "ok", "plan": [step]})
        if result.get("status") == "ok":
            out = result["results"].get(step["id"], {})
            print(json.dumps(out, indent=2))
        else:
            print("Execution error:", result)
            break


def main():
    parser = argparse.ArgumentParser(
        description="Run the Codebud agent from the command line."
    )
    parser.add_argument(
        "message", help="Natural‑language request to send to the agent"
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Approve and execute each plan step interactively",
    )

    args = parser.parse_args()

    agent = AgentCore()
    plan = agent.handle_user_message(args.message)

    if args.interactive:
        _interactive_run(agent, plan)
    else:
        print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
