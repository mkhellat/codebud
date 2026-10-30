"""
codebud/cli/chat.py

`codebud chat`: the interactive session -- the REPL, slash commands,
plan review and step execution, and saving and resuming sessions.
"""

import argparse
import contextlib
import os
import sys
from dataclasses import dataclass
from typing import Any

from codebud import crypto
from codebud.cli import chat_input, common
from codebud.cli.admin import apply_retention, cmd_index
from codebud.cli.display import (
    ProgressIndicator,
    print_assistant_reply,
    print_plan,
    print_rule,
    print_status,
    print_warning,
    visible,
)
from codebud.cli.errors import print_error
from codebud.cli.steps import policy_for_mode, run_steps
from codebud.modes import MODES

# chat warns once the next planner prompt reaches this share of num_ctx.
_CONTEXT_WARN_RATIO = 0.70


def _open_session_to_resume(args):
    """Open the session named by --resume, restoring its model if possible.

    Exits with status 1 if there is no such session or it is open in
    another chat. An explicit --model wins over the session's saved model.
    """
    from codebud import sessions
    from codebud.cli.display import print_warning

    ref = args.resume or sessions.most_recent_available()
    if ref is None:
        print("No saved session to resume. Start one with: codebud chat", file=sys.stderr)
        sys.exit(1)
    try:
        session = sessions.Session.open(ref)
    except sessions.SessionError as exc:
        print(f"Cannot resume: {exc}", file=sys.stderr)
        sys.exit(1)

    saved = session.model
    if saved and not getattr(args, "model", None) and saved != os.environ.get("OLLAMA_MODEL"):
        from codebud.llm_stub import list_models

        if saved in list_models():
            os.environ["OLLAMA_MODEL"] = saved
        else:
            current = os.environ.get("OLLAMA_MODEL", "(not set)")
            print_warning(
                f"The session used '{saved}', which is not downloaded; continuing with {current}."
            )
    return session


class _ChatCheckpoint:
    """Keeps a chat's conversation saved to a session file (codebud/sessions.py).

    The session file is created lazily, the first time there is something
    to save, so a chat that ends without any turns leaves nothing behind.
    """

    def __init__(self, agent, session=None):
        self.agent = agent
        self.session = session
        self._saved = (list(session.history), session.mode) if session else ([], None)

    @property
    def ref(self) -> str:
        """The name to pass to --resume: the session's name, else its ID."""
        return self.session.name or self.session.id

    def save(self, force: bool = False) -> None:
        """Save the history if it changed (or always, with ``force``)."""
        from codebud import sessions

        history = self.agent.get_history()
        mode = self.agent.get_mode()
        if (history, mode) == self._saved and not force:
            return
        if self.session is None:
            if not history and not force:
                return
            self.session = sessions.Session.create()
        from codebud import privacy, profiles

        self.session.save(
            history,
            self.agent.get_model(),
            mode=mode,
            privacy=privacy.level(),
            profile=profiles.active().name,
        )
        self._saved = (history, mode)

    def close(self) -> None:
        if self.session is not None:
            self.session.close()


@dataclass
class ChatState:
    """What a chat keeps between turns (and slash commands may change)."""

    args: Any
    agent: Any
    checkpoint: Any
    hints: Any
    turn: int = 0
    last_plan: dict | None = None
    last_step_output: dict | None = None


def _with_progress(state: ChatState, work):
    """Run ``work(on_chunk)`` with the progress indicator; return its result."""
    prog = ProgressIndicator(no_progress=common.no_progress(state.args))
    prog.start()
    try:
        return work(prog.on_chunk)
    finally:
        prog.stop()


# ---------------------------------------------------------------------------
# Slash commands: handler(state, rest) -- rest is the text after the command.
# chat_input.SLASH_COMMANDS lists them for /help and the "/" menu; a test
# keeps the two in step.
# ---------------------------------------------------------------------------


def _slash_help(state: ChatState, rest: str) -> None:
    print(chat_input.help_text(), file=sys.stderr)


def _slash_clear(state: ChatState, rest: str) -> None:
    state.agent.clear_history()
    state.last_plan = None
    state.turn = 0
    print_status("History cleared.\n")


def _slash_history(state: ChatState, rest: str) -> None:
    history = state.agent.get_history()
    if not history:
        print_status("(no history yet)\n")
        return
    for i, t in enumerate(history, 1):
        print(f"  [{i}] {t['role'].upper()}: {visible(t['content'][:120])}", file=sys.stderr)
    print(file=sys.stderr)


