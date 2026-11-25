"""
tests/test_packaging.py

codebud and codebud-sec are two PyPI distributions released together from
one repository. Covers the split: the root pyproject.toml ships only the
codebud package, packaging/codebud-sec/pyproject.toml ships the security
layer with every profile file, and scripts/stage-codebud-sec lays the second
distribution out with the real version and every module of src/codebud_sec.
"""

import fnmatch
import shutil
import subprocess
from pathlib import Path

import pytest

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
SEC_SRC = ROOT / "src" / "codebud_sec"
TEMPLATE = ROOT / "packaging" / "codebud-sec" / "pyproject.toml"
STAGE = ROOT / "scripts" / "stage-codebud-sec"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")


def _load(path: Path) -> dict:
    return tomllib.loads(path.read_text())


def _version() -> str:
    return _load(ROOT / "pyproject.toml")["project"]["version"]


def _stage(out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(STAGE), str(ROOT), str(out)], capture_output=True, text=True)


@pytest.fixture
def staged(tmp_path):
    out = tmp_path / "stage"
    result = _stage(out)
    assert result.returncode == 0, result.stderr
    return out


def test_root_distribution_is_the_core_only():
    data = _load(ROOT / "pyproject.toml")
    find = data["tool"]["setuptools"]["packages"]["find"]
    assert find["include"] == ["codebud", "codebud.*"]
    assert not any(fnmatch.fnmatch("codebud_sec", pat) for pat in find["include"])
    assert "codebud-sec" not in data["project"]["scripts"]
    assert "codebud_sec" not in data["tool"]["setuptools"]["package-data"]


def test_security_distribution_template():
    data = _load(TEMPLATE)
    project = data["project"]
    assert project["name"] == "codebud-sec"
    assert project["version"] == "@VERSION@"
    assert project["dependencies"] == ["codebud[crypto]==@VERSION@"]
    assert project["scripts"]["codebud-sec"] == "codebud_sec.cli:main"
    find = data["tool"]["setuptools"]["packages"]["find"]
    assert find["include"] == ["codebud_sec", "codebud_sec.*"]


def test_security_package_data_covers_every_profile_file():
    patterns = _load(TEMPLATE)["tool"]["setuptools"]["package-data"]["codebud_sec"]
    files = [p for p in (SEC_SRC / "data" / "profiles").rglob("*") if p.is_file()]
    assert files
    for path in files:
        rel = path.relative_to(SEC_SRC).as_posix()
        assert any(fnmatch.fnmatch(rel, pat) for pat in patterns), rel


def test_stage_writes_the_distribution(staged):
    version = _version()
    text = (staged / "pyproject.toml").read_text()
    assert "@VERSION@" not in text
    data = tomllib.loads(text)
    assert data["project"]["version"] == version
    assert data["project"]["dependencies"] == [f"codebud[crypto]=={version}"]
    assert (staged / "codebud_sec" / "cli.py").is_file()
    assert any((staged / "codebud_sec" / "data" / "profiles").glob("*.toml"))
    assert (staged / "README.md").is_file()
    assert (staged / "COPYING").is_file()
    assert not list(staged.rglob("__pycache__"))
    assert not list(staged.rglob("*.pyc"))


def test_stage_copies_every_module(staged):
    def py_files(base: Path) -> set[str]:
        return {
            p.relative_to(base).as_posix()
            for p in base.rglob("*.py")
            if "__pycache__" not in p.parts
        }

    assert py_files(staged / "codebud_sec") == py_files(SEC_SRC)


def test_stage_refuses_a_non_empty_directory(tmp_path):
    (tmp_path / "x").write_text("in the way")
    result = _stage(tmp_path)
    assert result.returncode != 0
    assert "not an empty directory" in result.stderr
    assert not (tmp_path / "pyproject.toml").exists()


def test_stage_accepts_an_empty_directory(tmp_path):
    assert _stage(tmp_path).returncode == 0
    assert (tmp_path / "pyproject.toml").is_file()


def test_the_core_sdist_prunes_the_security_layer():
    """Review of the split: whatever a stale *.egg-info lists, the codebud
    sdist must not carry src/codebud_sec (it is codebud-sec's)."""
    lines = (ROOT / "MANIFEST.in").read_text().splitlines()
    assert "prune src/codebud_sec" in [line.strip() for line in lines]
    assert "MANIFEST.in" in (ROOT / "Makefile.am").read_text()
