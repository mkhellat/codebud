"""
codebud/cli/integrity.py

codebud integrity: the pinned manifest of models and tools (integrity.py).

  integrity pin      record the chat and embedding models' digests and the
                     SHA-256 of every allow-listed program on PATH (plus the
                     isolation tool); --model / --command add more
  integrity verify   compare what is there now with the manifest
  integrity show     list what is pinned
"""

import os
import sys
from pathlib import Path

from codebud import integrity
from codebud.cli.common import plural


def _fail(message: str) -> None:
    print(f"codebud: {message}", file=sys.stderr)
    sys.exit(1)


def _load():
    try:
        return integrity.load()
    except integrity.IntegrityError as exc:
        _fail(str(exc))


def _digests() -> dict[str, str]:
    from codebud.llm_stub import model_digests

    digests = model_digests()
    if digests is None:
        _fail("cannot reach Ollama to read the model digests (is `ollama serve` running?)")
    return digests


def _chat_model() -> str:
    return os.environ.get("OLLAMA_MODEL", "")


def _embed_model() -> str:
    from codebud.tools.embeddings import embed_model

    return embed_model()


def _shipped_files() -> list[Path]:
    """Every file of the security layer's profiles (their TOML and policy files)."""
    from codebud import profiles

    directory = profiles.shipped_profile_dir()
    if directory is None:
        return []
    return sorted(p for p in directory.rglob("*") if p.is_file())


def _shipped_programs() -> set[str]:
    """The programs the shipped profiles' allow-lists name, so they are
    pinned whichever profile is active when you pin."""
    from codebud import policy

    programs = set()
    for path in _shipped_files():
        if path.name in (policy.HARMLESS_FILE, policy.POWERFUL_FILE):
            entries = policy._commands(path) or []
            programs |= integrity.allow_listed_programs(policy.Policy(entries, [], []))
        elif path.name == policy.TRUSTED_SEQ_FILE:
            sequences = policy._sequences(path) or []
            programs |= integrity.allow_listed_programs(policy.Policy([], [], sequences))
    return programs


def _programs(extra) -> set[str]:
    from codebud import isolation, policy, profiles

    programs = integrity.allow_listed_programs(policy.effective()) | set(extra)
    programs |= _shipped_programs()
    tool = isolation.available()
    if tool:
        programs.add(tool)  # it runs every isolated step
    if profiles.shipped_profile_dir() is not None and isolation.fence_available():
        programs |= set(isolation.FENCE_PROGRAMS)  # the security layer's network fence
    return programs


def _pin(models_extra, commands_extra, yes: bool) -> None:
    chat = _chat_model()
    if not chat:
        _fail("no chat model to pin: set OLLAMA_MODEL (or `model` in config.toml)")
    digests = _digests()

    wanted = [integrity.model_name(chat)]
    for name in map(integrity.model_name, models_extra):
        if name not in digests:
            _fail(f"model {name} is not downloaded (ollama pull {name}); nothing was pinned")
        wanted.append(name)
    if wanted[0] not in digests:
        _fail(f"the chat model {wanted[0]} is not downloaded; nothing was pinned")
    embed = integrity.model_name(_embed_model())
    if embed in digests:
        wanted.append(embed)
    else:
        print(
            f"codebud: the embedding model {embed} is not downloaded, so it is not pinned; "
            "while this manifest is in force, project search is refused.",
            file=sys.stderr,
        )

    manifest, missing = integrity.build(
        {name: digests[name] for name in wanted},
        _programs(commands_extra),
        files=_shipped_files(),
    )
    if missing:
        print(
            "codebud: not installed, so not pinned (steps using them will be refused): "
            + ", ".join(missing),
            file=sys.stderr,
        )

    path = integrity.manifest_path()
    if path.exists() and not yes:
        if not sys.stdin.isatty():
            _fail(f"{path} exists; re-run with --yes to replace it")
        answer = input(f"Replace {path} with what is installed now? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            _fail("nothing was changed")
    integrity.save(manifest)
    count = len(manifest.binaries)
    binaries = f"{count} binar{'y' if count == 1 else 'ies'}"
    files = f", {plural(len(manifest.files), 'profile file')}" if manifest.files else ""
    print(f"Pinned {plural(len(manifest.models), 'model')}, {binaries}{files} in {path}")


def _verify() -> None:
    manifest = _load()
    if manifest is None:
        _fail("nothing is pinned yet: codebud integrity pin")
    digests = _digests()
    names = list(manifest.models)
    chat = _chat_model()
    if chat and integrity.model_name(chat) not in names:
        names.append(chat)
    problems = integrity.check_models(manifest, names, digests)
    problems += integrity.check_binaries(manifest)
    problems += integrity.check_files(manifest)
    if problems:
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        _fail(
            f"{plural(len(problems), 'mismatch')} with {integrity.manifest_path()}; if the "
            "changes are yours (an update), pin again: codebud integrity pin"
        )
    print(f"Everything matches {integrity.manifest_path()} (pinned {manifest.created}).")


def _show() -> None:
    manifest = _load()
    if manifest is None:
        print("Nothing is pinned (codebud integrity pin).")
        return
    print(f"{integrity.manifest_path()} (pinned {manifest.created})")
    print("Models:")
    for name, digest in sorted(manifest.models.items()):
        print(f"  {name:<32} {digest}")
    print("Binaries:")
    for name, entry in sorted(manifest.binaries.items()):
        print(f"  {name:<16} {entry['sha256'][:16]}  {entry['path']}")
    if manifest.files:
        print("Profile files:")
        for path, digest in sorted(manifest.files.items()):
            print(f"  {digest[:16]}  {path}")


def cmd_integrity(args):
    if args.integrity_action == "pin":
        _pin(args.model or [], args.command or [], args.yes)
    elif args.integrity_action == "verify":
        _verify()
    else:
        _show()
