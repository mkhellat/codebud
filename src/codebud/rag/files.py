"""
codebud/rag/files.py

Choose the files the project index reads (phase 9, B2).

In a git work tree the candidates are git's own view of the project:
tracked files plus new files that are not ignored (``git ls-files
--cached --others --exclude-standard``), so .gitignore and
.git/info/exclude apply. Elsewhere the directory is walked, skipping
hidden directories, virtual environments, caches and build output.

Then, in order, a candidate is skipped when it:

- looks like a secret (keys, .env/.envrc files, credential stores, shell
  histories, Terraform state, ~/.ssh-style directories) -- always,
  whatever the ignore files say, since the index keeps a copy of the text;
- matches a pattern in the project's .codebudignore;
- is a symbolic link, a lock/minified/source-map file, larger than
  MAX_FILE_BYTES, or not UTF-8 text.
"""

import fnmatch
import os
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from codebud import gitsafe

MAX_FILE_BYTES = 256 * 1024
IGNORE_FILE = ".codebudignore"

SECRET_NAMES = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.keystore",
    "*.jks",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".envrc",
    ".netrc",
    ".pgpass",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "*credential*",
    "*secret*",
    "*.gpg",
    "*.kdbx",
    ".vault-token",
    ".htpasswd",
    ".s3cfg",
    ".boto",
    ".dockercfg",
    "*.tfvars",
    "*.tfstate",
    "*.tfstate.*",
    # shell and REPL histories (commands, often with passwords and tokens)
    ".*history",
    "*_history",
    ".histfile",
)
SECRET_DIRS = {
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".kube",
    ".docker",
    ".password-store",
    ".terraform",
}

GENERATED_NAMES = ("*.lock", "package-lock.json", "*.min.js", "*.min.css", "*.map")

# Directories never entered when walking a directory that is not a git
# work tree (hidden directories are skipped too).
WALK_SKIP_DIRS = {"node_modules", "__pycache__", "venv", "env", "build", "dist", "site-packages"}


@dataclass
class Selection:
    files: list[str]  # relative POSIX paths, sorted
    skipped: Counter = field(default_factory=Counter)  # reason -> count


def select_files(root: Path | str) -> Selection:
    """Return the files under ``root`` the index should read."""
    root = Path(root)
    candidates = _git_files(root)
    if candidates is None:
        candidates = _walk(root)
    ignore = _ignore_patterns(root)
    selection = Selection(files=[])
    for rel in sorted(set(candidates)):
        reason = _skip_reason(root, rel, ignore)
        if reason:
            selection.skipped[reason] += 1
        else:
            selection.files.append(rel)
    return selection


def _git_files(root: Path) -> list[str] | None:
    """Files git considers part of the project, or None outside a work tree."""
    try:
        result = subprocess.run(
            gitsafe.git_argv(
                ["ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root
            ),
            cwd=root,
            capture_output=True,
            check=False,
            env=gitsafe.git_environment(),  # the repository runs nothing (gitsafe.py)
        )
    except OSError:  # git not installed
        return None
    if result.returncode != 0:
        return None
    return [p for p in result.stdout.decode("utf-8", "surrogateescape").split("\0") if p]


def _walk(root: Path) -> list[str]:
    found = []
    for directory, dirs, names in os.walk(root):
        dirs[:] = [
            d
            for d in dirs
            if not d.startswith(".") and d not in WALK_SKIP_DIRS and not d.endswith(".egg-info")
        ]
        base = Path(directory).relative_to(root)
        found.extend((base / name).as_posix() for name in names)
    return found


def _ignore_patterns(root: Path) -> list[str]:
    try:
        lines = (root / IGNORE_FILE).read_text().splitlines()
    except OSError:
        return []
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def _ignored(rel: str, patterns: list[str]) -> bool:
    name = rel.rsplit("/", 1)[-1]
    for pattern in patterns:
        if pattern.endswith("/"):
            if rel.startswith(pattern) or f"/{pattern}" in f"/{rel}":
                return True
        elif fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
            return True
    return False


def _matches(name: str, patterns) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def candidate_files(root: Path | str) -> list[str]:
    """Like select_files().files, but judged by name and size only.

    Nothing is read, so this is cheap enough to run before every chat
    prompt (staleness checks); binary files may be included.
    """
    root = Path(root)
    candidates = _git_files(root)
    if candidates is None:
        candidates = _walk(root)
    ignore = _ignore_patterns(root)
    found = []
    for rel in sorted(set(candidates)):
        if _name_skip_reason(rel, ignore):
            continue
        path = root / rel
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        found.append(rel)
    return found


def _name_skip_reason(rel: str, ignore: list[str]) -> str | None:
    """Reasons to skip ``rel`` that need only its path."""
    parts = rel.split("/")
    name = parts[-1]
    if _matches(name.lower(), SECRET_NAMES) or SECRET_DIRS & set(parts[:-1]):
        return "secret"
    if _ignored(rel, ignore):
        return "ignored"
    if _matches(name, GENERATED_NAMES):
        return "generated"
    return None


def _skip_reason(root: Path, rel: str, ignore: list[str]) -> str | None:
    name = rel.rsplit("/", 1)[-1]
    reason = _name_skip_reason(rel, ignore)
    if reason in ("secret", "ignored"):
        return reason
    path = root / rel
    if path.is_symlink():
        return "symlink"
    if not path.is_file():
        return "missing"
    if _matches(name, GENERATED_NAMES):
        return "generated"
    if path.stat().st_size > MAX_FILE_BYTES:
        return "too large"
    if not _is_text(path):
        return "binary"
    return None


def _is_text(path: Path) -> bool:
    """True for UTF-8 text without NUL bytes."""
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if b"\0" in data[:8192]:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True
