"""
openclaw/register.py

This module exposes the `register()` function required by OpenClaw.
It returns a dictionary mapping skill names to skill instances.

OpenClaw will call:
    skills = register()
    skill = skills["local_coding_agent"]

Then it will call:
    skill.run(...)
    skill.regenerate(...)
"""

from .skill import OpenClawSkill


def register():
    """
    Register the Local Coding Agent skill with OpenClaw.

    Returns:
    {
        "local_coding_agent": <OpenClawSkill instance>
    }
    """
    return {
        "local_coding_agent": OpenClawSkill()
    }
