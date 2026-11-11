"""
tests/test_integrity_steps.py

While an integrity manifest exists, each command step's programs are
checked right before they run (D14h): the program itself, the one a
wrapper such as `timeout` runs, and the isolation tool. A mismatch or an
unpinned program is refused and nothing runs.
"""

import pytest

from codebud import integrity
from codebud.sandbox import Sandbox


@pytest.fixture
def bindir(tmp_path, monkeypatch):
    """PATH with tools that leave a mark when they run."""
    d = tmp_path / "bin"
    d.mkdir()
    marks = tmp_path / "marks"
    marks.mkdir()
    for name in ("grep", "timeout", "ls", "wrap"):
        exe = d / name
        if name in ("timeout", "wrap"):  # run the rest of the command line
            body = f'touch "{marks}/{name}"; shift_n={1 if name == "timeout" else 0}\n'
            body += '[ "$shift_n" = 1 ] && shift; exec "$@"\n'
        else:
            body = f'touch "{marks}/{name}"; echo {name}\n'
        exe.write_text("#!/bin/sh\n" + body)
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{d}:/usr/bin:/bin")
    # No real isolation here; a test can wrap with `wrap` instead.
    monkeypatch.setattr("codebud.isolation.prepare", lambda argv, network=False: (argv, None))
    return d, marks


def _pin(bindir, programs):
    manifest, _ = integrity.build({}, programs, path=str(bindir))
    integrity.save(manifest)


def test_no_manifest_runs_as_before(bindir):
    d, marks = bindir
    result = Sandbox().run_command("grep x")
    assert result["returncode"] == 0
    assert (marks / "grep").exists()


def test_pinned_program_runs(bindir):
    d, marks = bindir
    _pin(d, ["grep"])
    result = Sandbox().run_command("grep x")
    assert result["returncode"] == 0, result["stderr"]
    assert (marks / "grep").exists()


def test_changed_program_is_refused_and_not_run(bindir):
    d, marks = bindir
    _pin(d, ["grep"])
    (d / "grep").write_text(f'#!/bin/sh\ntouch "{marks}/grep"\n')
    result = Sandbox().run_command("grep x")
    assert result["returncode"] == 1
    assert "grep: contents changed" in result["stderr"]
    assert "codebud integrity pin" in result["stderr"]
    assert not (marks / "grep").exists()


def test_unpinned_program_is_refused(bindir):
    d, marks = bindir
    _pin(d, ["grep"])
    result = Sandbox().run_command("ls")
    assert result["returncode"] == 1
    assert "ls: not pinned" in result["stderr"]
    assert not (marks / "ls").exists()


def test_the_program_behind_a_wrapper_is_checked(bindir):
    d, marks = bindir
    _pin(d, ["timeout"])  # grep not pinned
    result = Sandbox().run_command("timeout 5 grep x")
    assert result["returncode"] == 1
    assert "grep: not pinned" in result["stderr"]
    assert not (marks / "timeout").exists()


def test_the_isolation_tool_is_checked(bindir, monkeypatch):
    d, marks = bindir
    monkeypatch.setattr(
        "codebud.isolation.prepare", lambda argv, network=False: (["wrap", *argv], None)
    )
    _pin(d, ["grep"])  # the wrapper is not pinned
    result = Sandbox().run_command("grep x")
    assert result["returncode"] == 1
    assert "wrap: not pinned" in result["stderr"]
    _pin(d, ["grep", "wrap"])
    result = Sandbox().run_command("grep x")
    assert result["returncode"] == 0, result["stderr"]
    assert (marks / "wrap").exists() and (marks / "grep").exists()


def test_a_project_script_is_not_checked(bindir, tmp_path, monkeypatch):
    """./configure and the like are project files, which a manifest of
    installed programs cannot pin; the allow-list still decides."""
    d, marks = bindir
    _pin(d, ["grep"])
    project = tmp_path / "project"
    project.mkdir()
    script = project / "build.sh"
    script.write_text("#!/bin/sh\necho built\n")
    script.chmod(0o755)
    monkeypatch.chdir(project)
    result = Sandbox().run_command("./build.sh")
    assert result["returncode"] == 0, result["stderr"]
    assert "built" in result["stdout"]


def test_damaged_manifest_refuses_every_step(bindir):
    d, marks = bindir
    integrity.manifest_path().parent.mkdir(parents=True, exist_ok=True)
    integrity.manifest_path().write_text("[]")
    result = Sandbox().run_command("grep x")
    assert result["returncode"] == 1
    assert "integrity.json" in result["stderr"]
    assert not (marks / "grep").exists()


def test_a_relative_path_leaving_the_project_is_checked(bindir, tmp_path, monkeypatch):
    """Only a path that resolves inside the project is a project file;
    ../ that leaves it reaches an installed program like any other."""
    d, marks = bindir
    _pin(d, ["grep"])
    outside = tmp_path / "outside"
    outside.mkdir()
    tool = outside / "tool"
    tool.write_text(f'#!/bin/sh\ntouch "{marks}/tool"\n')
    tool.chmod(0o755)
    project = tmp_path / "project"
    (project / "sub").mkdir(parents=True)
    monkeypatch.chdir(project)
    for cmd in ("../outside/tool", "sub/../../outside/tool"):
        result = Sandbox().run_command(cmd)
        assert result["returncode"] == 1, cmd
        assert "not pinned" in result["stderr"], cmd
    assert not (marks / "tool").exists()


def test_a_symlink_in_the_project_pointing_out_is_checked(bindir, tmp_path, monkeypatch):
    d, marks = bindir
    _pin(d, ["grep"])
    project = tmp_path / "project"
    project.mkdir()
    (project / "ls-link").symlink_to(d / "ls")
    monkeypatch.chdir(project)
    result = Sandbox().run_command("./ls-link")
    assert result["returncode"] == 1
    assert not (marks / "ls").exists()
