"""
tests/test_build.py

The GNU Autotools build lists every installed file explicitly (automake has
no wildcards). These tests fail when a module, data file or manual chapter
is added without being listed, which would otherwise silently leave it out
of `make install` and the release tarball.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _am_list(makefile_am: Path, variable: str) -> set[str]:
    """Return the words assigned to ``variable`` in a Makefile.am."""
    text = makefile_am.read_text().replace("\\\n", " ")
    match = re.search(rf"^{re.escape(variable)}\s*=\s*(.*)$", text, re.M)
    assert match, f"{variable} not found in {makefile_am}"
    return set(match.group(1).split())


def test_every_python_module_is_installed():
    package = ROOT / "src"
    on_disk = {
        str(p.relative_to(package))
        for name in ("codebud", "codebud_sec")
        for p in (package / name).rglob("*.py")
        if "__pycache__" not in p.parts
    }
    assert _am_list(package / "Makefile.am", "nobase_python_PYTHON") == on_disk


def test_every_package_data_file_is_installed():
    package = ROOT / "src"
    on_disk = {
        str(p.relative_to(package))
        for p in (package / "codebud" / "data").rglob("*")
        if p.is_file()
    }
    assert _am_list(package / "Makefile.am", "nobase_dist_python_DATA") == on_disk


def test_every_manual_chapter_is_listed():
    docs = ROOT / "docs"
    on_disk = {str(p.relative_to(docs)) for p in (docs / "chapters").glob("*.texi")}
    assert _am_list(docs / "Makefile.am", "codebud_TEXINFOS") == on_disk


def test_every_test_file_is_in_the_manual():
    """Development > Test Suite lists each test file with what it covers."""
    manual = (ROOT / "docs" / "chapters" / "development.texi").read_text()
    listed = set(re.findall(r"\b(test_[a-z0-9_]+\.py)\b", manual))
    on_disk = {p.name for p in (ROOT / "tests").glob("test_*.py")}
    assert on_disk - listed == set()


def test_configure_takes_its_version_from_pyproject():
    configure_ac = (ROOT / "configure.ac").read_text()
    assert "pyproject.toml" in configure_ac
    assert re.search(r"AC_INIT\(\[codebud\],\s*m4_esyscmd_s", configure_ac)


def _news_version_ok(news: str, version: str) -> bool:
    """True if NEWS fits the version in pyproject.toml: its newest entry is
    that version, or (after a release) an (unreleased) entry for the next
    one sits on top of the released version's entry."""
    entries = re.findall(r"(?m)^\* Noteworthy changes in release (\S+) (.*)$", news)
    if not entries:
        return False
    (top, rest), *older = entries
    if top == version:
        return True
    return "unreleased" in rest and bool(older) and older[0][0] == version


def test_news_top_entry_matches_the_version():
    """NEWS's newest entry is the version in pyproject.toml (Versioning in
    the manual): a release cannot ship with its changes filed elsewhere."""
    version = re.search(r'(?m)^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text())
    assert version
    assert _news_version_ok((ROOT / "NEWS").read_text(), version.group(1))


@pytest.mark.parametrize(
    ("headings", "ok"),
    [
        (["0.1.0 (unreleased) [alpha]"], True),
        (["0.1.0 (2026-11-20) [alpha]"], True),
        (["0.1.1 (unreleased) [alpha]", "0.1.0 (2026-11-20) [alpha]"], True),
        (["0.2.0 (2026-12-01) [alpha]", "0.1.0 (2026-11-20) [alpha]"], False),
        (["0.1.1 (unreleased) [alpha]", "0.0.9 (2026-10-01) [alpha]"], False),
        (["0.1.1 (unreleased) [alpha]"], False),
        ([], False),
    ],
)
def test_news_version_rule(headings, ok):
    news = "".join(f"* Noteworthy changes in release {h}\n\ntext\n\n" for h in headings)
    assert _news_version_ok(news, "0.1.0") is ok


def test_readme_links_are_absolute():
    """PyPI renders README.md as the project page, where a relative link
    such as (COPYING) resolves against pypi.org and breaks."""
    relative = re.findall(r"\]\((?!https?://|#)([^)]+)\)", (ROOT / "README.md").read_text())
    assert relative == []
