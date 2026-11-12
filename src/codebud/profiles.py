"""
codebud/profiles.py

Profiles (phase 10, first part; design settled 2026-10-28). A profile is a
named bundle of the two dials and what goes with them:

- mode      how much codebud asks (modes.py): the mode chat starts in;
            "full-auto" also makes `run` and `plan --execute` unattended
- privacy   what it may reach (privacy.py)
- policy    a directory of policy files (harmless_commands.json, ...)
            that take precedence over the project's, the user's and the
            shipped ones
- prompt    extra instructions for the planner
- encrypt   true turns encryption at rest on (crypto.py); a profile can
            only turn it on, never off
- isolation "observer" lets command steps see the host's processes (the
            host's /proc, read-only) while keeping their own process and
            network namespaces -- for live triage (isolation.py)
- integrity true refuses to start without a pinned integrity manifest
            (integrity.py); with one, it is checked whatever the profile
- engagement true refuses command and web search steps unless a step
            check providing "engagement" is registered (safety.py) -- the
            security layer's scope guard, when an engagement is open

Built in: "default" (ask, private) and "unattended" (full-auto, trusted).
The security layer, codebud_sec, ships more in its data/profiles/: they
are found by that exact package name, without importing it, and only
when it is installed alongside codebud. Your own live in
$XDG_CONFIG_HOME/codebud/profiles/<name>.toml and cannot take a shipped
profile's name; a project cannot define or choose one, since a profile
can lower the privacy level.

Choose with --profile, `/profile` in chat, CODEBUD_PROFILE or `profile =`
in your config.toml. An explicit --privacy, --mode, --yes or
CODEBUD_PRIVACY still wins over the profile; config.toml does not.
Whatever the profile, the allow-list and the refused arguments apply.
"""

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from codebud import paths
from codebud.modes import MODES
from codebud.privacy import LEVELS

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

DEFAULT = "default"
PROFILE_DIR = "profiles"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_KEYS = ("description", "mode", "privacy", "policy", "prompt", "isolation")
ISOLATIONS = ("default", "observer")
_BOOL_KEYS = ("encrypt", "integrity", "engagement")


class ProfileError(Exception):
    """A profile that does not exist or cannot be used."""


@dataclass(frozen=True)
class Profile:
    name: str
    description: str = ""
    mode: str = "ask"
    privacy: str = "private"
    policy: Path | None = None  # directory of policy files
    prompt: str = ""
    source: str = "built in"  # or the profile file
    encrypt: bool = False  # turn encryption at rest on
    integrity: bool = False  # refuse to start without an integrity manifest
    isolation: str = "default"  # or "observer" (isolation.py)
    engagement: bool = False  # steps need an open engagement (safety.py)

    def summary(self) -> str:
        extras = (", encrypted" if self.encrypt else "") + (", pinned" if self.integrity else "")
        extras += ", observer" if self.isolation == "observer" else ""
        extras += ", engagement" if self.engagement else ""
        return f"{self.mode}, privacy {self.privacy}" + extras


BUILTIN = {
    "default": Profile("default", "Ask before each step; data stays where you chose."),
    "unattended": Profile(
        "unattended",
        "Fully unattended: nothing is asked, steps are not isolated. The allow-list still applies.",
        mode="full-auto",
        privacy="trusted",
    ),
}

_active: Profile = BUILTIN[DEFAULT]


def profile_dir() -> Path:
    return paths.config_dir() / PROFILE_DIR


def shipped_profile_dir() -> Path | None:
    """codebud_sec's data/profiles/, or None. Found by the exact package
    name with importlib.util.find_spec, which does not import it, and only
    when it sits beside codebud itself (the same site directory or source
    tree): another codebud_sec elsewhere on the path is not the layer."""
    import importlib.util

    try:
        spec = importlib.util.find_spec("codebud_sec")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.origin:
        return None
    layer = Path(spec.origin).resolve().parent
    if layer.parent != Path(__file__).resolve().parent.parent:
        return None
    directory = layer / "data" / PROFILE_DIR
    return directory if directory.is_dir() else None


def _shipped(name: str) -> Path | None:
    directory = shipped_profile_dir()
    path = directory / f"{name}.toml" if directory is not None else None
    return path if path is not None and path.is_file() else None


