"""
codebud/settings.py

Codebud's settings and the configuration file (phase 8, decision D12a).

Every setting has an environment variable and a key for the TOML
configuration file. Where a value comes from, highest first:

1. the environment variable;
2. the project's .codebud/config.toml (nearest at or above the current
   directory) -- but never an egress setting, see below;
3. the user's $XDG_CONFIG_HOME/codebud/config.toml;
4. the default.

Egress settings decide whether data leaves the machine (the backend, the
Ollama URL, plain-http opt-in, the OpenAI key, web search); protective
ones guard stored data (secret masking). A project file may set neither. A project file
must not set them: cloning a repository must never redirect your prompts.
Such keys in a project file are ignored with a warning, as are unknown
keys, values of the wrong type and files that are not valid TOML.

apply() copies file values into the environment where the variable is not
set yet, so the rest of codebud reads the environment as before; the CLI
calls it at startup. resolve() says where each value came from, for
`codebud config`.
"""

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from codebud import paths

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

logger = logging.getLogger(__name__)

CONFIG_FILE = "config.toml"


@dataclass(frozen=True)
class Setting:
    key: str  # in config.toml
    env: str  # environment variable
    default: object
    kind: type  # str, int or bool
    egress: bool = False  # may make data leave the machine
    secret: bool = False  # never displayed
    protective: bool = False  # protects stored data; weakening it is the user's call
    user_only: str = ""  # another reason a project file may not set it (the reason)


SETTINGS = [
    Setting("backend", "CODEBUD_BACKEND", "ollama", str, egress=True),
    Setting("model", "OLLAMA_MODEL", None, str),
    Setting("ollama_base_url", "OLLAMA_BASE_URL", "http://localhost:11434", str, egress=True),
    Setting("allow_remote_http", "CODEBUD_ALLOW_REMOTE_HTTP", False, bool, egress=True),
    Setting("embed_model", "EMBED_MODEL", "all-minilm", str),
    Setting("rag", "CODEBUD_RAG", True, bool),
    Setting("rag_hints", "CODEBUD_RAG_HINTS", True, bool),
    # A project file could ask for a window that exhausts the machine's memory.
    Setting("num_ctx", "CODEBUD_NUM_CTX", 8192, int, user_only="sizes the model's memory use"),
    Setting("openai_api_key", "OPENAI_API_KEY", None, str, egress=True, secret=True),
    Setting(
        "openai_model",
        "OPENAI_MODEL",
        "gpt-3.5-turbo",
        str,
        user_only="decides what is billed to your OpenAI account",
    ),
    Setting("web_search", "WEB_SEARCH_ENABLED", False, bool, egress=True),
    Setting("mask_secrets", "CODEBUD_MASK_SECRETS", True, bool, protective=True),
    # Encryption at rest (crypto.py): sessions, step history, new indexes.
    Setting("encrypt", "CODEBUD_ENCRYPT", False, bool, protective=True),
    # More environment variables for command steps (sandbox.py); a project
    # file must not be able to hand them the user's tokens, hence protective.
    Setting("step_env", "CODEBUD_STEP_ENV", "", str, protective=True),
    Setting("privacy", "CODEBUD_PRIVACY", "private", str, protective=True),
    # A profile can lower the privacy level, so a project may not choose one.
    Setting("profile", "CODEBUD_PROFILE", "default", str, protective=True),
    # 0 keeps sessions forever (decision D12e); a project file must not be
    # able to delete the user's sessions, hence protective.
    Setting("session_retention_days", "CODEBUD_SESSION_RETENTION_DAYS", 0, int, protective=True),
]
BY_KEY = {s.key: s for s in SETTINGS}
BY_ENV = {s.env: s for s in SETTINGS}

# Environment variables apply() set, with the source of their value.
_applied: dict[str, tuple[str, str]] = {}  # env -> (value, source)

# Warnings already given: the files are read by apply() and again by
# resolve(), and each problem should be reported once per run.
_warned: set[str] = set()


def _warn(message: str, *args) -> None:
    text = message % args
    if text not in _warned:
        _warned.add(text)
        logger.warning(text)


def user_config_file() -> Path:
    return paths.config_dir() / CONFIG_FILE


def project_config_file() -> Path | None:
    directory = paths.project_config_dir()
    if directory is None or not (directory / CONFIG_FILE).is_file():
        return None
    return directory / CONFIG_FILE


