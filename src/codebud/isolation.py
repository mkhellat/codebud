"""
codebud/isolation.py

Isolated command steps (phase 8, Part C). POSIX has no API for this, so it
is per platform; on Linux codebud uses bubblewrap (bwrap, as Flatpak
does), unprivileged:

- a new user, pid, ipc, uts and network namespace (--unshare-all): no
  network unless the step declared it needs one;
- the file system read-only (--ro-bind / /), except the project
  directory, which stays writable, and a fresh /tmp;
- the user's secrets hidden: ~/.ssh, ~/.gnupg, cloud and container
  credentials, keyrings, .netrc and similar files, and codebud's own data,
  configuration (the encryption key) and runtime directory (decrypted index
  copies) -- wherever $XDG_* puts them;
- the user's whole runtime directory (/run/user/<uid>: session D-Bus,
  systemd, keyring, gpg and SSH agents) and the system bus (/run/dbus);
- killed with codebud (--die-with-parent), in its own session.

Without bwrap, `unshare --user --net` (util-linux) still cuts the network,
but nothing else. Where neither works (macOS, BSD, containers, blocked
user namespaces), steps are not isolated: privacy level "private" warns
once and runs them, "locked" refuses them. "trusted" never isolates.
"""

import logging
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# Relative to $HOME. Directories are covered by an empty tmpfs, files by
# /dev/null; only the ones that exist are hidden.
HIDDEN_DIRS = (
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".kube",
    ".docker",
    ".password-store",
    ".config/gcloud",
    ".config/gh",
    ".local/share/keyrings",
)
HIDDEN_FILES = (".netrc", ".pgpass", ".pypirc", ".npmrc", ".git-credentials")

_warned = False
_warned_unshare_network = False


@lru_cache(maxsize=1)
def _probe() -> str | None:
    """Which isolation actually works here (bwrap may exist but be blocked)."""
    if shutil.which("bwrap"):
        ok = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "--unshare-all", "--die-with-parent", "true"],
            capture_output=True,
            timeout=10,
        )
        if ok.returncode == 0:
            return "bwrap"
    if shutil.which("unshare"):
        ok = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--net", "true"],
            capture_output=True,
            timeout=10,
        )
        if ok.returncode == 0:
            return "unshare"
    return None


def available() -> str | None:
    """ "bwrap", "unshare" (network only) or None."""
    try:
        return _probe()
    except (OSError, subprocess.SubprocessError):
        return None


def describe(kind: str | None) -> str:
    return {
        "bwrap": "bubblewrap: no network, project-only writes, secrets hidden",
        "unshare": "unshare: no network (files not protected; install bubblewrap)",
        None: "none: steps are not isolated",
    }[kind]


def codebud_dirs() -> list[Path]:
    """Where codebud keeps what steps must not read, wherever XDG puts it:
    its data, its configuration (the encryption key), and the runtime
    directory holding decrypted index copies."""
    from codebud import paths

    found = [paths.data_dir(), paths.config_dir()]
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        found.append(Path(runtime) / "codebud")
    return found


def runtime_dirs() -> list[Path]:
    """The user's runtime directory and the system bus, hidden whole.

    /run/user/<uid> holds sockets a step could use to act outside the
    sandbox: the session D-Bus (and through it systemd, which can start
    programs outside it), the keyring, gpg-agent and the SSH agent;
    /run/dbus the system bus (polkit lets local sessions do a lot). A
    read-only bind does not help -- connecting to a socket is not a write
    (security review C2, 2026-10-29).
    """
    found = []
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        found.append(Path(runtime))
    found += [Path(f"/run/user/{os.getuid()}"), Path("/run/dbus")]
    return found


def hidden_dirs(home: Path) -> list[Path]:
    """Every directory a bwrap step gets an empty tmpfs over, outermost only."""
    candidates = [*(home / name for name in HIDDEN_DIRS), *codebud_dirs(), *runtime_dirs()]
    unique = list(dict.fromkeys(Path(os.path.realpath(p)) for p in candidates))
    return [d for d in unique if not any(other != d and _contains(other, d) for other in unique)]


def _contains(directory: Path, path: Path) -> bool:
    """Whether ``path`` is ``directory`` or inside it (never hide the project)."""
    try:
        Path(path).resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True


def wrap(argv: list[str], project: Path, network: bool, kind: str) -> list[str]:
    """``argv`` wrapped for ``kind`` of isolation."""
    if kind == "unshare":
        if network:
            return argv  # unshare only cuts the network
        return ["unshare", "--user", "--map-root-user", "--net", "--", *argv]
    home = Path.home()
    cwd = Path.cwd()
    wrapped = ["bwrap", "--die-with-parent", "--new-session", "--unshare-all"]
    if network:
        wrapped.append("--share-net")
    wrapped += ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    # After the /tmp tmpfs, so a project under /tmp is still there.
    wrapped += ["--bind", str(project), str(project)]
    for directory in hidden_dirs(home):
        if directory.is_dir() and not _contains(directory, project):
            wrapped += ["--tmpfs", str(directory)]
    for name in HIDDEN_FILES:
        if (home / name).is_file():
            wrapped += ["--ro-bind", "/dev/null", str(home / name)]
    wrapped += ["--chdir", str(cwd), "--", *argv]
    return wrapped


def prepare(argv: list[str], network: bool = False) -> tuple[list[str] | None, str | None]:
    """(argv to run, None) -- or (None, why the step must not run).

    Follows the privacy level: trusted runs ``argv`` as it is; private and
    locked isolate it where possible; without isolation private warns once
    and runs it, locked refuses. Network steps are refused when locked.
    """
    global _warned
    from codebud import privacy
    from codebud.rag.indexer import project_root

    level = privacy.level()
    if level == "trusted":
        return argv, None
    if network and level == "locked":
        return None, privacy.locked_refusal("a step that needs the network")
    kind = available()
    if kind is None:
        if level == "locked":
            return None, (
                "privacy is locked and steps cannot be isolated here (install bubblewrap, "
                "or allow user namespaces); not running it."
            )
        if not _warned:
            _warned = True
            logger.warning(
                "Steps are not isolated: bubblewrap / user namespaces are unavailable. "
                "Install bubblewrap for network-free, project-only steps."
            )
        return argv, None
    if kind == "unshare" and network:
        # unshare can only cut the network; a step that needs it gets no
        # isolation at all here, so say so, as above (security review, low).
        global _warned_unshare_network
        if not _warned_unshare_network:
            _warned_unshare_network = True
            logger.warning(
                "A step that needs the network runs without isolation here: only "
                "unshare is available, and it can only cut the network. Install "
                "bubblewrap for project-only writes and hidden secrets."
            )
    return wrap(argv, project_root(), network, kind), None
