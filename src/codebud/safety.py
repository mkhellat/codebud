"""
codebud/safety.py

This module defines the SafetyEngine class, which validates LLM-generated
steps before execution. It enforces:

- the harmless and powerful command lists (whole-word matching)
- arguments that are always refused (find -exec/-delete, python -c, ...)
- the active agent mode (see codebud/modes.py)

It also reports which commands are powerful (they need confirmation in
auto mode and in `codebud run`). Trusted sequences are loaded and shown to
the planner, but not enforced.

The safety engine is consulted by:
- planner.py (during plan validation)
- core.py (final plan validation)
- executor.py (may_execute(), right before each step runs)

It does NOT:
- execute commands
- modify plans
- interact with the LLM
"""

import json
import logging
import os
import re
import shlex
from typing import Any

from . import privacy
from .modes import DEFAULT_MODE, step_allowed

# The policy in effect -- yours, adjusted by the project's -- comes from
# policy.py; the file names stay importable from here.
from .policy import HARMLESS_FILE, POWERFUL_FILE, TRUSTED_SEQ_FILE  # noqa: F401

logger = logging.getLogger(__name__)


def _split(cmd: str) -> list[str]:
    """Split ``cmd`` like the Sandbox does; [] if it cannot be parsed."""
    try:
        return shlex.split(cmd)
    except ValueError:
        return []


def _starts_with_words(words: list[str], entry: str) -> bool:
    """Return whether ``words`` begins with all the words of ``entry``."""
    entry_words = entry.split()
    return bool(entry_words) and words[: len(entry_words)] == entry_words


# Arguments that turn an allowed program into "run anything" or "delete or
# overwrite something", refused regardless of the allow-lists.
_FIND_FORBIDDEN = {
    "-exec",
    "-execdir",
    "-ok",
    "-okdir",
    "-delete",
    "-fprint",
    "-fprint0",
    "-fprintf",
    "-fls",
}
_GIT_FORBIDDEN_PREFIXES = ("--output", "--ext-diff")
_PYTHONS = {"python", "python3"}
_AWKS = {"awk", "gawk", "mawk", "nawk"}
_SEDS = {"sed", "gsed"}
# Python options that take a separate value (the value must not be mistaken
# for the script name when scanning for -c).
_PYTHON_VALUE_OPTIONS = {"-W", "-X"}


def _python_runs_inline_code(args: list[str]) -> bool:
    """Return whether the interpreter options in ``args`` include -c.

    Only the interpreter's own options are checked: they end at the first
    word that is not an option (the script) or after -m, so "-c" passed to
    a script is not refused.
    """
    i = 0
    while i < len(args):
        word = args[i]
        if not word.startswith("-") or word == "-" or word.startswith("--"):
            if word.startswith("--"):
                i += 1
                continue
            return False
        flags = word[1:]
        for pos, flag in enumerate(flags):
            if flag == "c":
                return True
            if flag == "m":
                return False
            if f"-{flag}" in _PYTHON_VALUE_OPTIONS:
                if pos == len(flags) - 1:
                    i += 1  # value is the next word
                break  # rest of this word is the option's value
        i += 1
    return False


def _forbidden_argument(words: list[str]) -> str | None:
    """Return the argument of ``words`` this policy always refuses, if any."""
    program, args = words[0], words[1:]
    if program == "find":
        return next((arg for arg in args if arg in _FIND_FORBIDDEN), None)
    if program in _PYTHONS:
        return "-c" if _python_runs_inline_code(args) else None
    if program == "git":
        return next((arg for arg in args if arg.startswith(_GIT_FORBIDDEN_PREFIXES)), None)
    if program in _AWKS:
        return _awk_forbidden(args)
    if program in _SEDS:
        return _sed_forbidden(args)
    return None


# awk runs programs via system()/print|cmd/cmd|getline, and reads its
# program from a file with -f (so the refused source could hide there).
_AWK_CODE = re.compile(r"\b(system|print|printf)\b.*\||\|\s*getline|\bsystem\s*\(")


def _awk_forbidden(args: list[str]) -> str | None:
    if any(a == "-f" or a == "--file" or a.startswith(("-f", "--file=")) for a in args):
        return "-f (awk program read from a file)"
    program = _first_non_option(args, value_opts={"-v"})
    if program is not None and _AWK_CODE.search(program):
        return "awk program that runs a command (system/getline/pipe)"
    return None


