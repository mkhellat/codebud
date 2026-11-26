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
    python scripts/chat_eval.py --scenario scripts/chat_eval_logs.json MODEL
    python scripts/chat_eval.py --repeat 3 MODEL  # 3 runs: score min/mean/max

The model samples, so one run cannot tell two code versions apart (the
same scenario scored 12/13 and 9/13).  Runs therefore use temperature 0
and seed 42 by default (--temperature, --seed; sent to codebud as
CODEBUD_TEMPERATURE and CODEBUD_SEED), and --repeat N plays the scenario
N times, each in a fresh process and a fresh fixture.

With a proxy in the environment, local Ollama needs
NO_PROXY=127.0.0.1,localhost.  A run takes several minutes per model on a
CPU.

Requirements: the dev environment (make install-dev), numpy, the models.
"""

import argparse
import contextlib
import io
import json
import multiprocessing
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

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
    "text_contains_model",
    "no_unread_claims",
    "no_steps",
    "no_shell_refused",
}

DEFAULT_TURN_TIMEOUT = 300
DEFAULT_TEMPERATURE = 0.0
DEFAULT_SEED = 42

REFUSED = ("plan_refused", "plan_invalid", "plan_error", "llm_unavailable")


# ---------------------------------------------------------------------------
# Checks (pure: no model, no agent)
# ---------------------------------------------------------------------------


def _step_texts(turn: dict) -> list[str]:
    """The target of each step: its path (file tools) or command."""
    return [str(s.get("target", "")) for s in turn.get("steps", [])]


def _unread_claim_ok(rule: dict, text: str, steps: list[str]) -> bool:
    """True when the text claims none of the file's contents, or a step of
    this turn read (or named) the file."""
    if not any(k.lower() in text for k in rule["keywords"]):
        return True
    name = rule["file"].lower()
    base = name.rsplit("/", 1)[-1]
    return any(name in s or base in s for s in steps)


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
    if kind == "text_contains_model":
        family = str(turn.get("model", "")).split(":")[0].lower()
        return bool(arg) == (bool(family and family in text) or "qwen" in text)
    if kind == "no_unread_claims":
        rules = [arg] if isinstance(arg, dict) else arg
        return all(_unread_claim_ok(r, text, steps) for r in rules)
    if kind == "no_steps":
        return (not steps) == bool(arg)
    if kind == "no_shell_refused":
        return ("without a shell" not in text) == bool(arg)
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


def make_fixture(root: Path, files: dict) -> None:
    """Write the fixture.  A value is the file's text, or a dict
    {"repeat": LINE, "times": N, "then": TAIL} for a generated file: LINE
    N times, then TAIL."""
    for name, content in files.items():
        if isinstance(content, dict):
            content = content["repeat"] * int(content["times"]) + content.get("then", "")
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


def _failed_record(model: str, number: int, turn: dict, status: str, seconds: int) -> dict:
    """A record for a turn that did not run to the end: every check failed."""
    names = [f"{k}={json.dumps(a)}" for check in turn["checks"] for k, a in check.items()]
    return {
        "model": model,
        "turn": number,
        "request": turn["request"],
        "status": status,
        "steps": [],
        "final_text": "",
        "seconds": seconds,
        "checks": [{"name": n, "passed": False} for n in names],
    }


def _child_main(
    model: str, scenario: dict, tmp: str, turn_runner, conn, temperature=None, seed=None
) -> None:
    """Child process: play every turn for one model inside the temporary
    directory TMP, sending {"ready": True} and then one record per finished
    turn through CONN.  TURN_RUNNER(agent, request) replaces play_turn (and
    the agent is None) when given: a test hook.  TEMPERATURE and SEED, when
    not None, are set as CODEBUD_TEMPERATURE and CODEBUD_SEED before the
    agent is created."""
    os.environ["OLLAMA_MODEL"] = model
    for name, value in (("CODEBUD_TEMPERATURE", temperature), ("CODEBUD_SEED", seed)):
        if value is not None:
            os.environ[name] = str(value)
    tmp_path = Path(tmp)
    project = tmp_path / "project"
    project.mkdir()
    # Keep the evaluation's sessions, memory and settings away from the
    # user's real ones.
    os.environ["XDG_DATA_HOME"] = str(tmp_path / "xdg-data")
    os.environ["XDG_CONFIG_HOME"] = str(tmp_path / "xdg-config")
    make_fixture(project, scenario["fixture"])
    os.chdir(project)
    agent = None
    try:
        if turn_runner is None:
            from codebud.core import AgentCore

            agent = AgentCore()
            agent.set_mode("full-auto")
            turn_runner = play_turn
        conn.send({"ready": True})
        for number, turn in enumerate(scenario["turns"], 1):
            started = time.monotonic()
            result = turn_runner(agent, turn["request"])
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
            conn.send(record)
    finally:
        if agent is not None:
            agent.close()
        conn.close()


def _stop(proc) -> None:
    """Terminate the child; its pending Ollama request is simply abandoned."""
    proc.terminate()
    proc.join(5)
    if proc.is_alive():
        proc.kill()
        proc.join()


def run_model(
    model: str,
    scenario: dict,
    turn_timeout: float = DEFAULT_TURN_TIMEOUT,
    turn_runner=None,
    progress=None,
    temperature: float | None = None,
    seed: int | None = None,
    run: int = 1,
) -> list[dict]:
    """Play every turn for one model in a child process; return one record
    per turn.  A turn that takes longer than TURN_TIMEOUT seconds is
    abandoned (child terminated) and recorded with status "timeout"; the
    later turns depend on it, so they are recorded as "skipped".  PROGRESS,
    if given, is called with each record as it arrives.  Every record carries
    "run" (RUN, the index of this repetition).  TEMPERATURE and SEED go to
    the child (see _child_main)."""
    turns = scenario["turns"]
    records: list[dict] = []

    def add(record: dict) -> None:
        record["run"] = run
        records.append(record)
        if progress is not None:
            progress(record)

    ctx = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="codebud-chat-eval-") as tmp:
        parent_conn, child_conn = ctx.Pipe(duplex=False)
        proc = ctx.Process(
            target=_child_main,
            args=(model, scenario, tmp, turn_runner, child_conn, temperature, seed),
            daemon=True,
        )
        proc.start()
        child_conn.close()
        failure = None  # status of the turn that did not finish
        try:
            # Starting up (imports, agent) is not a turn; allow it at least a minute.
            if not parent_conn.poll(max(turn_timeout, 60)):
                failure = "timeout"
            else:
                try:
                    parent_conn.recv()
                except EOFError:
                    failure = "error"
            while failure is None and len(records) < len(turns):
                if not parent_conn.poll(turn_timeout):
                    failure = "timeout"
                    break
                try:
                    add(parent_conn.recv())
                except EOFError:  # the child died
                    failure = "error"
        finally:
            _stop(proc)
            parent_conn.close()
    if failure is not None:
        number = len(records) + 1
        seconds = round(turn_timeout) if failure == "timeout" else 0
        add(_failed_record(model, number, turns[number - 1], failure, seconds))
        for number in range(len(records) + 1, len(turns) + 1):
            add(_failed_record(model, number, turns[number - 1], "skipped", 0))
    return records


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("models", nargs="+", help="Ollama models to evaluate")
    parser.add_argument(
        "--scenario",
        type=Path,
        default=SCENARIO,
        help="scenario file (default: scripts/chat_eval.json)",
    )
    parser.add_argument("--json", action="store_true", help="print one JSON line per turn")
    parser.add_argument(
        "--turn-timeout",
        type=float,
        default=DEFAULT_TURN_TIMEOUT,
        metavar="SECONDS",
        help="abandon a turn that takes longer, skip the model's later turns "
        f"(default: {DEFAULT_TURN_TIMEOUT})",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=DEFAULT_TEMPERATURE,
        help=f"sampling temperature sent to the model (default: {DEFAULT_TEMPERATURE:g})",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"sampling seed sent to the model (default: {DEFAULT_SEED})",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        metavar="N",
        help="play each model's scenario N times, each in a fresh process (default: 1)",
    )
    return parser


def _progress(record: dict) -> None:
    """Report a finished turn on stderr (stdout keeps the table / JSON)."""
    print(
        f"[{record['model']}] run {record.get('run', 1)} turn {record['turn']}: "
        f"{record['status']}, {record['seconds']} s",
        file=sys.stderr,
        flush=True,
    )


def score(records: list[dict]) -> tuple[int, int]:
    """(checks passed, checks in all) over the records of one run."""
    return (
        sum(c["passed"] for r in records for c in r["checks"]),
        sum(len(r["checks"]) for r in records),
    )


def summarize(model: str, runs: list[list[dict]]) -> str:
    """One summary line for MODEL from its RUNS (one record list each)."""
    scores = [score(records) for records in runs]
    seconds = sum(r["seconds"] for records in runs for r in records)
    timeouts = sum(r["status"] == "timeout" for records in runs for r in records)
    if len(runs) == 1:
        line = f"{model}: {scores[0][0]}/{scores[0][1]} checks passed, {seconds} s"
    else:
        passed = [p for p, _ in scores]
        line = (
            f"{model}: runs {' '.join(f'{p}/{t}' for p, t in scores)} "
            f"(min {min(passed)}, mean {sum(passed) / len(passed):.1f}, max {max(passed)}), "
            f"{seconds} s"
        )
    if timeouts:
        line += f", {timeouts} turn{'s' if timeouts != 1 else ''} timed out"
    return line


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.repeat < 1:
        build_parser().error("--repeat must be at least 1")
    scenario = load_scenario(args.scenario)

    summary = []
    for model in args.models:
        runs = []
        for run in range(1, args.repeat + 1):
            records = run_model(
                model,
                scenario,
                args.turn_timeout,
                progress=_progress,
                temperature=args.temperature,
                seed=args.seed,
                run=run,
            )
            runs.append(records)
            for rec in records:
                if args.json:
                    print(json.dumps(rec), flush=True)
                    continue
                passed = sum(c["passed"] for c in rec["checks"])
                prefix = f"run {run}  " if args.repeat > 1 else ""
                print(
                    f"{prefix}{rec['turn']:2d}  {str(rec['status']):12s}  "
                    f"{passed}/{len(rec['checks'])}  {rec['seconds']:4d} s  {rec['request'][:50]}"
                )
        summary.append(summarize(model, runs))
    if not args.json:
        print("\n" + "\n".join(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
