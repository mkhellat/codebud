"""
codebud/cli/doctor.py

Environment health checks for `codebud doctor`.

Each check is a function that returns (passed, label, hint).  passed is
True, False, or None for a warning: something optional is missing, which
does not count as a failure.  The label is always shown; failures and
warnings also print the hint, a one-line "How to fix:" suggestion.
"""

import json
import os
import shutil
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import requests

from codebud.llm_stub import (
    BACKENDS,
    backend,
    backend_description,
    ollama_base,
    ollama_proxies,
    remote_http_refusal,
)
from codebud.tools.embeddings import embed_model

# Linux memory statistics; macOS and FreeBSD have none, so the RAM check is
# skipped there.
MEMINFO_PATH = "/proc/meminfo"

# ANSI color helpers — degrade to no-op when stdout isn't a TTY (piped output).
_IS_TTY = sys.stdout.isatty()
_BOLD = "\033[1m" if _IS_TTY else ""
_DIM = "\033[2m" if _IS_TTY else ""
_GREEN = "\033[32m" if _IS_TTY else ""
_RED = "\033[31m" if _IS_TTY else ""
_YELLOW = "\033[33m" if _IS_TTY else ""
_RESET = "\033[0m" if _IS_TTY else ""

# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

CheckResult = tuple[bool | None, str, str]  # (passed or None = warning, label, hint)


def check_python_version() -> CheckResult:
    ok = sys.version_info >= (3, 10)
    ver = ".".join(str(v) for v in sys.version_info[:3])
    hint = "Install Python 3.10 or later (pacman -S python)."
    return ok, f"Python version: {ver}", hint


def check_agent_importable() -> CheckResult:
    try:
        import codebud  # noqa: F401

        return True, "codebud package is importable", ""
    except ImportError:
        hint = "Run: ./bootstrap --dev  (or: pip install -e . --group dev)"
        return False, "codebud package is NOT importable", hint


def check_backend() -> CheckResult:
    """Which backend prompts go to, and whether it is usable at all."""
    chosen = backend()
    if chosen not in BACKENDS:
        return (
            False,
            f"Unknown LLM backend '{chosen}'",
            "Set CODEBUD_BACKEND (or `backend` in config.toml) to ollama or openai.",
        )
    if chosen == "openai" and not os.environ.get("OPENAI_API_KEY"):
        return (
            False,
            f"LLM backend: {backend_description()}",
            "Set OPENAI_API_KEY, or go back to the local backend: CODEBUD_BACKEND=ollama.",
        )
    if chosen == "ollama" and remote_http_refusal(ollama_base()):
        return (
            False,
            f"LLM backend: {backend_description()}, plain http (refused)",
            "Use https for the remote server, or allow plain http with "
            "CODEBUD_ALLOW_REMOTE_HTTP=1 (allow_remote_http = true in your config.toml).",
        )
    return True, f"LLM backend: {backend_description()}", ""


def check_privacy() -> CheckResult:
    """The privacy level, and whether the chosen backend fits it."""
    from codebud import privacy
    from codebud.llm_stub import is_remote

    current = privacy.level()
    meaning = {
        "locked": "nothing leaves this machine",
        "private": "data leaves only where you chose",
        "trusted": "no prompts before egress; the allow-list still applies",
    }[current]
    label = f"Privacy: {current} ({meaning})"
    if current == "locked" and (backend() == "openai" or is_remote(ollama_base())):
        return (
            False,
            label + " -- but the backend is not on this machine",
            "Use a local Ollama (CODEBUD_BACKEND=ollama, OLLAMA_BASE_URL=http://localhost:11434), "
            "or choose another privacy level.",
        )
    return True, label, ""


