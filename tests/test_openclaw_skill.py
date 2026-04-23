"""
tests/test_openclaw_skill.py

Verifies that the OpenClaw SKILL.md is well-formed and that the codebud
binary is accessible to the gateway's skill checker.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_MD = Path(__file__).parent.parent / "openclaw" / "SKILL.md"
MANAGED_SKILL = Path.home() / ".openclaw" / "skills" / "codebud" / "SKILL.md"


# ---------------------------------------------------------------------------
# SKILL.md content tests
# ---------------------------------------------------------------------------

class TestSkillMd:
    def test_skill_md_exists(self):
        assert SKILL_MD.exists(), "openclaw/SKILL.md not found in repo"

    def test_frontmatter_has_required_keys(self):
        text = SKILL_MD.read_text()
        assert text.startswith("---"), "SKILL.md must start with YAML frontmatter"
        assert "name: codebud" in text
        assert "description:" in text
        assert '"emoji"' in text
        assert '"requires"' in text
        assert '"bins"' in text

    def test_body_covers_main_subcommands(self):
        text = SKILL_MD.read_text()
        for cmd in ("codebud run", "codebud plan", "codebud chat", "codebud doctor"):
            assert cmd in text, f"SKILL.md missing documentation for '{cmd}'"

    def test_no_progress_flag_documented(self):
        text = SKILL_MD.read_text()
        assert "--no-progress" in text, "SKILL.md must document --no-progress flag"

    def test_source_is_managed_skill(self):
        """openclaw-managed copy must exist and match the repo source."""
        if not MANAGED_SKILL.exists():
            pytest.skip("Skill not installed — run 'make install-skill' first")
        assert MANAGED_SKILL.read_text() == SKILL_MD.read_text(), (
            "Managed skill is out of sync with repo; run 'make install-skill'"
        )


# ---------------------------------------------------------------------------
# Binary availability test
# ---------------------------------------------------------------------------

class TestBinaryAvailability:
    def test_codebud_binary_in_path(self):
        assert shutil.which("codebud") is not None, (
            "codebud not found in PATH — run 'make install-skill' to install"
        )

    def test_codebud_version_exits_zero(self):
        result = subprocess.run(
            ["codebud", "version"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "codebud" in result.stdout

    def test_codebud_doctor_exits_cleanly(self):
        """doctor must exit 0 or 1 — never crash with a traceback."""
        result = subprocess.run(
            ["codebud", "doctor"],
            capture_output=True,
            text=True,
        )
        assert result.returncode in (0, 1), (
            f"codebud doctor crashed (exit {result.returncode}):\n{result.stderr}"
        )
        assert "Traceback" not in result.stderr


# ---------------------------------------------------------------------------
# openclaw CLI integration (requires openclaw in PATH)
# ---------------------------------------------------------------------------

class TestOpenClawIntegration:
    @pytest.fixture(autouse=True)
    def require_openclaw(self):
        if shutil.which("openclaw") is None:
            pytest.skip("openclaw CLI not in PATH")

    def test_skill_appears_in_list(self):
        result = subprocess.run(
            ["openclaw", "skills", "list"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "codebud" in result.stdout, (
            "codebud skill not found in 'openclaw skills list' — "
            "run 'make install-skill' to register it"
        )

    def test_skill_is_ready(self):
        result = subprocess.run(
            ["openclaw", "skills", "list"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        lines = result.stdout.splitlines()
        codebud_line = next((l for l in lines if "codebud" in l), None)
        assert codebud_line is not None, "codebud not in skill list"
        assert "ready" in codebud_line, (
            f"codebud skill is not ready:\n{codebud_line}\n"
            "Run 'make install-skill' to register the skill and binary."
        )
