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

A network step can be fenced (phase 10, step 5 plan, PR 6): an optional
layer registers a function (add_network_fence) that says which addresses
the step may reach, and the step then runs in a network namespace of its
own whose only way out is pasta (passt), with nftables letting through
only those addresses:

- pasta --no-map-gw -t none -u none -T none -U none: nothing of the host
  is reachable -- not its loopback, not through the gateway address, no
  port forwarded either way (pasta's defaults reach the host's loopback);
- nft loads the rules in that namespace (pasta's user namespace gives
  CAP_NET_ADMIN there) before bwrap starts the step with --cap-drop ALL in
  a user namespace of its own, so the step cannot change them -- and has
  no raw sockets, whose packets would pass by the rules;
- no DNS: the names the step uses are resolved before, into an /etc/hosts
  of its own, and resolv.conf is empty -- DNS through the host's resolver
  would carry data to whoever serves a name.
"""

import atexit
import ipaddress
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
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


def wrap(
    argv: list[str],
    project: Path,
    network: bool,
    kind: str,
    observer: bool = False,
    files: tuple[tuple[str, str], ...] = (),
    no_capabilities: bool = False,
) -> list[str]:
    """``argv`` wrapped for ``kind`` of isolation. ``observer`` (bwrap only):
    the host's /proc instead of a fresh one, so the step sees the host's
    processes and, through /proc/1/net, its sockets -- while its own process
    namespace keeps it from signalling them and it still has no network.
    ``files`` (bwrap only): (host file, path in the step) bound read-only
    over the step's view; ``no_capabilities``: --cap-drop ALL."""
    if kind == "unshare":
        if network:
            return argv  # unshare only cuts the network
        return ["unshare", "--user", "--map-root-user", "--net", "--", *argv]
    home = Path.home()
    cwd = Path.cwd()
    wrapped = ["bwrap", "--die-with-parent", "--new-session", "--unshare-all"]
    if network:
        wrapped.append("--share-net")
    if no_capabilities:
        wrapped += ["--cap-drop", "ALL"]
    proc = ["--ro-bind", "/proc", "/proc"] if observer else ["--proc", "/proc"]
    wrapped += ["--ro-bind", "/", "/", "--dev", "/dev", *proc, "--tmpfs", "/tmp"]
    # After the /tmp tmpfs, so a project under /tmp is still there.
    wrapped += ["--bind", str(project), str(project)]
    for directory in hidden_dirs(home):
        if directory.is_dir() and not _contains(directory, project):
            wrapped += ["--tmpfs", str(directory)]
    for name in HIDDEN_FILES:
        if (home / name).is_file():
            wrapped += ["--ro-bind", "/dev/null", str(home / name)]
    for source, target in files:
        wrapped += ["--ro-bind", source, target]
    wrapped += ["--chdir", str(cwd), "--", *argv]
    return wrapped


# --- the network fence ---------------------------------------------------------------

FENCE_PROGRAMS = ("pasta", "nft", "sh")
_PASTA_OPTIONS = ["--config-net", "--quiet", "--no-map-gw", "-t", "none", "-u", "none",
                  "-T", "none", "-U", "none"]  # fmt: skip
# Run inside pasta's namespace: load the rules, then the step.
_FENCE_SCRIPT = 'printf %s "$1" | "$2" -f - || exit 97; shift 2; exec "$@"'


@dataclass(frozen=True)
class Fence:
    """What a fenced step may reach: ``allow`` networks, never ``deny``
    networks (they win), and ``hosts``: (name, address) for its
    /etc/hosts."""

    allow: tuple = ()
    deny: tuple = ()
    hosts: tuple[tuple[str, str], ...] = ()


def nft_ruleset(fence: Fence) -> str:
    """The nftables rules for ``fence``: everything out is dropped but the
    namespace's loopback, replies, and the allowed networks; denied
    networks are dropped first, so they win over an allowed one around
    them."""

    def rule(network, verdict):
        network = ipaddress.ip_network(network, strict=False)
        family = "ip" if network.version == 4 else "ip6"
        return f"    {family} daddr {network} {verdict}"

    lines = [
        "table inet codebud_fence {",
        "  chain out {",
        "    type filter hook output priority 0; policy drop;",
        '    oif "lo" accept',
        "    ct state established,related accept",
        *(rule(n, "drop") for n in fence.deny),
        *(rule(n, "accept") for n in fence.allow),
        "  }",
        "}",
    ]
    return "\n".join(lines) + "\n"


def fence_available() -> bool:
    return all(shutil.which(program) for program in FENCE_PROGRAMS)


_FENCES: list = []
_fence_dir: Path | None = None