def check_isolation() -> CheckResult:
    """How command steps are isolated at the current privacy level."""
    from codebud import isolation, privacy

    level = privacy.level()
    if level == "trusted":
        return True, "Isolation: off (privacy is trusted)", ""
    kind = isolation.available()
    label = f"Isolation: {isolation.describe(kind)}"
    if kind == "bwrap":
        return True, label, ""
    hint = (
        "Install bubblewrap (pacman -S bubblewrap / apt install bubblewrap / "
        "dnf install bubblewrap) and allow unprivileged user namespaces."
    )
    if kind is None and level == "locked":
        return False, label + " -- privacy is locked, so command steps are refused", hint
    return None, label, hint


def check_outbound() -> CheckResult:
    """Every place data can leave this machine to, given the settings."""
    from urllib.parse import urlsplit

    from codebud import privacy
    from codebud.llm_stub import is_remote
    from codebud.tools.web_search import enabled as web_search_enabled

    destinations = []
    if privacy.level() == "locked":
        pass  # refused at this level, whatever is configured (see the privacy check)
    elif backend() == "openai":
        destinations.append("OpenAI API (prompts)")
    elif is_remote(ollama_base()):
        destinations.append(f"{urlsplit(ollama_base()).hostname} (prompts, embeddings)")
    if web_search_enabled():
        destinations.append("lite.duckduckgo.com (web search queries)")
    if not destinations:
        return True, "Outbound: none - prompts, code and searches stay on this machine", ""
    return True, "Outbound: " + "; ".join(destinations), ""


def check_private_data() -> CheckResult:
    """Warn when codebud's data or config is readable by other users."""
    from codebud.paths import config_dir, data_dir

    loose_roots, loose = [], 0
    for root in (data_dir(), config_dir()):
        if not root.exists():
            continue
        found = 0
        for path in (root, *root.rglob("*")):
            try:
                mode = path.lstat().st_mode
            except OSError:
                continue
            if not stat.S_ISLNK(mode) and stat.S_IMODE(mode) & 0o077:
                found += 1
        if found:
            loose += found
            loose_roots.append(str(root))
    if not loose:
        return True, "Data and config files are private (owner only)", ""
    return (
        None,
        f"{loose} codebud files or directories are readable by other users",
        "Make them private: chmod -R go-rwx " + " ".join(loose_roots),
    )


def check_encryption() -> CheckResult:
    """The encryption key for data at rest, and a passphrase in the environment."""
    from codebud import crypto

    if crypto.passphrase_from_env() is not None:
        return (
            None,
            f"{crypto.PASSPHRASE_ENV} is set: whatever can read this environment "
            "can unlock your encrypted data",
            "Unset it unless this is an unattended run; codebud asks for the "
            "passphrase on the terminal otherwise.",
        )
    if crypto.enabled():
        try:
            crypto.check_ready()
        except crypto.CryptoError as exc:
            return False, "Encryption at rest is on but cannot work", str(exc)
        return (
            True,
            f"Encryption at rest: on (sessions, step history, indexes; key {crypto.key_dir()})",
            "",
        )
    if not crypto.available():
        return True, "Encryption at rest: off (pyrage not installed)", ""
    if not crypto.has_key():
        return True, "Encryption at rest: off (no key; create one with: codebud key init)", ""
    return True, "Encryption at rest: off (turn it on with encrypt = true in config.toml)", ""


def check_integrity() -> CheckResult:
    """The integrity manifest: what the next run would check (integrity.py)."""
    from codebud import integrity, profiles
    from codebud.cli.common import plural
    from codebud.llm_stub import model_digests
    from codebud.rag.context import enabled as rag_enabled

    try:
        manifest = integrity.load()
    except integrity.IntegrityError as exc:
        return False, "Integrity: the manifest cannot be used", str(exc)
    if manifest is None:
        profile = profiles.active()
        if profile.integrity:
            return (
                False,
                f"Integrity: profile {profile.name} requires a pinned manifest",
                "Pin the model and tools: codebud integrity pin",
            )
        return (
            True,
            "Integrity: nothing pinned (codebud integrity pin to check model and tools)",
            "",
        )
    models = [os.environ.get("OLLAMA_MODEL", "")] + ([embed_model()] if rag_enabled() else [])
    problems = integrity.start_problems(manifest, [m for m in models if m], model_digests())
    if problems:
        return (
            False,
            f"Integrity: {plural(len(problems), 'mismatch')} with the pinned manifest",
            f"{problems[0]}; codebud integrity verify lists them all",
        )
    count = len(manifest.binaries)
    return (
        True,
        f"Integrity: {plural(len(manifest.models), 'model')}, "
        f"{count} binar{'y' if count == 1 else 'ies'} pinned ({manifest.created}); all match",
        "",
    )


