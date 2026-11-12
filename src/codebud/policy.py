"""
codebud/policy.py

The command policy in effect: your policy files, adjusted by the project's
(security review C3, 2026-10-29; design chosen by the user: trust per
project).

Your policy is the first found of the active profile's policy directory,
your config dir and the defaults shipped with codebud (paths.py). A
project's .codebud/ may hold its own harmless_commands.json,
powerful_commands.json and trusted_sequences.json:

- narrowing always applies: a command the project leaves out of both of
  its lists is not allowed, and one it lists as powerful is asked about
  even if you call it harmless;
- widening -- a new command, a powerful command made harmless, a new
  trusted sequence -- applies only once you trust the project. Trust is
  recorded with the SHA-256 of the project's policy files, so any change
  to them, by anyone including codebud's own steps, needs trusting again.

Entries must be plain command words ("git status", "python3"): anything
else is dropped with a warning, from every source, since the lists are
also shown to the model.

refused_arguments.json maps a program to options it may never be given
({"curl": ["-T", "--upload-file"]}). Unlike the lists above, every
layer counts: the active profile's policy directory, your config dir and
the project's .codebud/ are combined, and each can only add to the
refusals built into safety.py -- never remove one. A project needs no
trust for it, since adding refusals only narrows.
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from codebud import paths
from codebud.fsutil import atomic_write_text, private_dir

logger = logging.getLogger(__name__)

HARMLESS_FILE = "harmless_commands.json"
POWERFUL_FILE = "powerful_commands.json"
TRUSTED_SEQ_FILE = "trusted_sequences.json"
POLICY_FILES = (HARMLESS_FILE, POWERFUL_FILE, TRUSTED_SEQ_FILE)
REFUSED_FILE = "refused_arguments.json"
TRUST_FILE = "trusted-projects.json"

_WORDS = re.compile(r"^[A-Za-z0-9._+/-]+( [A-Za-z0-9._+/:=-]+)*$")

# Flagged when a project wants them harmless or adds them: they run
# arbitrary code, reach the network, or delete and rewrite freely.
RISKY = {
    "bash", "sh", "zsh", "dash", "fish", "ksh", "csh", "tcsh",
    "python", "python2", "python3", "perl", "ruby", "node", "deno", "bun", "php", "lua",
    "awk", "gawk", "sed", "env", "xargs", "eval", "exec", "nohup", "timeout", "watch",
    "curl", "wget", "nc", "ncat", "netcat", "socat", "ssh", "scp", "sftp", "rsync", "ftp",
    "telnet", "sudo", "su", "doas", "pkexec", "rm", "dd", "mv", "cp", "chmod", "chown",
    "make", "npm", "npx", "pip", "pip3", "cargo", "go", "docker", "podman", "systemctl",
    "systemd-run", "busctl", "dbus-send", "git push", "git config", "git remote",
}  # fmt: skip


_PROGRAM = re.compile(r"^[A-Za-z0-9._+-]+$")
# -e, -iR, --exec, --vacuum-* (a trailing * means "and anything after")
_OPTION = re.compile(r"^--?[A-Za-z0-9][A-Za-z0-9_-]*\*?$")


def valid_entry(entry) -> bool:
    return isinstance(entry, str) and bool(_WORDS.match(entry))


def risky(entry: str) -> bool:
    words = entry.split()
    return bool(words) and (words[0] in RISKY or " ".join(words[:2]) in RISKY)


def _load(path: Path | None):
    """The JSON list at ``path`` (None if there is none), warning on problems."""
    if path is None:
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring policy file %s: %s", path, exc)
        return []
    if not isinstance(data, list):
        logger.warning("Ignoring policy file %s: not a JSON list", path)
        return []
    return data


def _commands(path: Path | None) -> list[str] | None:
    data = _load(path)
    if data is None:
        return None
    good = [e for e in data if valid_entry(e)]
    for bad in (e for e in data if not valid_entry(e)):
        logger.warning("Ignoring entry %r in %s: not a plain command", bad, path)
    return good


def _sequences(path: Path | None) -> list[dict] | None:
    data = _load(path)
    if data is None:
        return None
    good = []
    for item in data:
        steps = item.get("sequence") if isinstance(item, dict) else None
        name = item.get("name") if isinstance(item, dict) else None
        if (
            isinstance(name, str)
            and valid_entry(name)
            and isinstance(steps, list)
            and all(valid_entry(s) for s in steps)
        ):
            good.append({"name": name, "sequence": steps})
        else:
            logger.warning("Ignoring trusted sequence %r in %s", item, path)
    return good


@dataclass(frozen=True)
class Change:
    """One way a project's policy widens yours."""

    kind: str  # "harmless", "powerful" or "trusted sequence"
    entry: str
    note: str = ""  # "new", "was powerful"

    @property
    def risky(self) -> bool:
        return self.kind != "trusted sequence" and risky(self.entry)

    def __str__(self) -> str:
        return f"{self.kind}: {self.entry}" + (f" ({self.note})" if self.note else "")


