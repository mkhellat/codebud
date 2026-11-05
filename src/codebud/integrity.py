"""
codebud/integrity.py

The integrity manifest (phase 10, decisions D14c and D14h): what the
model and the tools codebud runs looked like when you pinned them.

`codebud integrity pin` records the digest of the chat and embedding
models in Ollama and, for every program on the allow-list that resolves
on PATH, its real path and SHA-256. While a manifest exists, codebud
checks it before each run and before each step and refuses on any
mismatch: a model whose digest changed, a binary whose contents changed
or that now resolves somewhere else, and a program that is not pinned.

The manifest is plain JSON in the config directory, so it can be read
and compared. It guards against the model or a tool being swapped
underneath codebud; it cannot guard against someone who can also
rewrite the manifest (that is, anyone with your account).

This module does no network I/O: callers pass the model digests
(llm_stub.model_digests()).
"""

import hashlib
import json
import os
import shlex
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from codebud import paths
from codebud.fsutil import atomic_write_text, private_dir

MANIFEST_FILE = "integrity.json"
VERSION = 1


class IntegrityError(Exception):
    """A manifest that cannot be used, or a mismatch with it."""


@dataclass(frozen=True)
class Manifest:
    models: dict[str, str] = field(default_factory=dict)  # name -> Ollama digest
    binaries: dict[str, dict[str, str]] = field(default_factory=dict)  # name -> path, sha256
    created: str = ""


def model_name(name: str) -> str:
    """``name`` as Ollama lists it: with a tag (all-minilm -> all-minilm:latest)."""
    return name if ":" in name else f"{name}:latest"


def manifest_path() -> Path:
    return paths.config_dir() / MANIFEST_FILE


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve(program: str, path: str | None = None) -> str | None:
    """The real file ``program`` runs: an absolute path as given, a bare
    name looked up on ``path`` (default: PATH); symlinks followed."""
    found = program if os.path.isabs(program) else shutil.which(program, path=path)
    if not found or not os.path.isfile(found):
        return None
    return os.path.realpath(found)


def allow_listed_programs(policy) -> set[str]:
    """The programs the allow-list lets steps run: the first word of each
    harmless and powerful entry and trusted-sequence step. Relative paths
    (./configure) belong to a project, not to PATH, and are left out."""
    entries = [*policy.harmless, *policy.powerful]
    entries += [step for seq in policy.sequences for step in seq["sequence"]]
    programs = set()
    for entry in entries:
        try:
            words = shlex.split(entry)
        except ValueError:
            continue
        if words and ("/" not in words[0] or os.path.isabs(words[0])):
            programs.add(words[0])
    return programs


def build(models: dict[str, str], programs, path: str | None = None):
    """A new manifest for ``models`` (name -> digest) and ``programs``;
    also returns the programs that were not found, sorted."""
    binaries, missing = {}, []
    for program in sorted(set(programs)):
        real = resolve(program, path)
        if real is None:
            missing.append(program)
        else:
            binaries[program] = {"path": real, "sha256": file_sha256(real)}
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    models = {model_name(k): v for k, v in models.items()}
    return Manifest(models, binaries, created), missing


def save(manifest: Manifest) -> None:
    data = {
        "version": VERSION,
        "created": manifest.created,
        "models": manifest.models,
        "binaries": manifest.binaries,
    }
    private_dir(paths.config_dir())
    atomic_write_text(str(manifest_path()), json.dumps(data, indent=2, sort_keys=True) + "\n")


def load() -> Manifest | None:
    """The manifest, or None if there is none. A manifest that exists but
    cannot be used raises IntegrityError: it must never switch the
    checks off silently."""
    path = manifest_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise IntegrityError(f"{path}: cannot read the manifest: {exc}") from exc
    if not _valid(data):
        raise IntegrityError(
            f"{path}: not a version-{VERSION} integrity manifest; "
            "pin again with: codebud integrity pin"
        )
    return Manifest(data["models"], data["binaries"], data.get("created", ""))


def _valid(data) -> bool:
    if not isinstance(data, dict) or data.get("version") != VERSION:
        return False
    models, binaries = data.get("models"), data.get("binaries")
    if not isinstance(models, dict) or not isinstance(binaries, dict):
        return False
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in models.items()):
        return False
    return all(
        isinstance(v, dict) and isinstance(v.get("path"), str) and isinstance(v.get("sha256"), str)
        for v in binaries.values()
    )


def check_models(manifest: Manifest, names, digests: dict[str, str]) -> list[str]:
    """Problems with the models ``names`` given Ollama's ``digests``."""
    problems = []
    for name in map(model_name, names):
        pinned, now = manifest.models.get(name), digests.get(name)
        if pinned is None:
            problems.append(f"model {name}: not pinned")
        elif now is None:
            problems.append(f"model {name}: not in Ollama any more")
        elif now != pinned:
            problems.append(f"model {name}: digest changed (pinned {pinned}, now {now})")
    return problems


def check_binary(manifest: Manifest, program: str, path: str | None = None) -> str | None:
    """The problem with running ``program`` now, or None if it matches."""
    key = program
    if os.path.isabs(program):
        # An absolute program matches the pinned entry for that file.
        real = os.path.realpath(program)
        key = next((k for k, v in manifest.binaries.items() if v["path"] == real), program)
    pinned = manifest.binaries.get(key)
    if pinned is None:
        return f"{program}: not pinned"
    real = resolve(program, path)
    if real is None:
        return f"{program}: not found (pinned {pinned['path']})"
    if real != pinned["path"]:
        return f"{program}: now resolves to {real} (pinned {pinned['path']})"
    if file_sha256(real) != pinned["sha256"]:
        return f"{program}: contents changed ({real})"
    return None


def check_binaries(manifest: Manifest, path: str | None = None) -> list[str]:
    """Problems with every pinned binary."""
    problems = (check_binary(manifest, name, path) for name in sorted(manifest.binaries))
    return [p for p in problems if p]