def _sed_forbidden(args: list[str]) -> str | None:
    """A sed that runs a command (e) or writes a file (w, W, s///w), or None.

    A loose regex would refuse ordinary scripts (the `w` in `s/widget/x/`,
    an address like `/warn/`), so the script is parsed at the command
    level: addresses, regexes and replacements are skipped, and only a
    real command letter counts.
    """
    scripts = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-e", "--expression"):
            if i + 1 < len(args):
                scripts.append(args[i + 1])
            i += 2
            continue
        if arg in ("-f", "--file") or arg.startswith(("-f", "--file=")):
            return "-f (sed script read from a file)"
        if arg.startswith(("-e", "--expression=")):
            scripts.append(arg.split("=", 1)[1] if "=" in arg else arg[2:])
        elif not arg.startswith("-"):
            scripts.append(arg)  # the first non-option is the script
            break
        i += 1
    for script in scripts:
        reason = _sed_script_forbidden(script)
        if reason:
            return reason
    return None


def _sed_script_forbidden(script: str) -> str | None:
    i, n = 0, len(script)
    while i < n:
        c = script[i]
        if c in " \t\n;":
            i += 1
            continue
        if c == "#":  # comment to end of line
            while i < n and script[i] != "\n":
                i += 1
            continue
        i = _sed_skip_address(script, i)
        if i >= n:
            break
        cmd = script[i]
        if cmd == "e":
            return "sed 'e' command (runs a shell command)"
        if cmd in "wW":
            return "sed 'w'/'W' command (writes to a file)"
        if cmd in "sy":
            end = _sed_skip_syntax(script, i)
            if cmd == "s" and _sed_s_has_w(script, i, end):
                return "sed s///w flag (writes to a file)"
            i = end
            continue
        if cmd in "aic":  # text commands: rest is their argument
            break
        i += 1
    return None


def _sed_skip_address(script: str, i: int) -> int:
    n = len(script)
    while i < n and (script[i].isdigit() or script[i] in "$+~"):
        i += 1
    while i < n and script[i] in "/\\":
        i = _sed_skip_regex(script, i)
        if i < n and script[i] == ",":
            i += 1
            while i < n and (script[i].isdigit() or script[i] in "$+~"):
                i += 1
        break
    while i < n and script[i] in " \t,!":
        i += 1
    return i


def _sed_skip_regex(script: str, i: int) -> int:
    delim = script[i]
    if delim == "\\" and i + 1 < len(script):
        delim = script[i + 1]
        i += 1
    i += 1
    while i < len(script):
        if script[i] == "\\":
            i += 2
            continue
        if script[i] == delim:
            return i + 1
        i += 1
    return i


def _sed_skip_syntax(script: str, i: int) -> int:
    """Skip a whole s/// or y/// command, returning the index after it."""
    delim = script[i + 1]
    j, seen = i + 2, 0
    while j < len(script) and seen < 2:
        if script[j] == "\\":
            j += 2
            continue
        if script[j] == delim:
            seen += 1
        j += 1
    while j < len(script) and script[j] not in ";\n":  # flags
        j += 1
    return j


def _sed_s_has_w(script: str, i: int, end: int) -> bool:
    """Whether the s/// command between its flags and ``end`` has the w flag."""
    delim = script[i + 1]
    j, seen = i + 2, 0
    while j < end and seen < 2:
        if script[j] == "\\":
            j += 2
            continue
        if script[j] == delim:
            seen += 1
        j += 1
    return "w" in script[j:end]


def _first_non_option(args: list[str], value_opts: set[str]) -> str | None:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in value_opts:
            i += 2
            continue
        if arg.startswith("-") and arg != "-":
            i += 1
            continue
        return arg
    return None


def _file_refusal(tool: str, args: dict[str, Any]) -> str | None:
    """Why a file tool step may not touch its path (confine.py), or None."""
    from codebud import confine
    from codebud.tools.patcher import patch_target

    if tool == "patch":
        path = patch_target(args.get("patch") or "")
    else:
        path = args.get("path")
    if not isinstance(path, str) or not path:
        return None  # a missing argument is the tool registry's to report
    try:
        if tool == "file_read":
            confine.check_read(path)
        else:
            confine.check_write(path)
    except confine.ConfinementError as exc:
        return str(exc)
    return None


