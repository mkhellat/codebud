"""
codebud/cli/main.py

Codebud CLI entry point.

Subcommands
-----------
  run "<msg>"     Plan + execute a request (default when no subcommand given)
                  --interactive/-i prompts for approval before each step
                  --yes/-y runs powerful commands and web searches without asking
                  --model/-m <name> overrides OLLAMA_MODEL, validated + warmed first
  plan "<msg>"    Plan only — print the plan without executing
                  --execute/-x also runs the steps and prints full output
                  (cannot combine with --json); like run, powerful steps and
                  web searches ask first unless --yes
                  --model/-m <name> overrides OLLAMA_MODEL, validated + warmed first
  chat            Interactive REPL: multiple requests in one session
                  /rag, /sources and /index work with the project index;
                  --model/-m <name> overrides OLLAMA_MODEL at startup;
                  /model <name> switches mid-session, /models lists available
                  --mode <ask|auto|full-auto|plan|edit> sets the agent mode
                  --resume [SESSION] continues a saved session
  doctor          Check the environment (Ollama, model, policy files, RAM, disk)
  history         Show past step executions (-n/--limit, --json; --purge)
  sessions        List saved chat sessions (-n/--limit, --json;
                  --delete SESSION, --purge [--older-than DAYS], --yes)
  index [PATH]    Build or refresh the project index for search
                  --status, --rebuild, --drop [--yes]
                  (run, plan and chat take --no-rag to plan without it)
  skill           Show or install codebud's skill for other agents
  models          List downloaded Ollama models
  config          Show resolved configuration (env vars + defaults)
  profiles        List profiles (--profile NAME chooses one)
  trust           Review / trust / revoke this project's .codebud/ command policy
  key             Encryption key for data at rest: init, passwd, status,
                  encrypt-existing [--yes]
  version         Print version information

Backwards compatibility: `codebud "some request"` (no subcommand) works
exactly as before — it maps to `run`.
"""

import os
import sys

from codebud.cli import common
from codebud.cli.admin import (
    cmd_config,
    cmd_doctor,
    cmd_history,
    cmd_index,
    cmd_models,
    cmd_profiles,
    cmd_sessions,
    cmd_skill,
    cmd_trust,
    cmd_version,
)
from codebud.cli.chat import cmd_chat
from codebud.cli.key import cmd_key
from codebud.cli.parser import KNOWN_SUBCOMMANDS, build_parser
from codebud.cli.run import cmd_plan, cmd_run


def _activate_profile(flag: str | None) -> bool:
    """Activate the profile --profile, CODEBUD_PROFILE or config.toml names."""
    from codebud import profiles, settings

    name, source = settings.resolve()["CODEBUD_PROFILE"]
    if flag:
        name, source = flag, "--profile"
    try:
        profiles.activate(profiles.get(name), source)
    except profiles.ProfileError as exc:
        print(f"codebud: {exc}", file=sys.stderr)
        return False
    return True


def main():
    # Backwards compatibility: if first arg is not a known subcommand and
    # doesn't start with '-', treat the whole invocation as `codebud run`.
    argv = sys.argv[1:]
    if argv and argv[0] not in KNOWN_SUBCOMMANDS and not argv[0].startswith("-"):
        argv = ["run"] + argv

    # The passphrase must not stay where child processes inherit it.
    from codebud import crypto, settings

    crypto.take_env_passphrase()

    # Values from config.toml become environment defaults; set variables win.
    settings.apply()

    parser = build_parser()
    args = parser.parse_args(argv)
    if not _activate_profile(args.profile):
        sys.exit(2)
    if args.privacy:
        settings.set_value("CODEBUD_PRIVACY", args.privacy, "--privacy")
    if args.subcommand in ("run", "plan", "chat"):
        common.announce_profile()
        common.announce_privacy()
        common.announce_backend()
    if getattr(args, "no_rag", False):
        os.environ["CODEBUD_RAG"] = "0"

    # One-time copy of data written by older versions to the checkout's data/.
    from codebud.paths import data_dir, migrate_legacy_data

    copied = migrate_legacy_data()
    if copied:
        print(
            f"codebud: copied {len(copied)} data file(s) from the old data/ directory to "
            f"{data_dir()}; the originals were left in place.",
            file=sys.stderr,
        )

    dispatch = {
        "run": cmd_run,
        "plan": cmd_plan,
        "chat": cmd_chat,
        "doctor": cmd_doctor,
        "history": cmd_history,
        "sessions": cmd_sessions,
        "index": cmd_index,
        "skill": cmd_skill,
        "models": cmd_models,
        "config": cmd_config,
        "profiles": cmd_profiles,
        "key": cmd_key,
        "trust": cmd_trust,
        "version": cmd_version,
    }

    handler = dispatch.get(args.subcommand)
    if handler is None:
        parser.print_help()
        sys.exit(0)

    try:
        if args.subcommand in _STORES_DATA:
            crypto.check_ready()  # encryption on but unusable: refuse, never store plaintext
        if args.subcommand in ("run", "plan", "chat"):
            common.warn_if_own_code_in_project()
            common.review_project_policy()
            common.unlock_project_index()
        handler(args)
    except crypto.CryptoError as exc:
        print(f"codebud: {exc}", file=sys.stderr)
        sys.exit(1)


# Subcommands that read or write sessions, history or indexes.
_STORES_DATA = {"run", "plan", "chat", "history", "sessions", "index"}


if __name__ == "__main__":
    main()
