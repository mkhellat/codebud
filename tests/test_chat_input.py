"""
tests/test_chat_input.py

Chat input on a terminal (decisions D2/D3): prompt_toolkit with a live
slash-command menu, a persistent bottom toolbar (mode, model, context) and
Shift-Tab to cycle modes. Plain input() is used when not on a terminal.

The interactive tests drive a real PromptSession through prompt_toolkit's
pipe input, sending the same bytes a terminal would.
"""

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from codebud.cli import chat_input


class _Agent:
    def __init__(self, mode="ask"):
        self.mode = mode

    def get_mode(self):
        return self.mode

    def set_mode(self, mode):
        self.mode = mode
        return True, f"Mode: {mode}"

    def get_model(self):
        return "qwen2.5-coder:3b-instruct-q4_K_M"

    def context_usage(self):
        return 1147, 8192


def _completions(text):
    doc = Document(text, len(text))
    return [
        (c.text, c.display_meta_text)
        for c in chat_input.SlashCompleter().get_completions(doc, None)
    ]


def test_every_slash_command_has_a_description():
    names = [name for name, _ in chat_input.SLASH_COMMANDS]
    assert len(names) == len(set(names))
    expected = {"/help", "/mode", "/model", "/models", "/compact", "/save", "/output"}
    assert expected | {"/exit", "/quit"} <= set(names)
    assert all(desc for _, desc in chat_input.SLASH_COMMANDS)


def test_help_text_lists_every_command():
    text = chat_input.help_text()
    for name, desc in chat_input.SLASH_COMMANDS:
        assert name in text
        assert desc in text


def test_slash_menu_filters_as_you_type():
    names = [name for name, _ in _completions("/mo")]
    assert names == ["/mode", "/model", "/models"]
    meta = dict(_completions("/comp"))
    assert "summarise" in meta["/compact"]


def test_slash_menu_only_for_the_command_word():
    assert _completions("hello") == []
    assert _completions("/mode e") == []


@pytest.mark.parametrize(
    "current,expected",
    [
        ("ask", "auto"),
        ("auto", "full-auto"),
        ("full-auto", "plan"),
        ("plan", "edit"),
        ("edit", "ask"),
    ],
)
def test_next_mode_cycles(current, expected):
    assert chat_input.next_mode(current) == expected


def test_toolbar_shows_mode_model_context_and_hint():
    text = chat_input.toolbar_text("edit", "qwen2.5-coder:3b", 1147, 8192)
    assert "mode: edit" in text
    assert "qwen2.5-coder:3b" in text
    assert "context 14%" in text
    assert "shift-tab" in text


def test_fallback_uses_plain_input_when_not_a_terminal(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "typed")
    reader = chat_input.ChatInput(_Agent(), interactive=False)
    assert reader.read("[1] You: ") == "typed"


def test_interactive_read_returns_the_line():
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        reader = chat_input.ChatInput(_Agent(), interactive=True)
        pipe.send_text("list the files\r")
        assert reader.read("[1] You: ") == "list the files"


def test_shift_tab_cycles_the_mode():
    agent = _Agent("ask")
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        reader = chat_input.ChatInput(agent, interactive=True)
        pipe.send_text("\x1b[Z\x1b[Zhi\r")  # Shift-Tab twice, then a message
        assert reader.read("[1] You: ") == "hi"
    assert agent.mode == "full-auto"  # ask → auto → full-auto


def test_toolbar_is_drawn_on_a_terminal():
    """The toolbar text reaches the screen (a real VT100 renderer, not a mock).

    prompt_toolkit sizes the area below the prompt from the terminal's reply
    to a cursor-position request (ESC [ row ; col R); a real terminal sends
    it automatically, so the test supplies it.
    """
    import io
    import re
    import threading

    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.output.vt100 import Vt100_Output

    screen = io.StringIO()
    output = Vt100_Output(screen, get_size=lambda: Size(rows=30, columns=100), term="xterm")
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        reader = chat_input.ChatInput(_Agent("edit"), interactive=True)
        threading.Timer(0.2, lambda: pipe.send_text("\x1b[1;1R")).start()
        threading.Timer(0.6, lambda: pipe.send_text("hi\r")).start()
        assert reader.read("[1] You: ") == "hi"

    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", screen.getvalue())
    assert "mode: edit" in text
    assert "qwen2.5-coder:3b-instruct-q4_K_M" in text
    assert "context 14%" in text


def test_every_listed_slash_command_has_a_handler_and_vice_versa():
    """/help and the "/" menu come from SLASH_COMMANDS; chat dispatches by
    SLASH_HANDLERS. A command in only one of them would be broken or hidden."""
    from codebud.cli.chat import SLASH_HANDLERS

    assert {name for name, _desc in chat_input.SLASH_COMMANDS} == set(SLASH_HANDLERS)
