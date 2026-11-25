#!/usr/bin/env python3
"""
chat_eval.py -- Play a scripted chat session and check how the model behaves.

Creates a small fixture project (three directories with one text file each)
in a fresh temporary directory, makes it the working directory, and plays
the ten turns of scripts/chat_eval.json in order, in one session (history
carries over), in full-auto mode: each request goes to the agent, a plan is
run, and the turn's final text is checked.  The turns probe what a real
chat showed going wrong: answering from step output (listing, counting,
summarising), not turning questions into plans, not claiming to be another
assistant, remembering the file it just wrote.  A turn's checks are listed
in the JSON file (text_contains_any, text_excludes, step_mentions_any,
file_exists_with, ...); the script reports how many pass.  Steps really
run, inside the temporary project; it is a measurement, so the exit status
is always 0.

Usage (from the repository root, Ollama running):

    python scripts/chat_eval.py qwen2.5-coder:3b-instruct-q4_K_M
    python scripts/chat_eval.py MODEL_A MODEL_B
    python scripts/chat_eval.py --json MODEL      # one JSON line per turn

With a proxy in the environment, local Ollama needs
NO_PROXY=127.0.0.1,localhost.  A run takes several minutes per model on a
CPU.

Requirements: the dev environment (make install-dev), numpy, the models.
"""

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, "src")

SCENARIO = Path(__file__).with_name("chat_eval.json")

KNOWN_CHECKS = {
    "text_not_empty",
    "text_contains_any",
    "text_contains_all",
    "text_excludes",
    "step_mentions_any",
    "step_excludes",
    "no_step_contains",
    "file_exists_with",
    "not_refused",
    "any_of",
}

REFUSED = ("plan_refused", "plan_invalid", "plan_error", "llm_unavailable")


# ---------------------------------------------------------------------------
# Checks (pure: no model, no agent)
# ---------------------------------------------------------------------------


def _step_texts(turn: dict) -> list[str]:
    """The target of each step: its path (file tools) or command."""
    return [str(s.get("target", "")) for s in turn.get("steps", [])]


def _check_one(kind: str, arg, turn: dict, project_dir: Path) -> bool:
    text = str(turn.get("final_text", "")).lower()
    steps = [t.lower() for t in _step_texts(turn)]
    if kind == "text_not_empty":
        return bool(text.strip()) == bool(arg)
    if kind == "text_contains_any":
        return any(w.lower() in text for w in arg)
    if kind == "text_contains_all":
        return all(w.lower() in text for w in arg)
    if kind == "text_excludes":
        return not any(w.lower() in text for w in arg)
    if kind == "step_mentions_any":
        return any(w.lower() in s for w in arg for s in steps)
    if kind == "step_excludes":
        return not any(w.lower() in s for w in arg for s in steps)
    if kind == "no_step_contains":
        return not any(str(arg).lower() in s for s in steps)
    if kind == "not_refused":
        return (turn.get("status") not in REFUSED) == bool(arg)
    if kind == "file_exists_with":
        path = project_dir / arg["path"]
        if not path.is_file():
            return False
        content = path.read_text(errors="replace").lower()
        return all(w.lower() in content for w in arg.get("contains", []))
    if kind == "any_of":
        return any(all(_check_one(k, a, turn, project_dir) for k, a in sub.items()) for sub in arg)
    raise ValueError(f"unknown check: {kind}")


def evaluate(checks: list[dict], turn: dict, project_dir: Path) -> list[tuple[str, bool]]:
    """Run each check against a turn record; return (name, passed) pairs."""
    results = []
    for check in checks:
        for kind, arg in check.items():
            name = f"{kind}={json.dumps(arg)}"
            results.append((name, _check_one(kind, arg, turn, project_dir)))
    return results


def load_scenario(path: Path = SCENARIO) -> dict:
    return json.loads(path.read_text())


