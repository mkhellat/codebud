"""
tests/test_release_check.py

scripts/release-check --quick (git state, tag, NEWS, lint) against a
throwaway git repository. The full run (distcheck, wheel install) is
exercised by `make release-check` itself, not here.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release-check"

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="needs git"),
    pytest.mark.skipif(
        subprocess.run([sys.executable, "-m", "ruff", "--version"], capture_output=True).returncode,
        reason="needs ruff (dev group)",
    ),
]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    """A checkout of version 1.2.3 that is ready to release."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.2.3"\n')
    (tmp_path / "NEWS").write_text(
        "X NEWS\n\n* Noteworthy changes in release 1.2.3 (2026-11-04) [alpha]\n\nFirst.\n"
    )
    (tmp_path / "x.py").write_text('print("x")\n')
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _check(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(SCRIPT), "--quick", str(repo), sys.executable],
        capture_output=True,
        text=True,
    )


def test_ready_checkout_passes(repo):
    result = _check(repo)
    assert result.returncode == 0, result.stdout
    assert "codebud 1.2.3 is ready to release" in result.stdout
    assert "FAIL" not in result.stdout


def test_unreleased_news_entry_fails(repo):
    news = repo / "NEWS"
    news.write_text(news.read_text().replace("(2026-11-04)", "(unreleased)"))
    _git(repo, "commit", "-q", "-am", "unreleased")
    result = _check(repo)
    assert result.returncode == 1
    assert "still marked (unreleased)" in result.stdout


def test_news_for_another_version_fails(repo):
    toml = repo / "pyproject.toml"
    toml.write_text(toml.read_text().replace("1.2.3", "1.2.4"))
    _git(repo, "commit", "-q", "-am", "bump")
    result = _check(repo)
    assert result.returncode == 1
    assert "newest entry is not release 1.2.4" in result.stdout


def test_existing_tag_fails(repo):
    _git(repo, "tag", "v1.2.3")
    result = _check(repo)
    assert result.returncode == 1
    assert "tag v1.2.3 already exists" in result.stdout


def test_dirty_tree_fails(repo):
    (repo / "stray.txt").write_text("x\n")
    result = _check(repo)
    assert result.returncode == 1
    assert "working tree has uncommitted or untracked files" in result.stdout


def test_other_branch_fails(repo):
    _git(repo, "checkout", "-q", "-b", "topic")
    result = _check(repo)
    assert result.returncode == 1
    assert "on branch 'topic', not main" in result.stdout


def test_unpushed_head_fails(repo):
    _git(repo, "update-ref", "refs/remotes/github/main", "HEAD")
    _git(repo, "remote", "add", "github", "https://example.invalid/x")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "later")
    result = _check(repo)
    assert result.returncode == 1
    assert "github/main is not at HEAD" in result.stdout


def test_lint_failure_fails(repo):
    (repo / "x.py").write_text("import os\n")
    _git(repo, "commit", "-q", "-am", "unused import")
    result = _check(repo)
    assert result.returncode == 1
    assert "FAIL  ruff check" in result.stdout


def test_every_failure_is_listed(repo):
    """It reports all problems at once instead of stopping at the first."""
    _git(repo, "tag", "v1.2.3")
    (repo / "stray.txt").write_text("x\n")
    result = _check(repo)
    assert result.stdout.count("FAIL") == 2
    assert "2 check(s) failed" in result.stdout
