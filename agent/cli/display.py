"""
agent/cli/display.py

Terminal display helpers: pretty plan list, streaming progress indicator,
step results. All output goes to stderr for progress lines so that stdout
can carry clean JSON when piped. Plain-text fallback when not a TTY.
"""

import sys
import threading
import time
from typing import Any

_IS_TTY = sys.stderr.isatty()

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


def _err(*args, end="\n", flush=False):
    print(*args, end=end, flush=flush, file=sys.stderr)


# ---------------------------------------------------------------------------
# Plan display
# ---------------------------------------------------------------------------


def print_plan(plan: dict[str, Any], *, verbose: bool = False) -> None:
    """Print a human-readable summary of a plan dict."""
    steps: list[dict] = plan.get("plan", [])
    n = len(steps)
    _err(f"\n{_BOLD}Plan ({n} step{'s' if n != 1 else ''}){_RESET}")
    for i, step in enumerate(steps, 1):
        tool = step.get("tool", "?")
        desc = step.get("description", "")
        _err(f"  {_CYAN}[{i}]{_RESET} {_BOLD}{tool:<18}{_RESET} {desc}")
        if verbose:
            args = step.get("args", {})
            for k, v in args.items():
                _err(f"       {_DIM}{k}: {v}{_RESET}")
    _err()


def print_plan_error(error: str) -> None:
    """Print a plan_error with a hint to run doctor."""
    _err(f"\n{_RED}Plan error:{_RESET} {error}")
    _err(f"{_DIM}Tip: run `codebud doctor` to check your environment.{_RESET}\n")


def print_step_header(index: int, step: dict[str, Any]) -> None:
    tool = step.get("tool", "?")
    desc = step.get("description", "")
    _err(f"{_BOLD}Step {index} — {tool}:{_RESET} {desc}")


def print_step_result(result: dict[str, Any], *, verbose: bool = False) -> None:
    """Print a one-line outcome for a step result dict."""
    rc = result.get("returncode", 0)
    stdout = result.get("stdout", "") or ""
    stderr = result.get("stderr", "") or ""
    lines = len(stdout.splitlines())
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
    if verbose and stdout:
        _err(stdout.rstrip())


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