def _load_file(name: str, path: Path) -> Profile:
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ProfileError(f"profile {name} ({path}): {exc}") from exc
    problems = [f"unknown key '{k}'" for k in data if k not in (*_KEYS, *_BOOL_KEYS)]
    problems += [
        f"'{k}' must be a string" for k in _KEYS if k in data and not isinstance(data[k], str)
    ]
    problems += [
        f"'{k}' must be true or false"
        for k in _BOOL_KEYS
        if k in data and not isinstance(data[k], bool)
    ]
    if problems:
        raise ProfileError(f"profile {name} ({path}): {'; '.join(problems)}")
    mode = data.get("mode", "ask")
    if mode not in MODES:
        raise ProfileError(f"profile {name}: mode '{mode}' is not one of {', '.join(MODES)}")
    kind = data.get("isolation", "default")
    if kind not in ISOLATIONS:
        raise ProfileError(
            f"profile {name}: isolation '{kind}' is not one of {', '.join(ISOLATIONS)}"
        )
    level = data.get("privacy", "private")
    if level not in LEVELS:
        raise ProfileError(f"profile {name}: privacy '{level}' is not one of {', '.join(LEVELS)}")
    policy = None
    if "policy" in data:
        policy = path.parent / Path(data["policy"]).expanduser()
        if not policy.is_dir():
            raise ProfileError(f"profile {name}: policy directory {policy} does not exist")
    return Profile(
        name,
        data.get("description", ""),
        mode,
        level,
        policy,
        data.get("prompt", "").strip(),
        str(path),
        data.get("encrypt", False),
        data.get("integrity", False),
        kind,
        data.get("engagement", False),
    )


def get(name: str) -> Profile:
    """The profile ``name``: the user's file if there is one, else built in."""
    if not _NAME.match(name):
        raise ProfileError(f"'{name}' is not a valid profile name (a-z, 0-9, '-', '_')")
    path = profile_dir() / f"{name}.toml"
    shipped = _shipped(name)
    if shipped is not None and path.is_file():
        # A stray or planted file must not quietly weaken a shipped profile.
        raise ProfileError(
            f"profile {name} ships with codebud's security layer; your {path} would "
            "replace it -- give your file another name"
        )
    if path.is_file():
        return _load_file(name, path)
    if shipped is not None:
        return _load_file(name, shipped)
    if name in BUILTIN:
        return BUILTIN[name]
    raise ProfileError(f"no profile '{name}' (choose one of: {', '.join(available())})")


def available() -> list[str]:
    names = set(BUILTIN)
    shipped = shipped_profile_dir()
    if shipped is not None:
        names |= {p.stem for p in shipped.glob("*.toml") if _NAME.match(p.stem)}
    if profile_dir().is_dir():
        names |= {p.stem for p in profile_dir().glob("*.toml") if _NAME.match(p.stem)}
    return sorted(names)


def active() -> Profile:
    return _active


def activate(profile: Profile, source: str, explicit: bool = False) -> None:
    """Make ``profile`` the active one; ``source`` is what chose it.

    Its privacy level replaces one from config.toml or an earlier profile,
    but not CODEBUD_PRIVACY set in the environment -- unless ``explicit``
    (/profile in chat, which, like /privacy, overrides everything). The
    default profile nobody chose changes nothing.
    """
    global _active
    from codebud import settings

    _active = profile
    paths.PROFILE_POLICY_DIR = profile.policy
    if profile.encrypt:
        # Only ever turns encryption on, so it may override everything --
        # even for a default profile nobody chose (your own default.toml).
        settings.set_value("CODEBUD_ENCRYPT", "1", f"profile {profile.name}")
    if source == "default" and not explicit:
        return
    settings.set_value("CODEBUD_PROFILE", profile.name, source)
    origin = f"profile {profile.name}"
    if explicit:
        settings.set_value("CODEBUD_PRIVACY", profile.privacy, origin)
    else:
        settings.set_default("CODEBUD_PRIVACY", profile.privacy, origin)


def prompt_note() -> str:
    """Text added to the planner prompt for the active profile ('' if none)."""
    if not _active.prompt:
        return ""
    return f"PROFILE {_active.name}:\n{_active.prompt}\n\n"