# --- catastrophic commands, refused whatever the allow-list says ----------
#
# The allow-list is the main gate, but the user can widen it (their own
# policy, a trusted project, a profile), so these are refused regardless:
# commands that power the machine off, wipe a disk, or recursively delete a
# vital directory. Steps run without a shell, so the words are the parsed
# argv: no glob, ~ or $VAR is ever expanded (security review M5; this
# replaces a raw-substring check that "rm -fr /" walked past).

# Words that run the rest of the line as a command; looked through.
_WRAPPERS = {"sudo", "doas", "env", "nice", "nohup", "ionice", "timeout", "time", "command", "exec"}
_POWER = {"shutdown", "reboot", "poweroff", "halt"}
_SYSTEMCTL_POWER = {"poweroff", "reboot", "halt", "kexec", "soft-reboot"}
# Directories whose recursive deletion is catastrophic.
_VITAL = {
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib32", "/lib64",
    "/opt", "/proc", "/root", "/run", "/sbin", "/srv", "/sys", "/usr", "/var",
    "/Users", "/System", "/Library", "/Applications",
}  # fmt: skip


def _unwrap(words: list[str]) -> list[str]:
    """``words`` with leading wrapper commands (sudo, env X=y, timeout 5, ...)
    and their options removed, so the real command is judged."""
    i = 0
    while i < len(words):
        word = words[i]
        name = word.rsplit("/", 1)[-1]
        if name in _WRAPPERS:
            i += 1
            # the wrapper's own options and arguments: -x, --x, X=y, numbers
            while i < len(words) and (
                words[i].startswith("-") or "=" in words[i] or words[i].replace(".", "").isdigit()
            ):
                i += 1
            continue
        break
    return words[i:]


def _vital(path: str) -> bool:
    """Whether deleting ``path`` recursively would be catastrophic."""
    if not path.startswith("/"):
        return False  # relative: inside the working directory (no ~ expansion)
    # normpath keeps a leading "//" (POSIX leaves it implementation-defined);
    # on the systems codebud runs on it is "/", so collapse it first.
    normal = os.path.normpath("/" + path.lstrip("/"))
    home = os.path.normpath(os.path.expanduser("~"))
    # any user's home directory, not just this one's
    homes = os.path.dirname(normal) in ("/home", "/Users")
    return normal in _VITAL or normal == home or homes


def catastrophic_reason(cmd: str) -> str | None:
    """Why ``cmd`` is refused outright, whatever the policy allows, or None."""
    words = _unwrap(_split(cmd))
    if not words:
        return None
    name = words[0].rsplit("/", 1)[-1]
    args = words[1:]
    if "--no-preserve-root" in args:
        return "--no-preserve-root is never allowed"
    if name in _POWER:
        return f"{name} would power the machine off or restart it"
    if name == "systemctl" and _SYSTEMCTL_POWER & set(args):
        return "systemctl would power the machine off or restart it"
    if name.startswith("mkfs") or name == "wipefs":
        return f"{name} would erase a file system"
    if name == "dd" and any(a.startswith("of=/dev/") for a in args):
        return "dd writing to a device would erase it"
    if name == "rm":
        recursive = any(
            a in ("--recursive", "-r", "-R")
            or (a.startswith("-") and not a.startswith("--") and ("r" in a or "R" in a))
            for a in args
        )
        targets = [a for a in args if not a.startswith("-")]
        vital = next((t for t in targets if _vital(t)), None)
        if recursive and vital:
            return f"rm would recursively delete {vital}"
    return None


POWERFUL_REASON = "Powerful command (runs code or changes files)."


def egress_reason(step: dict[str, Any]) -> str | None:
    """Why ``step`` sends data off the machine, or None.

    Such steps need a confirmation like powerful commands (phase 8, A3),
    except in full-auto mode, where they run unattended (user decision).
    """
    if step.get("tool") == "web_search":
        query = step.get("args", {}).get("query", "")
        return f'Web search: sends "{query}" to DuckDuckGo.'
    args = step.get("args", {})
    if step.get("tool") == "command" and args.get("network"):
        return f'Network: "{args.get("cmd", "")}" may reach the internet.'
    return None