def _slash_undo(state: ChatState, rest: str) -> None:
    if state.agent.undo_last_turn():
        state.turn = max(0, state.turn - 1)
        print_status("Last turn removed.\n")
    else:
        state.turn = 0
        print_status("History cleared (nothing left to undo).\n")


def _slash_mode(state: ChatState, rest: str) -> None:
    if not rest:
        print_status(
            f"Mode: {state.agent.get_mode()}  (choose with /mode <name>: {', '.join(MODES)})\n"
        )
        return
    _ok, msg = state.agent.set_mode(rest)
    print_status(f"{msg}\n")


def _slash_privacy(state: ChatState, rest: str) -> None:
    from codebud import privacy

    if rest:
        wanted = rest.lower()
        if wanted not in privacy.LEVELS:
            print_status(
                f"Unknown privacy level '{wanted}'. Choose one of: {', '.join(privacy.LEVELS)}.\n"
            )
            return
        os.environ["CODEBUD_PRIVACY"] = wanted
        common.announce_privacy()
    print_status(
        f"Privacy: {privacy.level()}  (choose with /privacy <name>: {', '.join(privacy.LEVELS)})\n"
    )


def _slash_profile(state: ChatState, rest: str) -> None:
    from codebud import profiles

    if rest:
        try:
            profile = profiles.get(rest.strip())
        except profiles.ProfileError as exc:
            print_status(f"{exc}\n")
            return
        profiles.activate(profile, "/profile", explicit=True)
        state.agent.reload_policy()  # the profile's policy files
        state.agent.set_mode(profile.mode)
        common.announce_privacy()
    profile = profiles.active()
    print_status(
        f"Profile: {profile.name} ({profile.summary()})  "
        f"(choose with /profile <name>: {', '.join(profiles.available())})\n"
    )


def _slash_save(state: ChatState, rest: str) -> None:
    from codebud import sessions

    checkpoint = state.checkpoint
    try:
        if rest:
            sessions.validate_name(rest)
        checkpoint.save(force=True)
        if rest:
            checkpoint.session.set_name(rest)
    except (sessions.SessionError, OSError) as exc:
        print_status(f"{exc}\n")
        return
    print_status(
        f"Session saved: {checkpoint.ref}  (resume with: codebud chat --resume {checkpoint.ref})\n"
    )


def _slash_compact(state: ChatState, rest: str) -> None:
    _ok, msg = _with_progress(
        state, lambda on_chunk: state.agent.compact_history(on_chunk=on_chunk)
    )
    print_status(f"{msg}\n")


def _slash_doctor(state: ChatState, rest: str) -> None:
    from codebud.cli.doctor import run_doctor

    run_doctor()


def _slash_model(state: ChatState, rest: str) -> None:
    if not rest:
        print_status(f"Active model: {state.agent.get_model() or '(not set)'}\n")
        return
    _ok, msg = _with_progress(
        state, lambda on_chunk: state.agent.set_model(rest, on_chunk=on_chunk)
    )
    print_status(f"{msg}\n")


def _slash_models(state: ChatState, rest: str) -> None:
    from codebud.cli.display import _GREEN, _RESET
    from codebud.llm_stub import list_models

    names = list_models()
    if not names:
        print_status("Cannot reach Ollama to list models.\n")
        return
    active = state.agent.get_model()
    for name in names:
        marker = f"  {_GREEN}<-- active{_RESET}" if name == active else ""
        print(f"  {name}{marker}", file=sys.stderr)
    print(file=sys.stderr)


def _slash_sources(state: ChatState, rest: str) -> None:
    """The chunks the last request was given as project context."""
    hits = state.agent.last_context
    if not hits:
        print_status("(no project context was used for the last request)\n")
        return
    print_status("Project context of the last request:")
    for hit in hits:
        c = hit.chunk
        symbol = f" ({c.symbol})" if c.symbol else ""
        print_status(f"  {c.path}:{c.start_line}-{c.end_line}{symbol}")
    print(file=sys.stderr)