def check_ollama_model_set() -> CheckResult:
    model = os.environ.get("OLLAMA_MODEL", "")
    if model:
        return True, f"OLLAMA_MODEL={model}", ""
    hint = "Run: export OLLAMA_MODEL=qwen2.5-coder:3b-instruct-q4_K_M"
    return False, "OLLAMA_MODEL is not set", hint


def check_ollama_reachable() -> CheckResult:
    url = f"{ollama_base()}/api/tags"
    try:
        r = requests.get(url, timeout=3, proxies=ollama_proxies(ollama_base()))
        if r.status_code == 200:
            return True, f"Ollama server reachable at {ollama_base()}", ""
        hint = "sudo systemctl start ollama  (or: ollama serve)"
        return False, f"Ollama returned HTTP {r.status_code}", hint
    except requests.exceptions.ConnectionError:
        hint = "Start the server: sudo systemctl start ollama  (or: ollama serve)"
        return False, f"Cannot connect to Ollama at {ollama_base()}", hint
    except Exception as exc:
        hint = "sudo systemctl start ollama"
        return False, f"Ollama check failed: {exc}", hint


def check_model_pulled() -> CheckResult:
    model = os.environ.get("OLLAMA_MODEL", "")
    if not model:
        return False, "OLLAMA_MODEL not set (skipping model-pull check)", "Set OLLAMA_MODEL first."
    try:
        r = requests.get(
            f"{ollama_base()}/api/tags", timeout=3, proxies=ollama_proxies(ollama_base())
        )
        if r.status_code != 200:
            return False, "Cannot reach Ollama to check model list", "sudo systemctl start ollama"
        names = [m.get("name", "") for m in r.json().get("models", [])]
        if model in names:
            return True, f"Model '{model}' is available", ""
        hint = f"ollama pull {model}"
        return False, f"Model '{model}' is NOT pulled", hint
    except Exception:
        return False, "Could not read model list from Ollama", "sudo systemctl start ollama"


def check_embed_model_pulled() -> CheckResult:
    """Warn (not fail) when the embedding model for project search is missing."""
    model = embed_model()
    try:
        r = requests.get(
            f"{ollama_base()}/api/tags", timeout=3, proxies=ollama_proxies(ollama_base())
        )
        names = {m.get("name", "") for m in r.json().get("models", [])}
    except Exception:
        return None, f"Could not check embedding model '{model}'", "Start Ollama first."
    # Ollama lists untagged pulls as "<name>:latest".
    if model in names or f"{model}:latest" in names:
        return True, f"Embedding model '{model}' is available", ""
    label = f"Embedding model '{model}' is not pulled (needed only for project search)"
    return None, label, f"ollama pull {model}"


def check_config_files() -> CheckResult:
    """Check that the policy files in effect exist and parse."""
    from codebud.paths import project_policy_file, user_policy_file
    from codebud.policy import POLICY_FILES as names

    project = [p for p in (project_policy_file(n) for n in names) if p is not None]
    for path in [user_policy_file(n) for n in names] + project:
        if path is None:
            continue
        if not path.exists():
            return (
                False,
                f"Missing policy file: {path}",
                "Reinstall codebud, or copy the defaults into ~/.config/codebud/.",
            )
        try:
            json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            return False, f"Invalid JSON in {path}: {exc}", f"Fix the JSON in {path}."
    where = user_policy_file(names[0]).parent
    if project:
        from codebud import policy

        trusted = "trusted" if policy.effective().trusted else "not trusted: narrowing only"
        return True, f"Policy files OK ({where}; project {project[0].parent}, {trusted})", ""
    return True, f"Policy files OK ({where})", ""


