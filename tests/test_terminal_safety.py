"""
tests/test_terminal_safety.py

Text that is not codebud's own -- model replies, step output, file
contents, error messages -- is shown with its control characters
escaped, never obeyed (security review M2, 2026-10-29). An escape
sequence or carriage return could otherwise move the cursor and rewrite
what the user is approving, and bidi overrides make text read differently
than it is. Newlines, tabs and emoji survive; piped stdout stays raw.
"""

import sys

import pytest

from codebud.cli import common
from codebud.cli.display import print_assistant_reply, print_step_result, visible
from codebud.cli.errors import print_error

HOSTILE = "ok\x1b[2K\rFAKE‮evil\x07"


@pytest.mark.parametrize(
    "raw, shown",
    [
        ("a\x1b[31mb", "a\\x1b[31mb"),
        ("a\rb", "a\\rb"),
        ("a‮b", "a\\u202eb"),  # right-to-left override (Trojan Source)
        ("a\x07b\x7fc", "a\\x07b\\x7fc"),
        ("a\x9bb", "a\\x9bb"),  # C1 control (CSI)
    ],
)
def test_control_characters_are_escaped(raw, shown):
    assert visible(raw) == shown


def test_newlines_tabs_and_emoji_survive():
    assert visible("a\n\tb", one_line=False) == "a\n\tb"
    assert visible("a\r\nb", one_line=False) == "a\nb"  # Windows line ends
    assert visible("a\nb") == "a\\nb"  # one-line mode keeps it on one line
    family = "\U0001f468‍\U0001f469‍\U0001f467"
    assert visible(family) == family


def _stderr_raw(capsys):
    return capsys.readouterr().err


def test_model_replies_are_escaped(capsys):
    print_assistant_reply(HOSTILE)
    err = _stderr_raw(capsys)
    assert "\x1b[2K" not in err and "\r" not in err and "‮" not in err
    assert "\\x1b[2K" in err and "\\u202e" in err


def test_step_output_is_escaped(capsys):
    print_step_result({"stdout": HOSTILE + "\nline two\n", "stderr": HOSTILE, "returncode": 1})
    err = _stderr_raw(capsys)
    assert "\x1b[2K" not in err and "‮" not in err and "\x07" not in err
    assert "line two" in err  # real newlines kept


def test_error_messages_are_escaped(capsys):
    print_error(HOSTILE, label="Plan error")
    err = _stderr_raw(capsys)
    assert "\x1b[2K" not in err and "\\x1b[2K" in err


def test_piped_replies_stay_raw(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert common.reply_text(HOSTILE) == HOSTILE  # a script gets the real text


def test_terminal_replies_are_escaped(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert "\x1b" not in common.reply_text(HOSTILE)