def make_fixture(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


# ---------------------------------------------------------------------------
# Playing a turn
# ---------------------------------------------------------------------------


def _final_text(agent, turn_result: dict) -> str:
    """What the user reads as the answer to this turn: a direct reply, the
    answer codebud gives after running steps (when this codebud has
    AgentCore.answer_after_steps; before it, a turn with steps has no
    answer, ""), or the refusal or error."""
    status = turn_result["plan"].get("status")
    if status == "chat_reply":
        return str(turn_result["plan"].get("reply", ""))
    if status == "ok":
        outcome = turn_result.get("outcome")
        answer = getattr(agent, "answer_after_steps", None)
        if answer is None or outcome is None or not (outcome.ran or outcome.failed):
            return ""
        return answer(turn_result["request"], outcome.records)
    return str(turn_result["plan"].get("error", ""))


def _step_record(step: dict) -> dict:
    args = step.get("args", {}) or {}
    tool = step.get("tool")
    target = args.get("cmd") if tool == "command" else args.get("path")
    if target is None:
        target = args.get("cmd") or args.get("path") or ""
    return {"tool": tool, "target": str(target)}


def play_turn(agent, request: str) -> dict:
    """Send one request, run its plan in full-auto; return the raw result."""
    from codebud.cli.steps import run_steps

    plan = agent.handle_user_message(request)
    steps, outcome = [], None
    if plan.get("status") == "ok":
        steps = [_step_record(s) for s in plan.get("plan", [])]
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            outcome = run_steps(agent, plan.get("plan", []), "full-auto")
        for rec, out in zip(steps, outcome.outputs, strict=False):
            rec["returncode"] = out.get("returncode")
            rec["stdout"] = "\n".join(str(out.get("stdout", "")).splitlines()[:3])
    return {"plan": plan, "steps": steps, "outcome": outcome, "request": request}


def run_model(model: str, scenario: dict) -> list[dict]:
    """Play every turn for one model; return one record per turn."""
    os.environ["OLLAMA_MODEL"] = model
    records = []
    with tempfile.TemporaryDirectory(prefix="codebud-chat-eval-") as tmp:
        project = Path(tmp) / "project"
        project.mkdir()
        # Keep the evaluation's sessions, memory and settings away from the
        # user's real ones.
        os.environ["XDG_DATA_HOME"] = str(Path(tmp) / "xdg-data")
        os.environ["XDG_CONFIG_HOME"] = str(Path(tmp) / "xdg-config")
        make_fixture(project, scenario["fixture"])
        previous = os.getcwd()
        os.chdir(project)
        agent = None
        try:
            from codebud.core import AgentCore

            agent = AgentCore()
            agent.set_mode("full-auto")
            for number, turn in enumerate(scenario["turns"], 1):
                started = time.monotonic()
                result = play_turn(agent, turn["request"])
                seconds = time.monotonic() - started
                record = {
                    "model": model,
                    "turn": number,
                    "request": turn["request"],
                    "status": result["plan"].get("status"),
                    "steps": result["steps"],
                    "final_text": _final_text(agent, result),
                    "seconds": round(seconds),
                }
                checks = evaluate(turn["checks"], record, project)
                record["checks"] = [{"name": n, "passed": p} for n, p in checks]
                record["final_text"] = record["final_text"][:500]  # checked in full above
                records.append(record)
        finally:
            if agent is not None:
                agent.close()
            os.chdir(previous)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("models", nargs="+", help="Ollama models to evaluate")
    parser.add_argument("--json", action="store_true", help="print one JSON line per turn")
    args = parser.parse_args()
    scenario = load_scenario()

    summary = []
    for model in args.models:
        records = run_model(model, scenario)
        for rec in records:
            if args.json:
                print(json.dumps(rec), flush=True)
                continue
            passed = sum(c["passed"] for c in rec["checks"])
            print(
                f"{rec['turn']:2d}  {str(rec['status']):12s}  "
                f"{passed}/{len(rec['checks'])}  {rec['seconds']:4d} s  {rec['request'][:50]}"
            )
        total = sum(len(r["checks"]) for r in records)
        passed = sum(c["passed"] for r in records for c in r["checks"])
        seconds = sum(r["seconds"] for r in records)
        summary.append(f"{model}: {passed}/{total} checks passed, {seconds} s")
    if not args.json:
        print("\n" + "\n".join(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