def check_disk_space() -> CheckResult:
    models_dir = Path.home() / ".ollama" / "models"
    if not models_dir.exists():
        return True, "~/.ollama/models/ not found (no models downloaded yet)", ""
    usage = shutil.disk_usage(models_dir)
    free_gb = usage.free / 1024**3
    if free_gb >= 2.0:
        return True, f"Free disk space: {free_gb:.1f} GB", ""
    hint = "Free up disk space; at least 2 GB is needed for the smallest model."
    return False, f"Low disk space: {free_gb:.1f} GB free near ~/.ollama/models/", hint


def _read_meminfo() -> dict[str, int]:
    """Parse MEMINFO_PATH ("Name:   123 kB" lines) into {name: kilobytes}."""
    values = {}
    with open(MEMINFO_PATH) as f:
        for line in f:
            name, _, rest = line.partition(":")
            fields = rest.split()
            if fields and fields[0].isdigit():
                values[name.strip()] = int(fields[0])
    return values


def check_ram() -> CheckResult:
    try:
        available_mb = _read_meminfo().get("MemAvailable", 0) // 1024
        model = os.environ.get("OLLAMA_MODEL", "")
        # Rough RAM requirements per model family
        thresholds = {
            "1.5b": 2048,
            "3b": 4096,
            "7b": 8192,
            "14b": 16384,
        }
        needed_mb = 4096  # default guess
        for key, mb in thresholds.items():
            if key in model.lower():
                needed_mb = mb
                break
        if available_mb >= needed_mb:
            return True, f"Available RAM: {available_mb} MB (need ~{needed_mb} MB)", ""
        hint = (
            f"Close other applications. The selected model needs ~{needed_mb} MB "
            f"of free RAM; only {available_mb} MB is available."
        )
        return False, f"Low RAM: {available_mb} MB available, ~{needed_mb} MB needed", hint
    except Exception:
        return True, "RAM check skipped (could not read /proc/meminfo)", ""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_CHECKS: list[Callable[[], CheckResult]] = [
    check_python_version,
    check_agent_importable,
    check_backend,
    check_privacy,
    check_isolation,
    check_outbound,
    check_ollama_model_set,
    check_ollama_reachable,
    check_model_pulled,
    check_embed_model_pulled,
    check_config_files,
    check_private_data,
    check_encryption,
    check_integrity,
    check_disk_space,
    check_ram,
]


def run_doctor() -> int:
    """Run all checks and print a report. Returns exit code (0=all pass)."""
    failures = 0
    warnings = 0
    print(f"{_BOLD}Codebud environment check{_RESET}\n")
    for check_fn in _CHECKS:
        passed, label, hint = check_fn()
        if passed:
            print(f"  {_GREEN}[PASS]{_RESET} {label}")
        elif passed is None:
            warnings += 1
            print(f"  {_YELLOW}[WARN]{_RESET} {label}")
            if hint:
                print(f"         {_DIM}-> {hint}{_RESET}")
        else:
            failures += 1
            print(f"  {_RED}[FAIL]{_RESET} {label}")
            if hint:
                print(f"         {_DIM}-> {hint}{_RESET}")
    print()
    if failures == 0:
        note = f" ({warnings} warning(s) above)" if warnings else ""
        print(f"{_GREEN}All checks passed. Codebud is ready.{_RESET}{note}")
    else:
        print(
            f"{_RED}{failures} check(s) failed.{_RESET} Fix the issues above and re-run: codebud doctor"
        )
    return 0 if failures == 0 else 1