class SafetyEngine:
    """
    Validates steps produced by the LLM planner.

    Responsibilities:
    - Load safety policies
    - Validate each step
    - Provide safety descriptions for planner prompt
    """

    # Active agent mode (see codebud/modes.py); AgentCore.set_mode() updates it.
    mode = DEFAULT_MODE

    def __init__(self):
        self.reload()

    def reload(self) -> None:
        """(Re)read the policy files, e.g. after a profile switch."""
        from . import policy

        effective = policy.effective()
        self.harmless_commands = effective.harmless
        self.powerful_commands = effective.powerful
        self.trusted_sequences = effective.sequences

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate_step(self, step: dict[str, Any]) -> bool:
        """Return whether ``step`` may run (see refusal_reason())."""
        return self.refusal_reason(step) is None

    def refusal_reason(self, step: dict[str, Any]) -> str | None:
        """Return why ``step`` is refused, or None if it may run.

        Checks the agent mode, then, for commands, that the command parses,
        uses no always-refused argument and starts with an allow-list entry.
        (Whether the tool exists is the tool registry's business.)
        """
        tool = step.get("tool")
        if not step_allowed(self.mode, step):
            return f"{tool} is not allowed in {self.mode} mode"
        if tool == "command" and step.get("args", {}).get("network"):
            if privacy.level() == "locked":
                return "steps that need the network are not allowed at privacy level locked"
        if tool == "command":
            return self._command_refusal(step.get("args", {}).get("cmd", ""))
        if tool in ("file_read", "file_write", "patch"):
            return _file_refusal(tool, step.get("args", {}))
        return None

    def is_powerful(self, step: dict[str, Any]) -> bool:
        """Return whether ``step`` is a command on the powerful list.

        Powerful commands run code or change files (python, pytest, cp,
        sed, ...); in auto mode they still need an explicit confirmation.
        """
        if step.get("tool") != "command":
            return False
        words = _split(step.get("args", {}).get("cmd", ""))
        return bool(words) and any(_starts_with_words(words, e) for e in self.powerful_commands)

    def confirmation_reason(self, step: dict[str, Any]) -> str | None:
        """Why ``step`` needs an explicit "y" in auto mode and `run`, or None.

        Powerful commands run code or change files; egress steps (web
        search) send data off the machine.
        """
        if self.is_powerful(step):
            return POWERFUL_REASON
        # At privacy level "trusted" egress runs without asking; safety
        # (powerful commands, the allow-list) is never lifted.
        if not privacy.egress_needs_confirmation():
            return None
        return egress_reason(step)

    def may_execute(self, step: dict[str, Any]) -> tuple[bool, str]:
        """Check, right before execution, that ``step`` may run in this mode.

        Re-validating here means a plan made in one mode cannot run after
        switching to a stricter one. Returns (allowed, reason).
        """
        if self.mode == "plan":
            return False, "plan mode: steps are never executed (switch with /mode ask)"
        reason = self.refusal_reason(step)
        if reason:
            return False, f"refused by the safety policy: {reason}"
        return True, ""

    def describe_rules(self) -> str:
        """
        Return a human-readable description of safety rules for the planner prompt.
        """
        return f"""
- Harmless commands: {self.harmless_commands}
- Powerful commands: {self.powerful_commands}
- Trusted sequences: {self.trusted_sequences}
- Dangerous commands are forbidden.
- Tools must be used responsibly.
"""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _command_refusal(self, cmd: str) -> str | None:
        """
        Judge a shell command against the harmless/powerful lists.

        Commands run without a shell (Sandbox uses shlex.split), so they
        are judged on the same parsed words: a list entry such as "cat" or
        "git log" must match the command's leading words exactly. A mere
        string prefix is not enough, so "cat" does not admit "catastrophe".
        Some arguments are refused even for listed programs (see
        _forbidden_argument), e.g. find -exec and python -c.
        Returns why the command is refused, or None.
        """
        if not cmd.strip():
            return "the command is empty"
        words = _split(cmd)
        if not words:
            return "the command could not be parsed"
        catastrophic = catastrophic_reason(cmd)
        if catastrophic:
            return f"{catastrophic} (refused whatever the allow-list says)"
        forbidden = _forbidden_argument(words)
        if forbidden:
            return f"{words[0]} {forbidden} is never allowed (it can run or delete anything)"
        allowed = (*self.harmless_commands, *self.powerful_commands)
        if not any(_starts_with_words(words, entry) for entry in allowed):
            return (
                f"{words[0]} is not on the allow-list "
                "(harmless_commands.json / powerful_commands.json)"
            )
        return None

    def _load_json(self, path: str) -> list[str]:
        """Load a JSON list from disk; warn and use [] if it is unusable."""
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring policy file %s: %s", path, exc)
            return []