@dataclass
class Policy:
    harmless: list[str]
    powerful: list[str]
    sequences: list[dict]
    project: Path | None = None  # the project's .codebud/, if it has policy files
    widening: list["Change"] = field(default_factory=list)  # what trust would add
    trusted: bool = False


def project_hash(project: Path) -> str:
    """SHA-256 over the project's policy files (and their absence)."""
    digest = hashlib.sha256(str(project.resolve()).encode() + b"\0")
    for name in POLICY_FILES:
        path = project / name
        digest.update(name.encode() + b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"\0<absent>\0")
    return digest.hexdigest()


def _trust_path() -> Path:
    return paths.config_dir() / TRUST_FILE


def _trusted() -> dict[str, str]:
    try:
        data = json.loads(_trust_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_trusted(project: Path) -> bool:
    return _trusted().get(str(project.resolve())) == project_hash(project)


def trust(project: Path) -> None:
    """Trust the project's policy files as they are now."""
    records = _trusted()
    records[str(project.resolve())] = project_hash(project)
    private_dir(paths.config_dir())
    atomic_write_text(str(_trust_path()), json.dumps(records, indent=2, sort_keys=True) + "\n")


def revoke(project: Path) -> bool:
    records = _trusted()
    if records.pop(str(project.resolve()), None) is None:
        return False
    atomic_write_text(str(_trust_path()), json.dumps(records, indent=2, sort_keys=True) + "\n")
    return True


def effective(start=None) -> Policy:
    """The policy in effect for the project at ``start`` (default: cwd)."""
    user_h = _commands(paths.user_policy_file(HARMLESS_FILE)) or []
    user_p = _commands(paths.user_policy_file(POWERFUL_FILE)) or []
    user_s = _sequences(paths.user_policy_file(TRUSTED_SEQ_FILE)) or []
    project = paths.project_config_dir(start)
    files = {n: paths.project_policy_file(n, start) for n in POLICY_FILES}
    if project is None or not any(files.values()):
        return Policy(user_h, user_p, user_s)

    proj_h = _commands(files[HARMLESS_FILE])
    proj_p = _commands(files[POWERFUL_FILE])
    proj_s = _sequences(files[TRUSTED_SEQ_FILE])
    # A list the project does not have stays yours.
    proj_h = user_h if proj_h is None else proj_h
    proj_p = user_p if proj_p is None else proj_p
    proj_s = user_s if proj_s is None else proj_s

    widening = [
        Change("harmless", e, "was powerful" if e in user_p else "new")
        for e in proj_h
        if e not in user_h
    ]
    widening += [Change("powerful", e, "new") for e in proj_p if e not in user_h + user_p]
    user_names = {s["name"] for s in user_s}
    widening += [
        Change("trusted sequence", s["name"]) for s in proj_s if s["name"] not in user_names
    ]

    if widening and is_trusted(project):
        return Policy(proj_h, proj_p, proj_s, project, widening, trusted=True)
    # Narrowing only: keep yours, minus what the project leaves out, and
    # ask about what it calls powerful.
    listed = set(proj_h) | set(proj_p)
    harmless = [e for e in user_h if e in proj_h]
    powerful = [e for e in user_p if e in listed] + [e for e in user_h if e in proj_p]
    names = {s["name"] for s in proj_s}
    sequences = [s for s in user_s if s["name"] in names]
    return Policy(harmless, powerful, sequences, project, widening, trusted=not widening)


def _refused_file(path: Path) -> dict[str, set[str]]:
    """The refusals in one refused_arguments.json, bad entries dropped
    with a warning (a damaged file adds nothing; it removes nothing)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring policy file %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("Ignoring policy file %s: not a JSON object", path)
        return {}
    refused = {}
    for program, options in data.items():
        if not (_PROGRAM.match(program) and isinstance(options, list)):
            logger.warning("Ignoring entry %r in %s: not a program and a list", program, path)
            continue
        good = {o for o in options if isinstance(o, str) and _OPTION.match(o)}
        for bad in (o for o in options if o not in good):
            logger.warning("Ignoring option %r for %s in %s: not an option", bad, program, path)
        if good:
            refused[program] = good
    return refused


def refused_arguments(start=None) -> dict[str, set[str]]:
    """Options refused per program by the refused_arguments.json files in
    force for the project at ``start``: the active profile's, yours and the
    project's, combined (safety.py adds its built-in ones)."""
    directories = [paths.PROFILE_POLICY_DIR, paths.config_dir(), paths.project_config_dir(start)]
    combined: dict[str, set[str]] = {}
    for directory in dict.fromkeys(d for d in directories if d is not None):
        path = Path(directory) / REFUSED_FILE
        if path.is_file():
            for program, options in _refused_file(path).items():
                combined.setdefault(program, set()).update(options)
    return combined
