"""
codebud/cli/chat_input.py

Reading the "[N] You:" line in `codebud chat`.

On a terminal this uses prompt_toolkit (decisions D2/D3):

- typing "/" opens a menu of slash commands, filtered as you type, each
  with a one-line description;
- a persistent bottom toolbar shows the agent mode, model, how full
  the context window is and the project index state (RAG, RAG stale,
  RAG off);
- Shift-Tab cycles the agent mode;
- arrow keys recall earlier lines.

When stdin or stdout is not a terminal (pipes, tests), it falls back to
plain input(), so scripted use behaves exactly as before.
"""

import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings

from codebud.modes import MODES

# Every chat slash command with a one-line description. The single source
# for /help, the "/" menu and the tests.
SLASH_COMMANDS = [
    ("/help", "show this message"),
    ("/history", "show the conversation history kept in memory"),
    ("/clear", "wipe conversation history and start fresh"),
    ("/undo", "remove the last turn from history"),
    ("/compact", "summarise older turns to shrink the conversation history"),
    ("/save", "save the session now; /save <name> also names it for --resume"),
    ("/mode", "show the agent mode, or /mode <ask|auto|full-auto|plan|edit> to switch"),
    ("/profile", "show the profile, or /profile <name> to switch (mode, privacy, policy)"),
    ("/privacy", "show the privacy level, or /privacy <locked|private|trusted> to switch"),
    ("/rag", "show project search state, or /rag <on|off> to use the index or not"),
    ("/sources", "list the project files the last request was given as context"),
    ("/index", "build or refresh the project index; /index status shows what changed"),
    ("/doctor", "run environment health checks"),
    ("/model", "show the active Ollama model, or /model <name> to switch"),
    ("/models", "list downloaded models, marking the active one"),
    ("/plan", "re-show the last plan (if any)"),
    ("/output", "show the full stdout of the last executed step (untruncated)"),
    ("/exit", "end the session (so do /quit, quit, exit, q and Ctrl-D)"),
    ("/quit", "end the session, like /exit"),
]


def help_text() -> str:
    """Return the /help listing built from SLASH_COMMANDS."""
    lines = ["Slash commands (type /cmd and press Enter):"]
    lines += [f"  {name:<10} — {desc}" for name, desc in SLASH_COMMANDS]
    return "\n".join(lines) + "\n"


def next_mode(current: str) -> str:
    """Return the mode after ``current`` in the Shift-Tab cycle."""
    index = MODES.index(current) if current in MODES else -1
    return MODES[(index + 1) % len(MODES)]


def toolbar_text(
    mode: str,
    model: str,
    used: int,
    total: int,
    rag: str = "",
    privacy: str = "",
    profile: str = "",
) -> str:
    """Return the bottom toolbar line; ``rag`` is the index state, if any.

    ``profile`` is shown only when it is not the default one.
    """
    percent = round(100 * used / total) if total else 0
    rag_part = f" · {rag}" if rag else ""
    privacy_part = f" · privacy: {privacy}" if privacy else ""
    profile_part = f"profile: {profile} · " if profile and profile != "default" else ""
    return (
        f" {profile_part}mode: {mode}{privacy_part} · {model or '(no model)'} · "
        f"context {percent}%{rag_part}"
        "   (shift-tab: mode)"
    )


class SlashCompleter(Completer):
    """Completes the slash command being typed as the first word."""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        for name, desc in SLASH_COMMANDS:
            if name.startswith(text):
                yield Completion(name, start_position=-len(text), display_meta=desc)


class ChatInput:
    """Reads chat input, with prompt_toolkit's menu and toolbar on a terminal."""

    def __init__(self, agent, interactive: bool | None = None, rag_state=None):
        self.agent = agent
        if interactive is None:
            interactive = sys.stdin.isatty() and sys.stdout.isatty()
        self._usage = (0, 0)
        # Callable returning the index state for the toolbar (index_hints).
        self._rag_state = rag_state or (lambda: "")
        self._rag = ""
        self._session = self._build_session() if interactive else None

    def read(self, prompt: str) -> str:
        """Read one line; raises EOFError/KeyboardInterrupt like input()."""
        if self._session is None:
            return input(prompt)
        # Context usage only changes between turns, so measure it once here
        # rather than on every toolbar redraw.
        self._usage = self.agent.context_usage()
        self._rag = self._rag_state()
        return self._session.prompt(prompt)

    def _toolbar(self) -> str:
        from urllib.parse import urlsplit

        from codebud.llm_stub import backend, is_remote, ollama_base, openai_model

        # Prompts leaving the machine must stay visible, not just be chosen once.
        model = self.agent.get_model()
        if backend() == "openai":
            model = f"☁ OpenAI {openai_model()}"
        elif is_remote(ollama_base()):
            model = f"{model} @ {urlsplit(ollama_base()).hostname}"
        from codebud import privacy, profiles

        return toolbar_text(
            self.agent.get_mode(),
            model,
            *self._usage,
            self._rag,
            privacy=privacy.level(),
            profile=profiles.active().name,
        )

    def _build_session(self) -> PromptSession:
        bindings = KeyBindings()

        @bindings.add("s-tab")
        def _cycle_mode(event):
            self.agent.set_mode(next_mode(self.agent.get_mode()))
            event.app.invalidate()

        return PromptSession(
            completer=SlashCompleter(),
            complete_while_typing=True,
            bottom_toolbar=self._toolbar,
            key_bindings=bindings,
            history=InMemoryHistory(),
        )
