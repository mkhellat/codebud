"""
codebud/cli/display.py

Terminal display helpers: pretty plan list, streaming progress indicator,
step results. All output goes to stderr for progress lines so that stdout
can carry clean JSON when piped. Plain-text fallback when not a TTY.
"""

import os
import shutil
import sys
import threading
import time
from typing import Any

_IS_TTY = sys.stderr.isatty()
_STDOUT_IS_TTY = sys.stdout.isatty()

# ---------------------------------------------------------------------------
# ANSI helpers (graceful fallback)
# ---------------------------------------------------------------------------

_BOLD = "\033[1m" if _IS_TTY else ""
_DIM = "\033[2m" if _IS_TTY else ""
_GREEN = "\033[32m" if _IS_TTY else ""
_YELLOW = "\033[33m" if _IS_TTY else ""
_RED = "\033[31m" if _IS_TTY else ""
_CYAN = "\033[36m" if _IS_TTY else ""
_RESET = "\033[0m" if _IS_TTY else ""

# Same palette, gated on stdout instead of stderr — for subcommands (history,
# models, config) whose primary output is stdout rather than progress/status.
_OUT_BOLD = "\033[1m" if _STDOUT_IS_TTY else ""
_OUT_DIM = "\033[2m" if _STDOUT_IS_TTY else ""
_OUT_GREEN = "\033[32m" if _STDOUT_IS_TTY else ""
_OUT_RED = "\033[31m" if _STDOUT_IS_TTY else ""
_OUT_CYAN = "\033[36m" if _STDOUT_IS_TTY else ""
_OUT_RESET = "\033[0m" if _STDOUT_IS_TTY else ""


def _err(*args, end="\n", flush=False):
    print(*args, end=end, flush=flush, file=sys.stderr)


def print_status(message: str) -> None:
    """Print a dim, non-error informational status line to stderr.

    Used for inline chat/run feedback like "-- skipped" or "History cleared."
    """
    _err(f"  {_DIM}{message}{_RESET}")


def print_warning(message: str) -> None:
    """Print a yellow warning line to stderr (non-fatal, needs user attention)."""
    _err(f"  {_YELLOW}{message}{_RESET}")


