"""
codebud/cli/steps.py

Running a plan's steps one by one -- shared by `codebud run`, `codebud
plan --execute` and `codebud chat`, which used to have a copy each.

How much is asked depends on the approval policy:

- "ask"        every step: "Execute step N? [y/N/a(all)/q(quit)]"; "a" runs
               the rest without asking, "q" stops (chat in ask/edit mode,
               run -i)
- "auto"       steps run unprompted, except those with a confirmation
               reason -- powerful commands, web searches -- which ask
               "Run it? [y/N]" (chat in auto mode, run)
- "full-auto"  nothing is asked (chat in full-auto mode, run --yes,
               plan --execute)

The first step that fails stops the run; the caller decides what that
means (run and plan exit 1, chat carries on with the conversation).
"""

import sys
from dataclasses import dataclass, field
from typing import Any

from codebud import privacy
from codebud.cli.display import print_status, print_step_header, print_step_result, print_warning
from codebud.cli.errors import print_error
from codebud.safety import egress_reason

POLICIES = ("ask", "auto", "full-auto")


@dataclass
class StepsOutcome:
    ran: int = 0
    failed: bool = False  # a step failed; the rest did not run
    unconfirmed: int = 0  # needed a "y" but nobody could answer (no terminal)
    changed_files: bool = False  # a step could have changed project files
    last_output: dict[str, Any] | None = None
    outputs: list[dict[str, Any]] = field(default_factory=list)


def policy_for_mode(mode: str) -> str:
    """The approval policy for a chat mode (plan mode never gets here)."""
    return mode if mode in ("auto", "full-auto") else "ask"


def _may_change_files(agent, step: dict) -> bool:
    return step.get("tool") in ("file_write", "patch") or agent.is_powerful(step)


def _ask(prompt: str) -> str | None:
    """The answer, lower-cased; None if nobody can answer (end of input)."""
    try:
        return input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return None


def run_steps(
    agent, steps: list[dict], policy: str, *, verbose: bool = False, full_output: bool = False
) -> StepsOutcome:
    """Approve and execute ``steps`` in order under ``policy``."""
    if policy not in POLICIES:
        raise ValueError(f"unknown approval policy: {policy}")
    outcome = StepsOutcome()
    ask_each = policy == "ask"
    all_approved = False  # "a" in ask mode: the rest run without asking...
    for number, step in enumerate(steps, 1):
        print_step_header(number, step)

        # ...except a step that sends data off the machine, which is still
        # confirmed (unless privacy is trusted), as the docs promise.
        egress = all_approved and privacy.egress_needs_confirmation() and egress_reason(step)
        if egress:
            print_warning(egress)
            answer = _ask("  Send it? [y/N] ")
            if answer is None or not answer.startswith("y"):
                outcome.unconfirmed += answer is None
                print_status("-- skipped")
                continue

        if ask_each:
            answer = _ask(f"  Execute step {number}? [y/N/a(all)/q(quit)] ")
            if answer is None or answer == "q":
                print_status("-- aborted")
                break
            if answer == "a":
                ask_each = False  # this step and the rest run without asking
                all_approved = True
            elif not answer.startswith("y"):
                print_status("-- skipped")
                continue
        elif policy == "auto":
            reason = agent.confirmation_reason(step)
            if reason:
                print_warning(reason)
                answer = _ask("  Run it? [y/N] ")
                if answer is None:
                    outcome.unconfirmed += 1
                    print_status("-- not run: no one to confirm")
                    continue
                if not answer.startswith("y"):
                    print_status("-- skipped")
                    continue

        result = agent.executor.execute_plan({"status": "ok", "plan": [step]})
        outcome.changed_files |= _may_change_files(agent, step)
        if result.get("status") != "ok":
            print_error(str(result), label="Execution error")
            outcome.failed = True
            break
        out = result["results"].get(step["id"], {})
        print_step_result(out, verbose=verbose, full=full_output)
        outcome.ran += 1
        outcome.last_output = out
        outcome.outputs.append(out)
    return outcome
