"""
codebud/paths.py

Where codebud keeps its files (decision D5b). Nothing here depends on the
directory codebud is started from.

- Runtime data (memory, sessions, project indexes):
      $XDG_DATA_HOME/codebud            (default ~/.local/share/codebud)
- Policy files (harmless/powerful commands, trusted sequences): yours, first
  found of
      the active profile's policy directory, if it has one (profiles.py)
      $XDG_CONFIG_HOME/codebud          (default ~/.config/codebud)
      the defaults shipped with codebud
  then adjusted by the project's .codebud/ (policy.py): narrowed always,
  widened only once you trust that project
- Data written by older versions to the checkout's data/ directory is copied
  to the data directory once (migrate_legacy_data); the originals are kept.
"""

import os
import shutil
from pathlib import Path

from codebud.fsutil import private_dir

APP = "codebud"
PROJECT_DIR_NAME = ".codebud"

_PACKAGE_DIR = Path(__file__).resolve().parent

# Default policy files shipped inside the package (package data).
PACKAGED_POLICY_DIR = _PACKAGE_DIR / "data" / "policy"

# The source checkout, when running from one (src/codebud/ -> repo root).
_SOURCE_ROOT = _PACKAGE_DIR.parent.parent

# Override for where versions before 2026-10-25 kept runtime data; see
# legacy_data_dir(). Tests set it (and CODEBUD_LEGACY_DATA_DIR) to an empty
# temporary directory.
LEGACY_DATA_DIR: Path | None = None

# The active profile's policy directory (set by profiles.activate()).
PROFILE_POLICY_DIR: Path | None = None


def _xdg(var: str, fallback: str) -> Path:
    """$``var``/codebud, or ~/``fallback``/codebud.

    A relative value is ignored, as the XDG Base Directory spec requires:
    honouring it would put codebud's data -- or its keys -- inside whatever
    directory it was started from (security review, low).
    """
    base = os.environ.get(var)
    if base and not os.path.isabs(base):
        _warn_once(f"Ignoring {var}={base!r}: it must be an absolute path")
        base = None
    return Path(base or os.path.join(os.path.expanduser("~"), fallback)) / APP


def data_dir() -> Path:
    """Directory for runtime data (memory, sessions, embeddings)."""
    return _xdg("XDG_DATA_HOME", os.path.join(".local", "share"))


def config_dir() -> Path:
    """Directory for the user's own policy files."""
    return _xdg("XDG_CONFIG_HOME", ".config")


def git_top(start: Path | str | None = None) -> Path | None:
    """The git work tree containing ``start`` (default: cwd), by looking for
    .git -- without running git, which follows the repository's config."""
    here = Path(start or os.getcwd()).resolve()
    for directory in (here, *here.parents):
        if (directory / ".git").exists():
            return directory
    return None


def project_config_dir(start: Path | str | None = None) -> Path | None:
    """The project's .codebud/ directory, or None.

    Looked for from ``start`` (default: cwd) up to the top of its git work
    tree -- or, outside git, in ``start`` only -- and never in $HOME or /
    (~/.codebud is not a project's). A .codebud/ owned by someone else is
    ignored with a warning: in a shared directory such as /tmp another
    user could plant one (security review C3, 2026-10-29).
    """
    here = Path(start or os.getcwd()).resolve()
    top = git_top(here) or here
    home = Path.home().resolve()
    for directory in (here, *here.parents):
        if directory == home or directory == Path(directory.anchor):
            break
        candidate = directory / PROJECT_DIR_NAME
        if candidate.is_dir():
            if candidate.stat().st_uid != os.getuid():
                _warn_once(f"Ignoring {candidate}: it belongs to another user")
                return None
            return candidate
        if directory == top:
            break
    return None


_warned: set[str] = set()


def _warn_once(message: str) -> None:
    import logging

    if message not in _warned:
        _warned.add(message)
        logging.getLogger(__name__).warning(message)


