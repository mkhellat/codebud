"""
run_agent.py

Codebud CLI entry point.

Subcommands
-----------
  run "<msg>"     Plan + auto-execute a request (default when no subcommand given)
  plan "<msg>"    Plan only — print the plan without executing
  chat            Interactive REPL: multiple requests in one session
  doctor          Check the environment (Ollama, model, config, RAM, disk)
  models          List downloaded Ollama models
  config          Show resolved configuration (env vars + defaults)
  version         Print version information

Backwards compatibility: `codebud "some request"` (no subcommand) works
exactly as before — it maps to `run`.
"""

import argparse
import json
import os
import sys

_KNOWN_SUBCOMMANDS = {"run", "plan", "chat", "doctor", "models", "config", "version"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent():
    from agent.core import AgentCore
    return AgentCore()


def _no_progress(args) -> bool:
    return getattr(args, "no_progress", False) or not sys.stderr.isatty()


# ---------------------------------------------------------------------------
# Subcommand: run
# ---------------------------------------------------------------------------


def cmd_run(args):
    from agent.cli.display import ProgressIndicator, print_plan, print_plan_error, \
        print_step_header, print_step_result
    from agent.cli.errors import print_error

    agent = _make_agent()
    prog = ProgressIndicator(no_progress=_no_progress(args))
    prog.start()
    try:
        plan = agent.handle_user_message(args.message, on_chunk=prog.on_chunk)
    finally:
        prog.stop()

    if plan.get("status") != "ok":
        err = plan.get("error", "unknown error")
        print_plan_error(err)
        print_error(err, label="Plan error")
        sys.exit(1)

    print_plan(plan, verbose=args.verbose)

    steps = plan.get("plan", [])
    for i, step in enumerate(steps, 1):
        print_step_header(i, step)
        result = agent.executor.execute_plan({"status": "ok", "plan": [step]})
        if result.get("status") == "ok":
            out = result["results"].get(step["id"], {})
            print_step_result(out, verbose=args.verbose)
        else:
            err = str(result)
            print_error(err, label="Execution error")
            sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommand: plan (plan only, no execution)
# ---------------------------------------------------------------------------


def cmd_plan(args):
    from agent.cli.display import ProgressIndicator, print_plan, print_plan_error
    from agent.cli.errors import print_error

    agent = _make_agent()
    prog = ProgressIndicator(no_progress=_no_progress(args))
    prog.start()
    try:
        plan = agent.handle_user_message(args.message, on_chunk=prog.on_chunk)
    finally:
        prog.stop()

    if plan.get("status") != "ok":
        err = plan.get("error", "unknown error")
        print_plan_error(err)
        print_error(err, label="Plan error")
        sys.exit(1)

    if args.json:
        print(json.dumps(plan, indent=2))
    else:
        print_plan(plan, verbose=args.verbose)


# ---------------------------------------------------------------------------
# Subcommand: chat (interactive REPL)
# ---------------------------------------------------------------------------


def cmd_chat(args):
    from agent.cli.display import ProgressIndicator, print_plan, print_plan_error, \
        print_step_header, print_step_result
    from agent.cli.errors import print_error

    agent = _make_agent()
    print("Codebud chat — type your request, or 'quit' to exit.\n", file=sys.stderr)

    while True:
        try:
            message = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            break
        if not message or message.lower() in ("quit", "exit", "q"):
            break

        prog = ProgressIndicator(no_progress=_no_progress(args))
        prog.start()
        try:
            plan = agent.handle_user_message(message, on_chunk=prog.on_chunk)
        finally:
            prog.stop()

        if plan.get("status") != "ok":
            err = plan.get("error", "unknown error")
            print_plan_error(err)
            print_error(err, label="Plan error")
            continue

        print_plan(plan, verbose=args.verbose)
        steps = plan.get("plan", [])

        for i, step in enumerate(steps, 1):
            print_step_header(i, step)
            approval = input(f"  Execute step {i}? [y/N/a(all)/q(quit)] ").strip().lower()
            if approval == "q":
                break
            if approval == "a":
                # Auto-execute remaining steps
                for j, s in enumerate(steps[i - 1:], i):
                    print_step_header(j, s)
                    result = agent.executor.execute_plan({"status": "ok", "plan": [s]})
                    if result.get("status") == "ok":
                        out = result["results"].get(s["id"], {})
                        print_step_result(out, verbose=args.verbose)
                    else:
                        print_error(str(result), label="Execution error")
                break
            if not approval.startswith("y"):
                print("  -- skipped", file=sys.stderr)
                continue

            result = agent.executor.execute_plan({"status": "ok", "plan": [step]})
            if result.get("status") == "ok":
                out = result["results"].get(step["id"], {})
                print_step_result(out, verbose=args.verbose)
            else:
                print_error(str(result), label="Execution error")
                break

        print(file=sys.stderr)


# ---------------------------------------------------------------------------
# Subcommand: doctor
# ---------------------------------------------------------------------------


def cmd_doctor(_args):
    from agent.cli.doctor import run_doctor
    sys.exit(run_doctor())


# ---------------------------------------------------------------------------
# Subcommand: models
# ---------------------------------------------------------------------------


def cmd_models(_args):
    import requests as _req

    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    selected = os.environ.get("OLLAMA_MODEL", "")
    try:
        r = _req.get(f"{base}/api/tags", timeout=5)
        r.raise_for_status()
        models = r.json().get("models", [])
    except Exception as exc:
        print(f"Cannot reach Ollama: {exc}", file=sys.stderr)
        print("Start it with: sudo systemctl start ollama", file=sys.stderr)
        sys.exit(1)

    if not models:
        print("No models downloaded. Pull one with:")
        print("  ollama pull qwen2.5-coder:3b-instruct-q4_K_M")
        return

    print(f"{'NAME':<45} {'SIZE':>8}  {'SELECTED'}")
    for m in models:
        name = m.get("name", "")
        size_bytes = m.get("size", 0)
        size_gb = size_bytes / 1024 ** 3
        marker = "<-- active" if name == selected else ""
        print(f"  {name:<43} {size_gb:>6.1f}G  {marker}")


# ---------------------------------------------------------------------------
# Subcommand: config
# ---------------------------------------------------------------------------


def cmd_config(_args):
    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    model = os.environ.get("OLLAMA_MODEL", "(not set)")
    embed = os.environ.get("EMBED_MODEL", model)
    openai_key = "(set)" if os.environ.get("OPENAI_API_KEY") else "(not set)"
    web_search = os.environ.get("WEB_SEARCH_ENABLED", "0")

    print("Codebud resolved configuration:")
    print(f"  OLLAMA_MODEL       = {model}")
    print(f"  OLLAMA_BASE_URL    = {base}")
    print(f"  EMBED_MODEL        = {embed}")
    print(f"  OPENAI_API_KEY     = {openai_key}")
    print(f"  WEB_SEARCH_ENABLED = {web_search}")


# ---------------------------------------------------------------------------
# Subcommand: version
# ---------------------------------------------------------------------------


def cmd_version(_args):
    import requests as _req

    model = os.environ.get("OLLAMA_MODEL", "(not set)")
    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

    # Try to get Ollama version
    try:
        r = _req.get(f"{base}/api/version", timeout=3)
        ollama_ver = r.json().get("version", "unknown") if r.ok else "unreachable"
    except Exception:
        ollama_ver = "unreachable"

    print("codebud  0.1.0")
    print(f"ollama   {ollama_ver}")
    print(f"model    {model}")
    print(f"python   {sys.version.split()[0]}")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codebud",
        description="Codebud — local coding agent powered by Ollama.",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Suppress the live progress indicator (useful when piping output).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show full step output instead of a one-line summary.",
    )

    sub = parser.add_subparsers(dest="subcommand")

    # run
    p_run = sub.add_parser("run", help="Plan and execute a request.")
    p_run.add_argument("message", help="Natural-language request.")

    # plan
    p_plan = sub.add_parser("plan", help="Plan a request without executing it.")
    p_plan.add_argument("message", help="Natural-language request.")
    p_plan.add_argument("--json", action="store_true", help="Output raw JSON plan.")

    # chat
    sub.add_parser("chat", help="Interactive multi-turn session.")

    # doctor
    sub.add_parser("doctor", help="Check the environment.")

    # models
    sub.add_parser("models", help="List downloaded Ollama models.")

    # config
    sub.add_parser("config", help="Show resolved configuration.")

    # version
    sub.add_parser("version", help="Show version information.")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    # Backwards compatibility: if first arg is not a known subcommand and
    # doesn't start with '-', treat the whole invocation as `codebud run`.
    argv = sys.argv[1:]
    if argv and argv[0] not in _KNOWN_SUBCOMMANDS and not argv[0].startswith("-"):
        argv = ["run"] + argv

    parser = _build_parser()
    args = parser.parse_args(argv)

    dispatch = {
        "run": cmd_run,
        "plan": cmd_plan,
        "chat": cmd_chat,
        "doctor": cmd_doctor,
        "models": cmd_models,
        "config": cmd_config,
        "version": cmd_version,
    }

    handler = dispatch.get(args.subcommand)
    if handler is None:
        parser.print_help()
        sys.exit(0)

    handler(args)


if __name__ == "__main__":
    main()
