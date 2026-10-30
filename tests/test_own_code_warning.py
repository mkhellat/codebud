"""
tests/test_own_code_warning.py

Codebud may be run on its own checkout -- one may use it on itself -- but
there an edit or a step can change the code the next run executes, safety
checks included. It is allowed, and said once at startup (security review,
low; user decision 2026-10-30: warn and document, no read-only protection).
"""

import sys

import pytest

import codebud
from codebud.cli import common
from codebud.rag import indexer


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(indexer, "project_root", lambda *a: root)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "elsewhere-venv"))
    return root


def test_warns_when_its_package_is_inside_the_project(project, monkeypatch, capsys):
    package = project / "src" / "codebud"
    package.mkdir(parents=True)
    monkeypatch.setattr(codebud, "__file__", str(package / "__init__.py"))
    common.warn_if_own_code_in_project()
    err = capsys.readouterr().err
    assert "contains codebud's code" in err and "safety checks" in err


def test_warns_when_its_python_environment_is_inside_the_project(project, monkeypatch, capsys):
    (project / ".venv").mkdir()
    monkeypatch.setattr(sys, "prefix", str(project / ".venv"))
    common.warn_if_own_code_in_project()
    assert "Python environment codebud runs in" in capsys.readouterr().err


def test_says_nothing_for_an_ordinary_project(project, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(codebud, "__file__", str(tmp_path / "site" / "codebud" / "__init__.py"))
    common.warn_if_own_code_in_project()
    assert capsys.readouterr().err == ""
