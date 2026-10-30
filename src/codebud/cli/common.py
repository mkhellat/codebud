"""
codebud/cli/common.py

Helpers shared by the subcommands: creating the agent, the progress and
--model handling, the start-up notices about privacy and the backend,
and confirming deletions.
Subcommands call them through this module (common.make_agent()), so
tests can replace them in one place.
"""

import os
import sys


def make_agent():
    from codebud import profiles
    from codebud.core import AgentCore

    agent = AgentCore()
    agent.set_mode(profiles.active().mode)
    return agent


def no_progress(args) -> bool:
    return getattr(args, "no_progress", False) or not sys.stderr.isatty()


def apply_model_override(args) -> bool:
    """Handle --model/-m: validate it's downloaded, warm it with live progress,
    then set OLLAMA_MODEL so agent construction picks it up.

    Returns False (after printing an error) if the override was requested but
    invalid, so the caller can abort before spending time on planning.
    """
    requested = getattr(args, "model", None)
    if not requested:
        return True

    from codebud.cli.display import ProgressIndicator
    from codebud.llm_stub import list_models, warm_model_sync

    available = list_models()
    if not available:
        print("Cannot reach Ollama to verify --model.", file=sys.stderr)
        return False
    if requested not in available:
        print(
            f"Model '{requested}' is not downloaded. Run: ollama pull {requested}", file=sys.stderr
        )
        return False

    prog = ProgressIndicator(no_progress=no_progress(args))
    prog.start()
    try:
        ok = warm_model_sync(requested, on_chunk=prog.on_chunk)
    finally:
        prog.stop()
    if not ok:
        print(f"Failed to warm up '{requested}'.", file=sys.stderr)
        return False

    os.environ["OLLAMA_MODEL"] = requested
    return True


def announce_profile() -> None:
    """Say on stderr which profile is active, when it is not the default."""
    from codebud import profiles
    from codebud.cli.display import print_status

    profile = profiles.active()
    if profile.name != profiles.DEFAULT:
        print_status(f"Profile: {profile.name} ({profile.summary()})")


def unattended(args) -> bool:
    """Whether run / plan --execute should ask nothing: --yes, or a
    full-auto profile (unless -i asks for every step)."""
    from codebud import profiles

    if getattr(args, "interactive", False):
        return False
    return args.yes or profiles.active().mode == "full-auto"


def announce_privacy() -> None:
    """Say on stderr when the privacy level is not the default."""
    from codebud import privacy
    from codebud.cli.display import print_status, print_warning

    current = privacy.level()
    if current == "trusted":
        print_warning(
            "Privacy: trusted -- web searches (and, where isolation is available, "
            "steps) run without asking and not isolated; the allow-list still applies."
        )
    elif current == "locked":
        print_status("Privacy: locked -- nothing leaves this machine; web search is off.")


def announce_backend() -> None:
    """Say once, on stderr, when prompts are going to leave this machine."""
    from urllib.parse import urlsplit

    from codebud import privacy
    from codebud.cli.display import print_warning
    from codebud.llm_stub import (
        backend,
        backend_description,
        is_remote,
        ollama_base,
        remote_http_refusal,
    )

    if privacy.level() == "locked":
        return  # nothing leaves; a backend off this machine gets its own error
    if backend() == "openai":
        print_warning(f"LLM backend: {backend_description()} (CODEBUD_BACKEND=openai).")
    elif is_remote(ollama_base()) and not remote_http_refusal(ollama_base()):
        # (A refused plain-http server gets its own error, not this notice.)
        host = urlsplit(ollama_base()).hostname
        print_warning(f"LLM backend: {backend_description()}: prompts are sent to {host}.")


def unlock_project_index() -> None:
    """Before planning: unlock the key if this project's index is encrypted.

    Retrieval never asks for the passphrase (it runs under the progress
    display), so run, plan and chat ask here, once, before anything else.
    If the key cannot be unlocked, project context is turned off for this
    run, with a notice, instead of failing.
    """
    from codebud import crypto
    from codebud.cli.display import print_warning
    from codebud.rag import context, indexer
    from codebud.rag.store import is_sealed

    if not context.enabled() or crypto.is_unlocked():
        return
    if not is_sealed(indexer.project_root()):
        return
    try:
        crypto.ensure_unlocked()
    except crypto.CryptoError as exc:
        print_warning(f"Project context off: the project index is encrypted ({exc}).")
        os.environ["CODEBUD_RAG"] = "0"


def reply_text(reply: str) -> str:
    """A model reply for stdout: control characters shown as escapes on a
    terminal, left as they are when piped (a script gets the raw text)."""
    if not sys.stdout.isatty():
        return reply
    from codebud.cli.display import visible

    return visible(reply, one_line=False)


def show_widening(effective) -> None:
    """List how a project's policy would widen yours, flagging risky entries."""
    from codebud.cli.display import print_status

    for change in effective.widening:
        flag = "   <- can run code, reach the network or delete" if change.risky else ""
        print_status(f"    {change}{flag}")


def review_project_policy() -> None:
    """At startup: ask once whether to trust a project policy that widens yours.

    Narrowing always applies without asking. Without a terminal the
    widening is ignored, with a notice (security review C3, 2026-10-29).
    """
    from codebud import policy
    from codebud.cli.display import print_status, print_warning

    effective = policy.effective()
    if effective.project is None or effective.trusted:
        return
    print_warning(f"This project's command policy ({effective.project}) widens yours:")
    show_widening(effective)
    if not sys.stdin.isatty():
        print_status(
            "  Ignored (no terminal to ask): only its narrowing applies. Review it with: codebud trust"
        )
        return
    try:
        answer = input("  Trust this project's policy? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.startswith("y"):
        policy.trust(effective.project)
        print_status("  Trusted. Codebud asks again if its policy files change.")
    else:
        print_status("  Not trusted: only its narrowing applies (codebud trust to change that).")


def confirm_deletion(paths: list[str], question: str, yes: bool) -> bool:
    """List what would be deleted and ask; --yes skips the question.

    Without a terminal and without --yes nothing is deleted: codebud exits
    with status 1 (deleting user data always needs a deliberate yes).
    """
    for path in paths:
        print(f"  {path}")
    if yes:
        return True
    if not sys.stdin.isatty():
        print("codebud: not deleting without confirmation; re-run with --yes.", file=sys.stderr)
        sys.exit(1)
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")