def print_rule() -> None:
    """Print a dim horizontal rule across the terminal to separate chat turns.

    Prints nothing when stderr is not a TTY, so piped output stays clean.
    Falls back to ASCII '-' when stderr's encoding can't represent '─'.
    """
    if not _IS_TTY:
        return
    width = shutil.get_terminal_size().columns
    char = "─"
    try:
        char.encode(sys.stderr.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        char = "-"
    _err(f"{_DIM}{char * width}{_RESET}")


def print_assistant_reply(reply: str) -> None:
    """Print a chat_reply from the model, labeled and colored consistently
    (control characters in it are shown, not obeyed)."""
    _err(f"\n{_CYAN}{_BOLD}Codebud:{_RESET} {visible(reply, one_line=False)}\n", flush=True)


# ---------------------------------------------------------------------------
# Plan display
# ---------------------------------------------------------------------------


# Kept although not "printable": the zero-width joiner holds emoji
# sequences together.
_KEEP_ALWAYS = "\u200d"


def visible(text: str, *, one_line: bool = True) -> str:
    """``text`` with control characters shown as escapes, never obeyed.

    Model replies, step output, file contents and error text are not
    codebud's own: an escape sequence or carriage return in them could
    move the cursor, rewrite what the user is approving or hide text, and
    bidi overrides can make text read differently than it is (security
    review M2). Newlines (unless ``one_line``) and tabs are kept; Windows
    line ends are normalised; everything else non-printable -- C0/C1
    controls, ESC, DEL, bidi and other format characters -- is shown as
    \\x1b, \\r, \\u202e, ...
    """
    text = str(text)
    if not one_line:
        text = text.replace("\r\n", "\n")
    keep = "\t" if one_line else "\t\n"
    return "".join(
        ch if ch.isprintable() or ch in keep or ch in _KEEP_ALWAYS else repr(ch)[1:-1]
        for ch in text
    )


def step_target(step: dict[str, Any]) -> str:
    """What a step really acts on, from its arguments -- never from the
    model's description: the resolved path of a file tool, a patch's target
    file, a command line, a search query."""
    from codebud.tools.patcher import patch_target

    args = step.get("args") or {}
    tool = step.get("tool")
    if tool in ("file_write", "file_read", "patch"):
        path = args.get("path", "") if tool != "patch" else patch_target(args.get("patch") or "")
        if not path:
            return "(no file given)"
        real = os.path.realpath(os.path.expanduser(path))
        shown = real if real == os.path.abspath(path) else f"{path} -> {real}"
        if tool == "file_write":
            shown += f" ({len(args.get('content', '') or '')} bytes)"
        return visible(shown)
    if tool == "command":
        cmd = visible(args.get("cmd", ""))
        return cmd + ("  [network]" if args.get("network") else "")
    if tool == "web_search":
        return visible(f'query: "{args.get("query", "")}"')
    return ""


def print_plan(plan: dict[str, Any], *, verbose: bool = False) -> None:
    """Print a human-readable summary of a plan dict.

    Each step shows what it acts on (step_target()) as well as the model's
    description, which is only the model's account of it.
    """
    steps: list[dict] = plan.get("plan", [])
    n = len(steps)
    _err(f"\n{_BOLD}Plan ({n} step{'s' if n != 1 else ''}){_RESET}")
    for i, step in enumerate(steps, 1):
        tool = step.get("tool", "?")
        desc = visible(step.get("description", ""))
        _err(f"  {_CYAN}[{i}]{_RESET} {_BOLD}{tool:<18}{_RESET} {desc}")
        target = step_target(step)
        if target:
            _err(f"       {target}")
        if verbose:
            args = step.get("args", {})
            for k, v in args.items():
                _err(f"       {_DIM}{k}: {visible(v, one_line=False)}{_RESET}")
    _err()


def print_step_header(index: int, step: dict[str, Any]) -> None:
    """The step about to run: what it acts on (always, even in full-auto)
    and, for a patch, the whole diff."""
    tool = step.get("tool", "?")
    desc = visible(step.get("description", ""))
    _err(f"{_BOLD}Step {index} — {tool}:{_RESET} {desc}")
    target = step_target(step)
    if target:
        _err(f"  {target}")
    if tool == "patch":
        for line in visible(step.get("args", {}).get("patch", ""), one_line=False).splitlines():
            _err(f"  {_DIM}|{_RESET} {line}")


_PREVIEW_LINES = 15


def print_step_result(result: dict[str, Any], *, verbose: bool = False, full: bool = False) -> None:
    """Print a one-line outcome for a step result dict, then stdout.

    By default (chat) only the first _PREVIEW_LINES lines of stdout are shown,
    with a note about how many more lines were withheld (--verbose or /output
    show the rest). Non-interactive callers (run) have no follow-up prompt to
    ask for more, so they pass full=True to always print everything untruncated.
    """
    rc = result.get("returncode", 0)
    # A step's output is whatever the program printed: shown, not obeyed.
    stdout = visible(result.get("stdout", "") or "", one_line=False)
    stderr = visible(result.get("stderr", "") or "")
    stdout_lines = stdout.splitlines()
    lines = len(stdout_lines)
    if rc == 0:
        color = _GREEN
        mark = "ok"
    else:
        color = _RED
        mark = f"exit {rc}"
    summary = f"{color}{mark}{_RESET}"
    if stdout:
        summary += f", {lines} line{'s' if lines != 1 else ''} stdout"
    if stderr:
        summary += f", stderr: {stderr[:80]}"
    _err(f"  -> {summary}")

    if not stdout:
        return
    if full or verbose or lines <= _PREVIEW_LINES:
        _err(stdout.rstrip())
    else:
        _err("\n".join(stdout_lines[:_PREVIEW_LINES]))
        hidden = lines - _PREVIEW_LINES
        _err(
            f"  {_DIM}... (+{hidden} more line{'s' if hidden != 1 else ''}, see /output or --verbose){_RESET}"
        )


# ---------------------------------------------------------------------------
# Streaming progress indicator
# ---------------------------------------------------------------------------


class ProgressIndicator:
    """Shows a live spinner + elapsed time during LLM prefill, then token rate.

    Usage::

        prog = ProgressIndicator()
        prog.start()
        # pass prog.on_chunk to call_llm
        prog.stop()

    on_chunk is called with each text chunk as it arrives. Prefill is
    detected as the period before the first non-empty chunk.
    """

    _SPINNER = "|/-\\"

    def __init__(self, no_progress: bool = False):
        self._no_progress = no_progress or not _IS_TTY
        self._started = 0.0
        self._first_chunk_time: float | None = None
        self._token_count = 0
        self._last_chunk_time = 0.0
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._spinner_idx = 0

    def start(self) -> None:
        if self._no_progress:
            return
        self._started = time.monotonic()
        self._last_chunk_time = self._started
        self._done.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def on_chunk(self, text: str) -> None:
        """Callback — called by _call_ollama with each response fragment."""
        now = time.monotonic()
        if text:
            if self._first_chunk_time is None:
                self._first_chunk_time = now
            self._token_count += 1
            self._last_chunk_time = now

    def stop(self) -> None:
        if self._no_progress:
            return
        self._done.set()
        if self._thread:
            self._thread.join(timeout=1)
        # Print final summary line
        total = time.monotonic() - self._started
        if self._first_chunk_time is not None:
            prefill = self._first_chunk_time - self._started
            gen = total - prefill
            rate = self._token_count / gen if gen > 0 else 0
            _err(
                f"\r{_DIM}prefill: {prefill:.0f}s | "
                f"gen: {gen:.0f}s | "
                f"total: {total:.0f}s | "
                f"tokens: {self._token_count} | "
                f"{rate:.1f} t/s{_RESET}          "
            )
        else:
            _err(f"\r{_DIM}total: {total:.0f}s (no output received){_RESET}          ")

    def _spin(self) -> None:
        while not self._done.wait(timeout=0.2):
            elapsed = time.monotonic() - self._started
            spin_char = self._SPINNER[self._spinner_idx % len(self._SPINNER)]
            self._spinner_idx += 1
            if self._first_chunk_time is None:
                # Prefill phase
                msg = f"\r{spin_char} Thinking... {elapsed:.0f}s (prefill)"
            else:
                # Generation phase
                since_start = time.monotonic() - self._first_chunk_time
                rate = self._token_count / since_start if since_start > 0 else 0
                msg = (
                    f"\r{spin_char} Generating... "
                    f"tokens: {self._token_count} | {rate:.1f} t/s | "
                    f"{elapsed:.0f}s elapsed"
                )
            _err(msg, end="", flush=True)