def user_policy_file(name: str) -> Path:
    """Your policy file ``name``: the active profile's, your config dir's, or
    the one shipped with codebud. A project's never comes in here -- it only
    adjusts this one (policy.py)."""
    for directory in (PROFILE_POLICY_DIR, config_dir()):
        if directory is not None and (directory / name).is_file():
            return directory / name
    return PACKAGED_POLICY_DIR / name


def project_policy_file(name: str, start: Path | str | None = None) -> Path | None:
    """The project's policy file ``name`` in its .codebud/, if it has one."""
    project = project_config_dir(start)
    if project is not None and (project / name).is_file():
        return project / name
    return None


def legacy_data_dir() -> Path:
    """Where older versions kept runtime data: the checkout's data/.

    Overridable with LEGACY_DATA_DIR or the CODEBUD_LEGACY_DATA_DIR
    environment variable (which child processes inherit).
    """
    if LEGACY_DATA_DIR is not None:
        return Path(LEGACY_DATA_DIR)
    if os.environ.get("CODEBUD_LEGACY_DATA_DIR"):
        return Path(os.environ["CODEBUD_LEGACY_DATA_DIR"])
    return _SOURCE_ROOT / "data"


def migrate_legacy_data() -> list[str]:
    """Copy files from the old data/ directory into data_dir(), once.

    The originals are never deleted: user data must survive even if the
    copy lands somewhere unexpected. A file is copied only if nothing exists
    at its new location, so newer data is never overwritten. Returns the
    new paths of copied files.

    Symbolic links are never followed (a link in the old directory could
    point anywhere). With encryption at rest on, the copies are written the
    way codebud writes such data: sessions encrypted, history lines sealed,
    an old entries.json array converted into sealed entries.jsonl lines; if
    encryption is on but cannot work yet (no key), nothing is copied this
    time rather than copied in plain text (security review, low).
    """
    from codebud import crypto

    legacy = legacy_data_dir()
    if not legacy.is_dir() or legacy.resolve() == data_dir().resolve():
        return []
    sealing = crypto.enabled()
    if sealing:
        try:
            crypto.check_ready()
        except crypto.CryptoError:
            return []  # the CLI reports why; the originals wait for next time
    root = legacy.resolve()
    copied = []
    for source in sorted(legacy.rglob("*")):
        if source.is_symlink() or not source.is_file() or source.suffix in (".lock", ".tmp"):
            continue
        if root not in source.resolve().parents:
            continue  # reached through a linked directory
        relative = source.relative_to(legacy)
        target = data_dir() / relative
        if sealing and relative.parts[0] == "memory" and source.name == "entries.json":
            target = target.with_suffix(".jsonl")  # converted, never copied as is
        if target.exists():
            continue
        private_dir(target.parent)
        if sealing:
            try:
                _copy_sealed(source, target)
            except (ValueError, OSError):
                continue  # unreadable (e.g. a damaged entries.json): the original stays
        else:
            shutil.copy2(source, target, follow_symlinks=False)
        os.chmod(target, 0o600)  # the copy only; the original is left as it was
        copied.append(str(target))
    return copied


def _copy_sealed(source: Path, target: Path) -> None:
    """Copy ``source`` to ``target`` encrypted the way codebud stores it."""
    import json

    from codebud import crypto
    from codebud.fsutil import atomic_write_bytes

    raw = source.read_bytes()
    if source.name == "entries.json":  # the pre-JSONL history: a JSON array
        entries = json.loads(raw)
        text = "".join(crypto.seal_line(json.dumps(e)) + "\n" for e in entries)
        atomic_write_bytes(target, text.encode())
    elif target.suffix == ".jsonl":
        lines = raw.decode().splitlines()
        sealed = [
            line
            if not line.strip() or crypto.is_encrypted_line(line)
            else crypto.encrypt_line(line)
            for line in lines
        ]
        atomic_write_bytes(target, "".join(line + "\n" for line in sealed).encode())
    elif target.parent.name == "sessions" and target.suffix == ".json":
        atomic_write_bytes(target, raw if crypto.is_encrypted(raw) else crypto.encrypt(raw))
    else:
        shutil.copy2(source, target, follow_symlinks=False)