def add_network_fence(fence) -> None:
    """Register ``fence(words)``: a Fence for a network step that must be
    fenced, None for one that need not be, or a str: why it must not run."""
    _FENCES.append(fence)


def remove_network_fence(fence) -> None:
    if fence in _FENCES:
        _FENCES.remove(fence)


def _fence_for(words: list[str]):
    for hook in list(_FENCES):
        try:
            decision = hook(words)
        except Exception as exc:  # noqa: BLE001 - a broken fence never lets a step out
            return f"the network fence failed ({type(exc).__name__}: {exc})"
        if decision is not None:
            return decision
    return None


def _fence_files(fence: Fence) -> tuple[tuple[str, str], ...]:
    """A hosts file and an empty resolv.conf for the step, in a private
    directory removed when codebud exits."""
    global _fence_dir
    if _fence_dir is None:
        _fence_dir = Path(tempfile.mkdtemp(prefix="codebud-fence-"))
        atexit.register(shutil.rmtree, _fence_dir, True)
    handle, hosts = tempfile.mkstemp(dir=_fence_dir, prefix="hosts-")
    with os.fdopen(handle, "w") as f:
        f.write("127.0.0.1 localhost\n::1 localhost\n")
        f.writelines(f"{address} {name}\n" for name, address in fence.hosts)
    resolv = _fence_dir / "resolv.conf"
    resolv.write_text("# no DNS in a fenced step\n")
    return ((hosts, "/etc/hosts"), (str(resolv), "/etc/resolv.conf"))


def fenced(inner: list[str], fence: Fence) -> list[str]:
    """``inner`` (a bwrap command line) run behind ``fence``."""
    return ["pasta", *_PASTA_OPTIONS, "--", "sh", "-c", _FENCE_SCRIPT, "codebud-fence",
            nft_ruleset(fence), "nft", *inner]  # fmt: skip


def prepare(argv: list[str], network: bool = False) -> tuple[list[str] | None, str | None]:
    """(argv to run, None) -- or (None, why the step must not run).

    Follows the privacy level: trusted runs ``argv`` as it is; private and
    locked isolate it where possible; without isolation private warns once
    and runs it, locked refuses. Network steps are refused when locked, and
    fenced when a registered fence says so (which needs bwrap and pasta).
    """
    argv, refusal, _ = prepare_with_programs(argv, network)
    return argv, refusal


def prepare_with_programs(
    argv: list[str], network: bool = False
) -> tuple[list[str] | None, str | None, list[int]]:
    """prepare(), and the positions in the result of the programs it added
    (the isolation tool, the fence's), for the integrity check."""
    global _warned
    from codebud import privacy, profiles
    from codebud.rag.indexer import project_root

    level = privacy.level()
    fence = _fence_for(argv) if network else None
    if isinstance(fence, str):
        return None, f"Refused: {fence}", []
    if fence is not None:
        if level == "trusted" or available() != "bwrap" or not fence_available():
            return (
                None,
                (
                    "this step must run behind the network fence, which needs bubblewrap, "
                    "pasta (passt) and nft, at privacy private or locked; not running it."
                ),
                [],
            )
        if level == "locked":
            return None, privacy.locked_refusal("a step that needs the network"), []
        inner = wrap(argv, project_root(), True, "bwrap", files=_fence_files(fence),
                     no_capabilities=True)  # fmt: skip
        outer = fenced(inner, fence)
        start = len(outer) - len(inner)
        sh, nft = outer.index("sh"), start - 1
        return outer, None, [0, sh, nft, start]
    if level == "trusted":
        return argv, None, []
    if network and level == "locked":
        return None, privacy.locked_refusal("a step that needs the network"), []
    kind = available()
    # Observer isolation is bubblewrap's or nothing: anything less would
    # share the host's process namespace and could signal its processes.
    observer = profiles.active().isolation == "observer"
    if observer and kind != "bwrap":
        return (
            None,
            (
                "this profile's observer isolation needs bubblewrap: without it a step "
                "would share the host's process namespace and could signal (kill) its "
                "processes; not running it."
            ),
            [],
        )
    if kind is None:
        if level == "locked":
            return (
                None,
                (
                    "privacy is locked and steps cannot be isolated here (install bubblewrap, "
                    "or allow user namespaces); not running it."
                ),
                [],
            )
        if not _warned:
            _warned = True
            logger.warning(
                "Steps are not isolated: bubblewrap / user namespaces are unavailable. "
                "Install bubblewrap for network-free, project-only steps."
            )
        return argv, None, []
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
    return wrap(argv, project_root(), network, kind, observer=observer), None, [0]
