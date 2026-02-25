"""
OpenClaw integration package for Codebud.

This package exposes the Codebud agent to the OpenClaw UI. The main
entry points are:

- OpenClawSkill: a thin wrapper around AgentCore (see skill.py)
- register(): required by OpenClaw to discover available skills
"""

from .skill import OpenClawSkill
from .register import register

__all__ = ["OpenClawSkill", "register"]