def _as_env(setting: Setting, value) -> str:
    if setting.kind is bool:
        return "1" if value else "0"
    return str(value)


def _read(path: Path | None, source: str) -> dict[str, str]:
    """Valid entries of the config file at ``path`` as {env var: value}."""
    if path is None or not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, ValueError) as exc:  # TOMLDecodeError is a ValueError
        _warn("Ignoring %s: %s", path, exc)
        return {}
    values = {}
    for key, value in data.items():
        setting = BY_KEY.get(key)
        if setting is None:
            _warn("Ignoring unknown setting '%s' in %s", key, path)
            continue
        if source == "project config" and (
            setting.egress or setting.protective or setting.user_only
        ):
            if setting.egress:
                why = "decides where data is sent"
            elif setting.protective:
                why = "protects your stored data"
            else:
                why = setting.user_only
            _warn(
                "Ignoring '%s' in %s: it can only be set in %s or the environment, since it %s",
                key,
                path,
                user_config_file(),
                why,
            )
            continue
        # bool is a subclass of int: require the exact type.
        if type(value) is not setting.kind:
            _warn(
                "Ignoring '%s' in %s: expected %s, got %r",
                key,
                path,
                setting.kind.__name__,
                value,
            )
            continue
        values[setting.env] = _as_env(setting, value)
    return values


def _file_values() -> dict[str, tuple[str, str]]:
    """{env var: (value, source)} from the config files, project winning."""
    merged = {
        env: (v, "user config") for env, v in _read(user_config_file(), "user config").items()
    }
    project = _read(project_config_file(), "project config")
    merged.update({env: (v, "project config") for env, v in project.items()})
    return merged


def apply() -> None:
    """Copy config-file values into the environment where it has none."""
    for env, (value, source) in _file_values().items():
        if env not in os.environ:
            os.environ[env] = value
            _applied[env] = (value, source)


_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def flag(env: str, default: bool, unknown: bool | None = None) -> bool:
    """The boolean setting ``env``: 1/true/yes/on or 0/false/no/off, any case.

    One parser for every switch (security review, low: CODEBUD_ENCRYPT=true
    used to mean "off" while other switches accepted it). Unset or empty
    gives ``default``. Anything else is warned about once and gives
    ``unknown`` -- the safe side for that switch: on for protective ones
    (encryption, masking), off for egress ones -- or ``default``.
    """
    raw = os.environ.get(env, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    fallback = default if unknown is None else unknown
    _warn(
        "%s=%r is not a yes/no value (1/true/yes/on or 0/false/no/off); treating it as %s",
        env,
        os.environ.get(env),
        "on" if fallback else "off",
    )
    return fallback


def from_environment(env: str) -> bool:
    """Whether ``env`` was set outside codebud (not from a file or profile)."""
    if env not in os.environ:
        return False
    applied = _applied.get(env)
    return applied is None or applied[0] != os.environ[env]


def set_value(env: str, value: str, source: str) -> None:
    """Set ``env`` to ``value``, shown as coming from ``source``."""
    os.environ[env] = value
    _applied[env] = (value, source)


def set_default(env: str, value: str, source: str) -> None:
    """Like set_value(), unless the environment already sets ``env``."""
    if not from_environment(env):
        set_value(env, value, source)


def resolve() -> dict[str, tuple[object, str]]:
    """{env var: (value, source)} for every setting, as codebud sees it now.

    The source is "environment", "project config", "user config",
    "default", or what set it (a profile, --privacy); values apply()
    copied in keep their file as the source.
    """
    files = _file_values()
    resolved = {}
    for s in SETTINGS:
        current = os.environ.get(s.env)
        if current is not None:
            applied = _applied.get(s.env)
            source = applied[1] if applied and applied[0] == current else "environment"
            resolved[s.env] = (current, source)
        elif s.env in files:
            resolved[s.env] = files[s.env]
        else:
            resolved[s.env] = (s.default, "default")
    return resolved


def display_value(env: str, value) -> str:
    """``value`` as `codebud config` shows it (secrets masked)."""
    setting = BY_ENV.get(env)
    if setting is not None and setting.secret:
        return "(set)" if value else "(not set)"
    if value is None:
        return "(not set)"
    if setting is not None and setting.kind is bool and isinstance(value, bool):
        return "1" if value else "0"
    return str(value)