def _slash_rag(state: ChatState, rest: str) -> None:
    """Show or switch project context for this session."""
    from codebud.rag import indexer

    agent = state.agent
    words = rest.split()
    if words:
        if words[0].lower() not in ("on", "off"):
            print_status("Use /rag on or /rag off (or /rag to show the state).\n")
            return
        agent.set_rag(words[0].lower() == "on")
    print_status(f"Project context: {'on' if agent.get_rag() else 'off'}")
    root = indexer.project_root()
    try:
        status = indexer.index_status(root)
    except crypto.CryptoError as exc:
        print_status(f"Index: encrypted, not readable now ({exc})\n")
        return
    if status is None:
        print_status(f"No index for {root}; build one with /index.\n")
        return
    changes = (status["changed"], status["new"], status["deleted"])
    stale = (
        " -- {} changed, {} new, {} deleted since: /index to refresh".format(*changes)
        if any(changes)
        else ""
    )
    print_status(
        f"Index: {common.plural(status['files'], 'file')}, "
        f"{common.plural(status['chunks'], 'chunk')}, "
        f"{status['model']}, updated {status['updated']}{stale}\n"
    )


def _slash_index(state: ChatState, rest: str) -> None:
    """Run `codebud index` without leaving chat."""
    words = rest.split()
    action = words[0].lower() if words else ""
    if action not in ("", "status", "rebuild"):
        print_status("Use /index, /index status or /index rebuild.\n")
        return
    index_args = argparse.Namespace(
        path=None,
        status=action == "status",
        rebuild=action == "rebuild",
        drop=False,
        yes=False,
        no_progress=False,
    )
    # Chat writes everything to stderr, so stdout stays clean for piping.
    with contextlib.redirect_stdout(sys.stderr):
        try:
            cmd_index(index_args)
        except SystemExit:
            pass  # cmd_index already printed the error; the chat goes on
        except crypto.CryptoError as exc:
            print(f"codebud: {exc}", file=sys.stderr)
    print(file=sys.stderr)
    state.hints.refreshed()


def _slash_plan(state: ChatState, rest: str) -> None:
    if state.last_plan:
        print_plan(state.last_plan, verbose=state.args.verbose)
    else:
        print_status("(no plan yet)\n")


def _slash_output(state: ChatState, rest: str) -> None:
    out = state.last_step_output
    if out is None:
        print_status("(no step output yet)\n")
        return
    stdout = visible(out.get("stdout") or "", one_line=False).rstrip()
    if stdout:
        print(stdout, file=sys.stderr)
    else:
        print_status("(last step produced no stdout)")
    print(file=sys.stderr)


SLASH_HANDLERS = {
    "/help": _slash_help,
    "/history": _slash_history,
    "/clear": _slash_clear,
    "/undo": _slash_undo,
    "/compact": _slash_compact,
    "/save": _slash_save,
    "/mode": _slash_mode,
    "/privacy": _slash_privacy,
    "/profile": _slash_profile,
    "/rag": _slash_rag,
    "/sources": _slash_sources,
    "/index": _slash_index,
    "/doctor": _slash_doctor,
    "/model": _slash_model,
    "/models": _slash_models,
    "/plan": _slash_plan,
    "/output": _slash_output,
}


def _handle_slash(state: ChatState, message: str) -> None:
    command, _, rest = message.partition(" ")
    handler = SLASH_HANDLERS.get(command.lower())
    if handler is None:
        print_status(f"Unknown command: {command.lower()}  (try /help)\n")
        return
    handler(state, rest.strip())


# ---------------------------------------------------------------------------
# A request: plan, review, run
# ---------------------------------------------------------------------------


def _report_unusable(plan: dict) -> bool:
    """Print a chat reply or plan error; True if ``plan`` cannot be run."""
    if plan.get("status") == "chat_reply":
        print_assistant_reply(plan.get("reply", ""))
        return True
    if plan.get("status") != "ok":
        print_error(plan.get("error", "unknown error"), label="Plan error")
        return True
    return False


