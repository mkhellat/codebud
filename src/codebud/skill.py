"""
codebud/skill.py

codebud's skills: SKILL.md files (shipped as package data) that teach other
agents when and how to call the codebud CLI. The format, Markdown with a
name/description frontmatter, is read by Claude Code, OpenClaw, Agensi and
others.

Two variants ship:

- ``default`` — for agents that install the skill locally (Claude Code,
  OpenClaw). Assumes ``codebud`` is already on PATH.
- ``agensi`` — hand-off skill for the Agensi marketplace: when to use
  codebud, how to install it, then how to call it. Upload the file as a
  ZIP with ``SKILL.md`` at the root (``codebud skill show --for agensi``).
"""

from pathlib import Path

from codebud.fsutil import atomic_write_text

_SKILL_DIR = Path(__file__).resolve().parent / "data" / "skill"

SKILL_FILES = {
    "default": _SKILL_DIR / "SKILL.md",
    "agensi": _SKILL_DIR / "agensi" / "SKILL.md",
}

# Where each supported agent looks for personal skills (relative to $HOME).
# These agents use the default skill text.
AGENT_SKILL_DIRS = {
    "claude": Path(".claude") / "skills" / "codebud",
    "openclaw": Path(".openclaw") / "skills" / "codebud",
}

# ``codebud skill show --for …``: marketplace / agent names → skill variant.
SHOW_FOR = {
    "claude": "default",
    "openclaw": "default",
    "agensi": "agensi",
}


class SkillError(Exception):
    """The skill could not be installed or resolved."""


def skill_text(kind: str = "default") -> str:
    """Return the contents of a shipped SKILL.md variant."""
    try:
        path = SKILL_FILES[kind]
    except KeyError as exc:
        raise SkillError(
            f"Unknown skill kind '{kind}'; choose one of: {', '.join(SKILL_FILES)}."
        ) from exc
    return path.read_text()


def kind_for_show(agent: str | None) -> str:
    """Map ``skill show --for`` to a SKILL.md variant (default if omitted)."""
    if agent is None:
        return "default"
    try:
        return SHOW_FOR[agent]
    except KeyError as exc:
        raise SkillError(f"Unknown agent '{agent}'; choose one of: {', '.join(SHOW_FOR)}.") from exc


def target_dir(agent: str) -> Path:
    """Return the skill directory for ``agent`` in the user's home."""
    if agent not in AGENT_SKILL_DIRS:
        raise SkillError(f"Unknown agent '{agent}'; choose one of: {', '.join(AGENT_SKILL_DIRS)}.")
    return Path.home() / AGENT_SKILL_DIRS[agent]


def install(directory: Path | str, force: bool = False, kind: str = "default") -> Path:
    """Write SKILL.md into ``directory`` and return its path.

    An existing identical copy is left as is. A copy that differs (perhaps
    edited by the user) is only replaced with ``force``.
    """
    path = Path(directory) / "SKILL.md"
    text = skill_text(kind)
    # Never write through a symbolic link: it could point anywhere, and a
    # dangling one would create its target (security review, low).
    if path.is_symlink():
        raise SkillError(f"{path} is a symbolic link; remove it or install with --dir elsewhere.")
    if path.exists() and path.read_text() != text and not force:
        raise SkillError(
            f"{path} exists and differs from codebud's skill; use --force to replace it."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(str(path), text)  # all or nothing
    return path
