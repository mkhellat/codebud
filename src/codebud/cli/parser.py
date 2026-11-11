"""
codebud/cli/parser.py

The command-line grammar: every subcommand and option.
"""

import argparse

from codebud.modes import MODES

KNOWN_SUBCOMMANDS = {
    "run",
    "plan",
    "chat",
    "doctor",
    "history",
    "sessions",
    "index",
    "skill",
    "models",
    "config",
    "profiles",
    "key",
    "integrity",
    "trust",
    "version",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codebud",
        description="Codebud — privacy-first, local coding agent using Ollama.",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Suppress the live progress indicator (useful when piping output).",
    )
    parser.add_argument(
        "--privacy",
        choices=("locked", "private", "trusted"),
        default=None,
        help="What codebud may reach: locked (nothing leaves this machine), private "
        "(default), trusted (unattended: no egress prompts). Overrides config.toml.",
    )
    parser.add_argument(
        "--profile",
        default=None,
        metavar="NAME",
        help="Start with this profile: its mode, privacy level, policy and prompt "
        "(built in: default, unattended; see `codebud profiles`).",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show full step output instead of a one-line summary.",
    )

    # --model is defined per-subcommand (not here at the top level) because
    # argparse silently lets a subparser's same-named default clobber a
    # parent-parser value — defining it once, shared via `parents=`, avoids
    # that trap entirely and keeps `--model` consistently *after* the
    # subcommand, same position as --interactive/--json/--execute.
    model_parent = argparse.ArgumentParser(add_help=False)
    model_parent.add_argument(
        "--model",
        "-m",
        default=None,
        help="Override OLLAMA_MODEL for this invocation (must already be downloaded).",
    )
    model_parent.add_argument(
        "--no-rag",
        action="store_true",
        help="Plan without project context from the index (same as CODEBUD_RAG=0).",
    )

    sub = parser.add_subparsers(dest="subcommand")

    # run
    p_run = sub.add_parser("run", help="Plan and execute a request.", parents=[model_parent])
    p_run.add_argument("message", help="Natural-language request.")
    p_run.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="Prompt for approval before executing each step.",
    )
    p_run.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Run powerful commands (python, pytest, cp, ...) and web searches without asking.",
    )

    # plan
    p_plan = sub.add_parser(
        "plan", help="Plan a request without executing it.", parents=[model_parent]
    )
    p_plan.add_argument("message", help="Natural-language request.")
    p_plan.add_argument("--json", action="store_true", help="Output raw JSON plan.")
    p_plan.add_argument(
        "--execute",
        "-x",
        action="store_true",
        help="Also execute the plan's steps and print their full output (cannot combine with --json).",
    )
    p_plan.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="With --execute: run powerful commands and web searches without asking.",
    )

    # chat
    p_chat = sub.add_parser("chat", help="Interactive multi-turn session.", parents=[model_parent])
    p_chat.add_argument(
        "--mode",
        choices=MODES,
        help="Start in this agent mode (default: ask). Switch later with /mode.",
    )
    p_chat.add_argument(
        "--resume",
        nargs="?",
        const="",
        default=None,
        metavar="SESSION",
        help="Resume a saved session by ID or name (default: the most recent one).",
    )

    # doctor
    sub.add_parser("doctor", help="Check the environment.")

    # history
    p_history = sub.add_parser("history", help="Show past step executions.")
    p_history.add_argument(
        "-n",
        "--limit",
        type=int,
        default=20,
        help="Show at most N most-recent entries (0 = all). Default: 20.",
    )
    p_history.add_argument("--json", action="store_true", help="Output raw JSON entries.")
    p_history.add_argument(
        "--purge", action="store_true", help="Delete the step history (asks first)."
    )
    p_history.add_argument("--yes", "-y", action="store_true", help="Do not ask before --purge.")

    # sessions
    p_sessions = sub.add_parser("sessions", help="List saved chat sessions.")
    p_sessions.add_argument(
        "-n",
        "--limit",
        type=int,
        default=20,
        help="Show at most N most recent sessions (0 = all). Default: 20.",
    )
    p_sessions.add_argument("--json", action="store_true", help="Output raw JSON.")
    deleting = p_sessions.add_mutually_exclusive_group()
    deleting.add_argument("--delete", metavar="SESSION", help="Delete one session (ID or name).")
    deleting.add_argument(
        "--purge", action="store_true", help="Delete all sessions not open in a chat."
    )
    p_sessions.add_argument(
        "--older-than",
        type=int,
        metavar="DAYS",
        default=None,
        help="With --purge: only sessions not used for DAYS days.",
    )
    p_sessions.add_argument("--yes", "-y", action="store_true", help="Do not ask before deleting.")

    # index
    p_index = sub.add_parser("index", help="Build or refresh the project index for search.")
    p_index.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Project directory (default: the git project containing the current directory).",
    )
    action = p_index.add_mutually_exclusive_group()
    action.add_argument("--status", action="store_true", help="Show the index and what changed.")
    action.add_argument(
        "--rebuild", action="store_true", help="Delete the index and build it from scratch."
    )
    action.add_argument("--drop", action="store_true", help="Delete the index.")
    p_index.add_argument("--yes", "-y", action="store_true", help="Do not ask before --drop.")

    # skill
    p_skill = sub.add_parser("skill", help="Show or install codebud's skill for other agents.")
    skill_sub = p_skill.add_subparsers(dest="skill_action", required=True)
    p_show = skill_sub.add_parser("show", help="Print a SKILL.md variant.")
    p_show.add_argument(
        "--for",
        dest="agent",
        choices=["claude", "openclaw", "agensi"],
        default=None,
        help="Which variant: claude/openclaw (default text) or agensi (marketplace hand-off).",
    )
    p_install = skill_sub.add_parser("install", help="Install the SKILL.md for an agent.")
    where = p_install.add_mutually_exclusive_group(required=True)
    where.add_argument(
        "--for",
        dest="agent",
        choices=["claude", "openclaw"],
        help="Install into that agent's personal skills directory.",
    )
    where.add_argument("--dir", help="Install into this directory instead.")
    p_install.add_argument(
        "--force", action="store_true", help="Replace an existing copy that differs."
    )

    # models
    sub.add_parser("models", help="List downloaded Ollama models.")

    # config
    sub.add_parser("config", help="Show resolved configuration.")

    # profiles
    sub.add_parser("profiles", help="List the profiles (built in and your own).")

    # trust
    p_trust = sub.add_parser(
        "trust", help="Review, trust or stop trusting this project's command policy."
    )
    p_trust.add_argument("--yes", "-y", action="store_true", help="Trust it without asking.")
    p_trust.add_argument("--revoke", action="store_true", help="Stop trusting it.")

    # key
    p_key = sub.add_parser("key", help="Manage the encryption key for data at rest.")
    key_sub = p_key.add_subparsers(dest="key_action", required=True)
    key_sub.add_parser("init", help="Create the key, sealed with a new passphrase.")
    key_sub.add_parser("passwd", help="Change the passphrase (the key and data stay).")
    key_sub.add_parser("status", help="Show whether encryption is available and the key exists.")
    p_existing = key_sub.add_parser(
        "encrypt-existing",
        help="Encrypt sessions, history and indexes saved in plain text "
        "(the plaintext is copied aside first, never deleted).",
    )
    p_existing.add_argument("--yes", "-y", action="store_true", help="Do not ask first.")

    # integrity
    p_int = sub.add_parser(
        "integrity", help="Pin the model and tools, and check nothing changed since."
    )
    int_sub = p_int.add_subparsers(dest="integrity_action", required=True)
    p_pin = int_sub.add_parser(
        "pin",
        help="Record the model digests and the hashes of the allow-listed programs.",
    )
    p_pin.add_argument(
        "--model", action="append", metavar="NAME", help="Also pin this model (repeatable)."
    )
    p_pin.add_argument(
        "--command", action="append", metavar="NAME", help="Also pin this program (repeatable)."
    )
    p_pin.add_argument(
        "--yes", "-y", action="store_true", help="Replace a manifest without asking."
    )
    int_sub.add_parser("verify", help="Compare what is installed now with the manifest.")
    int_sub.add_parser("show", help="List what is pinned.")

    # version
    sub.add_parser("version", help="Show version information.")

    return parser