def _handle_request(state: ChatState, message: str) -> None:
    agent = state.agent
    plan = _with_progress(state, lambda on_chunk: agent.handle_user_message(message, on_chunk))
    state.turn += 1
    if _report_unusable(plan):
        return

    state.last_plan = plan
    if agent.get_mode() == "plan":
        print_plan(plan, verbose=state.args.verbose)
        print_status("plan mode: not executed (switch with /mode ask to run plans)\n")
        return

    # Review: run it, revise it (with an optional note), or skip it.
    while True:
        print_plan(plan, verbose=state.args.verbose)
        if agent.get_mode() == "full-auto":
            review = "y"  # unattended: no plan approval
        else:
            try:
                review = input("  Run this plan? [y/N/r(evise)[: note]] ").strip()
            except (EOFError, KeyboardInterrupt):
                print(file=sys.stderr)
                review = "n"

        if review.lower() == "n" or review == "":
            print_status("-- skipped")
            return
        if not review.lower().startswith("r"):
            break  # y / yes / anything else: run it
        feedback = review[1:].lstrip(": ").strip()
        revision = message if not feedback else f"{message} (revision note: {feedback})"
        print(file=sys.stderr)
        plan = _with_progress(
            state, lambda on_chunk, text=revision: agent.handle_user_message(text, on_chunk)
        )
        if _report_unusable(plan):
            return

    outcome = run_steps(
        agent, plan.get("plan", []), policy_for_mode(agent.get_mode()), verbose=state.args.verbose
    )
    if outcome.last_output is not None:
        state.last_step_output = outcome.last_output
    if outcome.changed_files:
        hint = state.hints.after_changes()
        if hint:
            print_status(hint)


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------


def _warn_if_context_is_filling(agent) -> None:
    used, total = agent.context_usage()
    if used >= _CONTEXT_WARN_RATIO * total:
        print_warning(
            f"Context {round(100 * used / total)}% full (~{used:,} of {total:,} tokens). "
            "Use /compact to summarise older turns."
        )


def cmd_chat(args):
    from codebud import sessions
    from codebud.cli.index_hints import IndexHints, rag_indicator
    from codebud.rag.indexer import project_root

    try:
        import readline as _rl  # noqa: F401 — enables arrow-key history and Ctrl+R in input()

        _rl.parse_and_bind("tab: complete")
    except ImportError:
        pass

    if not common.apply_model_override(args):
        sys.exit(1)

    resumed = _open_session_to_resume(args) if args.resume is not None else None
    apply_retention()  # after the resumed session is locked, so it is kept

    agent = common.make_agent()
    if resumed is not None and resumed.mode and not args.profile:
        agent.set_mode(resumed.mode)  # unless --profile chose the mode
    if args.mode:
        agent.set_mode(args.mode)  # an explicit --mode wins over the session's
    print(
        f"Codebud chat  |  model: {agent.get_model() or '(not set)'}  |  "
        f"mode: {agent.get_mode()}  |  /help for commands\n",
        file=sys.stderr,
    )

    root = project_root()
    state = ChatState(
        args=args,
        agent=agent,
        checkpoint=_ChatCheckpoint(agent, resumed),
        hints=IndexHints(agent, root),
    )
    reader = chat_input.ChatInput(agent, rag_state=lambda: rag_indicator(agent, root))
    checked_turn = 0
    if resumed is not None:
        agent.restore_history(resumed.history)
        state.turn = checked_turn = sum(1 for t in resumed.history if t.get("role") == "user")
        print_status(f"Resumed session {state.checkpoint.ref} ({len(resumed.history)} entries).")
        for t in resumed.history[-2:]:
            print_status(f"  {t['role'].upper()}: {t['content'][:120]}")
        print(file=sys.stderr)

    def autosave():
        try:
            state.checkpoint.save()
        except (sessions.SessionError, OSError) as exc:
            print_warning(f"Could not save the session: {exc}")

    start_hint = state.hints.at_start()
    if start_hint:
        print_status(start_hint + "\n")

    try:
        while True:
            autosave()
            # After each completed turn (not after slash commands), warn
            # if the next planner prompt would fill most of the context.
            if state.turn and state.turn != checked_turn:
                checked_turn = state.turn
                _warn_if_context_is_filling(agent)
            print_rule()
            try:
                message = reader.read(f"[{state.turn + 1}] You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print(file=sys.stderr)
                break
            if not message:
                continue  # an accidental Enter must not end the session
            if message.lower() in ("quit", "exit", "q"):
                break
            if message.startswith("/"):
                _handle_slash(state, message)
                continue
            _handle_request(state, message)
            print(file=sys.stderr)
    finally:
        autosave()
        checkpoint = state.checkpoint
        if checkpoint.session is not None:
            print_status(
                f"Session saved: {checkpoint.ref}  "
                f"(resume with: codebud chat --resume {checkpoint.ref})"
            )
        checkpoint.close()
        agent.close()
